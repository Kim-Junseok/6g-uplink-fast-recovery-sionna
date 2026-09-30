#!/usr/bin/env python3
"""Verify the publication candidate's frozen source and raw-record inventory.

This reads local files only. Original private manifest hashes in the
transformation log are provenance assertions; the private originals are not
required by this public verifier.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_RUN_COUNTS = {
    "v0_5": 32,
    "v0_6_p3": 16,
    "v0_6_k3": 8,
    "v0_6_f3": 8,
    "v0_7_rho070": 64,
    "v0_8_test_d": 8,
}
PUBLIC_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
REDACTED = "<local-python-executable-redacted>"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PRIVATE_PATH_PATTERNS = (
    re.compile(rb"/home/[^/\s]+/"),
    re.compile(rb"/Users/[^/\s]+/"),
    re.compile(rb"/mnt/[a-z]/Users/[^/\s]+/", re.IGNORECASE),
    re.compile(rb"[A-Za-z]:\\Users\\[^\\\s]+\\"),
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def path_for(relative: str) -> Path:
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts,
            f"unsafe relative path: {relative}")
    absolute = ROOT / path
    require(absolute.resolve().is_relative_to(ROOT.resolve()),
            f"path escapes release root: {relative}")
    require(absolute.is_file(), f"missing file: {relative}")
    return absolute


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def no_private_path(data: bytes, label: str) -> None:
    require(not any(pattern.search(data) for pattern in PRIVATE_PATH_PATTERNS),
            f"personal absolute path in public file: {label}")


def load_public_ids() -> dict[str, dict]:
    """Map public directory names to frozen campaign and scenario identities."""
    path = path_for("provenance/public_id_map.json")
    no_private_path(path.read_bytes(), str(path))
    document = json.loads(path.read_text(encoding="utf-8"))
    require(set(document) == {"campaigns"} and isinstance(document["campaigns"], list),
            "invalid public ID map structure")
    campaigns = {}
    public_campaigns = set()
    scenario_count = 0
    for item in document["campaigns"]:
        require(set(item) == {"legacy_id", "public_id", "run_count", "scenarios"},
                "invalid public campaign mapping")
        legacy = item["legacy_id"]
        public = item["public_id"]
        require(legacy in EXPECTED_RUN_COUNTS and legacy not in campaigns and
                item["run_count"] == EXPECTED_RUN_COUNTS[legacy],
                f"invalid public campaign identity/count: {legacy}")
        require(isinstance(public, str) and PUBLIC_ID.fullmatch(public) is not None and
                public not in public_campaigns,
                f"invalid or duplicate public campaign ID: {public}")
        require(isinstance(item["scenarios"], list), f"invalid scenarios: {legacy}")
        scenarios = {}
        original_scenarios = set()
        for scenario in item["scenarios"]:
            require(set(scenario) == {"legacy_id", "public_id"},
                    f"invalid public scenario mapping: {legacy}")
            original_id = scenario["legacy_id"]
            public_id = scenario["public_id"]
            require(isinstance(original_id, str) and original_id and
                    original_id not in original_scenarios and
                    isinstance(public_id, str) and PUBLIC_ID.fullmatch(public_id) is not None and
                    public_id not in scenarios,
                    f"invalid or duplicate scenario ID: {legacy}/{public_id}")
            scenarios[public_id] = original_id
            original_scenarios.add(original_id)
        require(len(scenarios) * 8 == item["run_count"],
                f"scenario count mismatch: {legacy}")
        campaigns[legacy] = {"public_id": public, "run_count": item["run_count"],
                             "scenarios": scenarios}
        public_campaigns.add(public)
        scenario_count += len(scenarios)
    require(set(campaigns) == set(EXPECTED_RUN_COUNTS) and scenario_count == 17,
            "expected six campaigns and 17 scenarios in public ID map")
    return campaigns


def verify_sources(rows: list[dict[str, str]],
                   campaigns: dict[str, dict]) -> dict[str, dict[str, str]]:
    require(len(rows) == len(campaigns), "source crosswalk must contain six campaigns")
    mapping = {}
    for row in rows:
        campaign = row["campaign"]
        require(campaign in campaigns and campaign not in mapping,
                f"unexpected or duplicate source campaign: {campaign}")
        run_count = campaigns[campaign]["run_count"]
        require(int(row["raw_run_count"]) == run_count,
                f"run count mismatch: {campaign}")
        require(HEX40.fullmatch(row["original_implementation_sha"]) is not None,
                f"invalid original implementation SHA: {campaign}")
        config = path_for(row["public_config_path"])
        config_hash = digest(config)
        require(config_hash == row["public_config_sha256"] ==
                row["original_config_sha256"],
                f"frozen config hash mismatch: {campaign}")
        no_private_path(config.read_bytes(), str(config))
        snapshot_root = ROOT / row["snapshot_path"]
        manifest_path = path_for(str(Path(row["snapshot_path"]) / "source_manifest.json"))
        require(digest(manifest_path) == row["snapshot_manifest_sha256"],
                f"source manifest hash mismatch: {campaign}")
        source = json.loads(manifest_path.read_text(encoding="utf-8"))
        require(source["campaign"] == campaign and
                source["original_implementation_sha"] == row["original_implementation_sha"] and
                source["frozen_config_path"] == row["original_config_path"] and
                source["frozen_config_sha256"] == config_hash,
                f"source identity mismatch: {campaign}")
        files = source["files"]
        require(len(files) == int(row["snapshot_file_count"]),
                f"source file count mismatch: {campaign}")
        names = [item["path"] for item in files]
        require(names == sorted(set(names)), f"source file list unsorted/duplicated: {campaign}")
        for item in files:
            path = path_for(str(Path(row["snapshot_path"]) / item["path"]))
            require(digest(path) == item["sha256"] and path.stat().st_size == item["bytes"],
                    f"source file hash/size mismatch: {path}")
            no_private_path(path.read_bytes(), str(path))
        actual = {str(path.relative_to(snapshot_root)) for path in snapshot_root.rglob("*")
                  if path.is_file()}
        require(actual == set(names) | {"source_manifest.json"},
                f"unlisted source snapshot files: {campaign}")
        tree = "".join(f"{item['path']}\0{item['sha256']}\n" for item in files).encode()
        require(hashlib.sha256(tree).hexdigest() == source["tree_sha256"] ==
                row["snapshot_tree_sha256"],
                f"source tree digest mismatch: {campaign}")
        mapping[campaign] = row
    return mapping


def verify_raw(mapping: dict[str, dict[str, str]],
               transformations: list[dict[str, str]],
               campaigns: dict[str, dict]) -> tuple[int, int]:
    require(len(transformations) == 136, "expected 136 manifest transformations")
    transformed = {}
    for row in transformations:
        path = row["public_manifest_relative_path"]
        require(path not in transformed, f"duplicate transformation: {path}")
        require(row["campaign"] in campaigns and
                row["transformation"] ==
                "replace JSON environment.python_executable value only" and
                row["replacement"] == REDACTED and
                HEX64.fullmatch(row["original_manifest_sha256"]) is not None,
                f"invalid transformation receipt: {path}")
        transformed[path] = row
    actual_campaign_dirs = {path.name for path in (ROOT / "data/raw").iterdir()
                            if path.is_dir()}
    require(actual_campaign_dirs == {item["public_id"] for item in campaigns.values()},
            "public raw campaign directories do not match ID map")
    seen = set()
    output_files = 0
    for campaign, spec in campaigns.items():
        public_id = spec["public_id"]
        count = spec["run_count"]
        scenarios = spec["scenarios"]
        raw_root = ROOT / "data/raw" / public_id
        actual_scenario_dirs = {path.name for path in raw_root.iterdir() if path.is_dir()}
        require(actual_scenario_dirs == set(scenarios),
                f"public scenario directories do not match ID map: {public_id}")
        manifests = sorted(raw_root.glob("*/seed_*/run_manifest.json"))
        require(len(manifests) == count,
                f"manifest count mismatch for {campaign}: {len(manifests)}")
        scenario_seeds = defaultdict(set)
        crosswalk = mapping[campaign]
        for path in manifests:
            relative = str(path.relative_to(ROOT))
            require(relative in transformed and transformed[relative]["campaign"] == campaign,
                    f"manifest missing from transformation log: {relative}")
            receipt = transformed[relative]
            require(digest(path) == receipt["public_manifest_sha256"],
                    f"public manifest hash mismatch: {relative}")
            no_private_path(path.read_bytes(), relative)
            manifest = json.loads(path.read_text(encoding="utf-8"))
            require(manifest["environment"]["python_executable"] == REDACTED,
                    f"personal interpreter path was not redacted: {relative}")
            sha = manifest.get("git_sha", manifest.get("implementation_sha"))
            config_hash = manifest.get("config_file_sha256", manifest.get("config_sha256"))
            require(sha == crosswalk["original_implementation_sha"] and
                    config_hash == crosswalk["original_config_sha256"],
                    f"implementation/config identity mismatch: {relative}")
            seed = int(path.parent.name.removeprefix("seed_"))
            scenario = path.parent.parent.name
            expected_relative = (Path("data/raw") / public_id / scenario /
                                 f"seed_{seed}" / "run_manifest.json").as_posix()
            require(relative == expected_relative and scenario in scenarios,
                    f"public run path does not match ID map: {relative}")
            require(manifest["seed"] == seed and
                    manifest["scenario_id"] == scenarios[scenario],
                    f"seed/original-scenario identity mismatch: {relative}")
            scenario_seeds[scenario].add(seed)
            expected_files = set(manifest["output_sha256"])
            actual_files = {item.name for item in path.parent.iterdir() if item.is_file()}
            require(actual_files == expected_files | {"run_manifest.json"},
                    f"missing/unlisted output files: {relative}")
            for name, expected_hash in manifest["output_sha256"].items():
                artifact = path.parent / name
                require(digest(artifact) == expected_hash,
                        f"raw output hash mismatch: {artifact}")
                payload = artifact.read_bytes()
                if artifact.suffix == ".gz":
                    payload = gzip.decompress(payload)
                no_private_path(payload, str(artifact))
                output_files += 1
            seen.add(relative)
        require(set(scenario_seeds) == set(scenarios) and
                all(seeds == set(range(9101, 9109)) for seeds in scenario_seeds.values()),
                f"incomplete scenario/seed grid: {campaign}")
    require(seen == set(transformed), "orphan manifest transformation rows")
    require(len(seen) == 136 and output_files == 752,
            f"expected 136 manifests and 752 output files, got {len(seen)}/{output_files}")
    return len(seen), output_files


def main() -> None:
    crosswalk = table(ROOT / "provenance/source_sha_crosswalk.csv")
    transformations = table(ROOT / "provenance/raw_manifest_transformations.csv")
    campaigns = load_public_ids()
    mapping = verify_sources(crosswalk, campaigns)
    manifests, outputs = verify_raw(mapping, transformations, campaigns)
    print(f"PUBLIC-RAW-INTEGRITY-PASS {manifests} manifests "
          f"{outputs} output files {len(mapping)} source snapshots "
          f"{len(mapping)} frozen configs")


if __name__ == "__main__":
    main()
