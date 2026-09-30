#!/usr/bin/env python3
"""Prepare a verified, isolated SLS execution tree without running SLS.

The published source is identified by its original implementation SHA. The
new local Git commit only meets the frozen runner's clean-worktree contract.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
HISTORICAL_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SEEDS = {f"seed_{seed}" for seed in range(9101, 9109)}
GITIGNORE = "generated/\nresults/\n__pycache__/\n*.py[cod]\n"


@dataclass(frozen=True)
class PreparedSnapshot:
    campaign: str
    execution_dir: Path
    output_root: Path
    original_implementation_sha: str
    local_reproduction_sha: str


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_path(value: str) -> Path:
    path = Path(value)
    require(value != "" and path.parts and not path.is_absolute() and
            all(part not in {"", ".", ".."} for part in path.parts),
            f"unsafe relative path: {value}")
    return path


def ordinary_file(path: Path, label: str) -> None:
    require(path.is_file() and not path.is_symlink(), f"missing or linked {label}: {path}")


def directory_names(path: Path) -> set[str]:
    require(path.is_dir() and not path.is_symlink(), f"missing or linked directory: {path}")
    children = list(path.iterdir())
    require(all(item.is_dir() and not item.is_symlink() for item in children),
            f"unexpected file or linked child directory: {path}")
    return {item.name for item in children}


def load_public_map(root: Path) -> dict[str, dict]:
    path = root / "provenance/public_id_map.json"
    ordinary_file(path, "public ID map")
    document = json.loads(path.read_text(encoding="utf-8"))
    require(set(document) == {"campaigns"} and isinstance(document["campaigns"], list),
            "invalid public ID map structure")
    mapped: dict[str, dict] = {}
    historical: set[str] = set()
    scenario_total = 0
    for item in document["campaigns"]:
        require(set(item) == {"legacy_id", "public_id", "run_count", "scenarios"},
                "invalid campaign mapping")
        public = item["public_id"]
        legacy = item["legacy_id"]
        require(isinstance(public, str) and PUBLIC_ID.fullmatch(public) is not None and
                public not in mapped and isinstance(legacy, str) and
                HISTORICAL_ID.fullmatch(legacy) is not None and
                legacy not in historical,
                f"invalid or duplicate campaign: {public}")
        require(isinstance(item["scenarios"], list), f"invalid scenarios: {public}")
        scenarios: dict[str, str] = {}
        legacy_scenarios: set[str] = set()
        for scenario in item["scenarios"]:
            require(set(scenario) == {"legacy_id", "public_id"},
                    f"invalid scenario mapping: {public}")
            public_scenario = scenario["public_id"]
            legacy_scenario = scenario["legacy_id"]
            require(isinstance(public_scenario, str) and
                    PUBLIC_ID.fullmatch(public_scenario) is not None and
                    public_scenario not in scenarios and
                    isinstance(legacy_scenario, str) and
                    HISTORICAL_ID.fullmatch(legacy_scenario) is not None and
                    legacy_scenario not in legacy_scenarios,
                    f"invalid or duplicate scenario: {public}/{public_scenario}")
            scenarios[public_scenario] = legacy_scenario
            legacy_scenarios.add(legacy_scenario)
        require(item["run_count"] == 8 * len(scenarios),
                f"run count disagrees with mapped scenarios: {public}")
        mapped[public] = {"legacy_id": legacy, "run_count": item["run_count"],
                          "scenarios": scenarios}
        historical.add(legacy)
        scenario_total += len(scenarios)
    require(len(mapped) == 6 and scenario_total == 17 and
            sum(item["run_count"] for item in mapped.values()) == 136,
            "expected six campaigns, 17 scenarios, and 136 mapped runs")
    return mapped


def load_source_crosswalk(root: Path, mapped: dict[str, dict]) -> dict[str, dict[str, str]]:
    path = root / "provenance/source_sha_crosswalk.csv"
    ordinary_file(path, "source SHA crosswalk")
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    by_legacy = {item["legacy_id"]: public for public, item in mapped.items()}
    crosswalk: dict[str, dict[str, str]] = {}
    for row in rows:
        legacy = row["campaign"]
        require(legacy in by_legacy and legacy not in crosswalk,
                f"unexpected or duplicate source crosswalk campaign: {legacy}")
        public = by_legacy[legacy]
        require(row["snapshot_path"] == f"snapshots/{public}" and
                row["public_config_path"] == f"configs/{public}.yaml" and
                int(row["raw_run_count"]) == mapped[public]["run_count"] and
                HEX40.fullmatch(row["original_implementation_sha"]) is not None and
                all(HEX64.fullmatch(row[key]) is not None for key in (
                    "original_config_sha256", "public_config_sha256",
                    "snapshot_tree_sha256", "snapshot_manifest_sha256")),
                f"invalid source crosswalk: {legacy}")
        crosswalk[legacy] = row
    require(set(crosswalk) == set(by_legacy), "source crosswalk campaign set mismatch")
    return crosswalk


def verify_public_inventory(root: Path, mapped: dict[str, dict],
                            crosswalk: dict[str, dict[str, str]]) -> None:
    expected_campaigns = set(mapped)
    require(directory_names(root / "data/raw") == expected_campaigns,
            "raw campaign directories disagree with public ID map")
    require(directory_names(root / "snapshots") == expected_campaigns,
            "source snapshot directories disagree with public ID map")
    configs = root / "configs"
    require(configs.is_dir() and not configs.is_symlink() and
            {item.name for item in configs.iterdir() if item.is_file()} ==
            {f"{campaign}.yaml" for campaign in expected_campaigns},
            "public config files disagree with public ID map")
    for campaign, spec in mapped.items():
        raw_campaign = root / "data/raw" / campaign
        require(directory_names(raw_campaign) == set(spec["scenarios"]),
                f"raw scenario directories disagree with public ID map: {campaign}")
        original = crosswalk[spec["legacy_id"]]["original_implementation_sha"]
        config_hash = crosswalk[spec["legacy_id"]]["original_config_sha256"]
        for scenario, historical in spec["scenarios"].items():
            raw_scenario = raw_campaign / scenario
            require(directory_names(raw_scenario) == SEEDS,
                    f"raw seed directories disagree with public ID map: {campaign}/{scenario}")
            for seed_name in SEEDS:
                manifest_path = raw_scenario / seed_name / "run_manifest.json"
                ordinary_file(manifest_path, "raw run manifest")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                require(manifest["scenario_id"] == historical and
                        manifest["seed"] == int(seed_name.removeprefix("seed_")) and
                        manifest.get("git_sha", manifest.get("implementation_sha")) == original and
                        manifest.get("config_file_sha256", manifest.get("config_sha256")) == config_hash,
                        f"raw run identity mismatch: {manifest_path}")


def verify_source(root: Path, public: str, spec: dict,
                  crosswalk: dict[str, dict[str, str]]) -> dict:
    row = crosswalk[spec["legacy_id"]]
    source_root = root / "snapshots" / public
    require(source_root.is_dir() and not source_root.is_symlink(),
            f"invalid source snapshot directory: {public}")
    manifest_path = source_root / "source_manifest.json"
    ordinary_file(manifest_path, "source manifest")
    require(sha256(manifest_path) == row["snapshot_manifest_sha256"],
            f"source manifest hash mismatch: {public}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config_path = root / row["public_config_path"]
    ordinary_file(config_path, "public config")
    config_hash = sha256(config_path)
    require(config_hash == row["public_config_sha256"] == row["original_config_sha256"] and
            manifest["campaign"] == spec["legacy_id"] and
            manifest["original_implementation_sha"] == row["original_implementation_sha"] and
            manifest["frozen_config_path"] == row["original_config_path"] and
            manifest["frozen_config_sha256"] == config_hash,
            f"source/config identity mismatch: {public}")
    files = manifest["files"]
    require(isinstance(files, list) and len(files) == int(row["snapshot_file_count"]),
            f"source file count mismatch: {public}")
    names = [item["path"] for item in files]
    require(names == sorted(set(names)), f"source paths unsorted or duplicated: {public}")
    require(all(relative_path(name).parts[0] not in
                {".git", "generated", "results", "source_manifest.json", ".gitignore"}
                for name in names), f"reserved source path: {public}")
    for item in files:
        path = source_root / relative_path(item["path"])
        ordinary_file(path, "source file")
        require(HEX64.fullmatch(item["sha256"]) is not None and
                sha256(path) == item["sha256"] and path.stat().st_size == item["bytes"],
                f"source file hash/size mismatch: {path}")
    actual = {str(path.relative_to(source_root)) for path in source_root.rglob("*")
              if path.is_file() or path.is_symlink()}
    require(actual == set(names) | {"source_manifest.json"},
            f"source snapshot contains missing/unlisted files: {public}")
    tree = "".join(f"{item['path']}\0{item['sha256']}\n" for item in files).encode()
    require(hashlib.sha256(tree).hexdigest() == manifest["tree_sha256"] ==
            row["snapshot_tree_sha256"], f"source tree hash mismatch: {public}")
    frozen_config = source_root / relative_path(manifest["frozen_config_path"])
    require(sha256(frozen_config) == config_hash,
            f"frozen config file hash mismatch: {public}")
    return manifest


def allowed_execution_roots(root: Path) -> tuple[Path, Path]:
    roots = (root / "scratch/sls", root / "scratch/reproduction-validation")
    require(all(path.resolve() == path for path in roots),
            "execution root has a redirected path")
    return roots


def safe_paths(root: Path, campaign: str, destination: Path | None,
               output_root: Path | None) -> tuple[Path, Path]:
    allowed_roots = allowed_execution_roots(root)
    final = (root / destination if destination is not None and
             not destination.is_absolute() else destination)
    if final is None:
        final = allowed_roots[0] / campaign
    final = final.absolute()
    resolved = final.resolve()
    require(any(resolved.is_relative_to(allowed) and resolved != allowed
                for allowed in allowed_roots),
            f"unsafe execution directory: {final}")
    require(not final.exists() and not final.is_symlink(),
            f"execution directory already exists: {final}")
    final = resolved
    relative_output = output_root or Path("generated") / campaign
    require(not relative_output.is_absolute() and ".." not in relative_output.parts,
            f"unsafe output root: {relative_output}")
    output = (final / relative_output).resolve()
    generated = (final / "generated").resolve()
    require(output.is_relative_to(generated) and output != generated,
            f"unsafe output root: {relative_output}")
    forbidden = [root / path for path in ("data/raw", "data/accepted_analysis",
                                          "data/figure_inputs", "figures",
                                          "provenance", "snapshots")]
    require(all(not resolved.is_relative_to(path) and
                not output.is_relative_to(path) for path in forbidden),
            "execution/output path targets distributed artifacts")
    return final, output


def copy_verified_source(root: Path, public: str, manifest: dict,
                         stage: Path) -> None:
    source_root = root / "snapshots" / public
    for item in manifest["files"]:
        relative = relative_path(item["path"])
        source = source_root / relative
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        require(sha256(target) == item["sha256"] and
                target.stat().st_size == item["bytes"],
                f"staged source hash/size mismatch: {relative}")
    actual = {str(path.relative_to(stage)) for path in stage.rglob("*") if path.is_file()}
    require(actual == {item["path"] for item in manifest["files"]},
            "staged source file set mismatch")


def git_command(stage: Path, *arguments: str) -> str:
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(["git", "-C", str(stage), *arguments], check=True,
                            capture_output=True, text=True, env=environment)
    return result.stdout.strip()


def commit_reproduction_source(stage: Path, manifest: dict) -> str:
    (stage / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    git_command(stage, "-c", "init.defaultBranch=main", "init", "-q")
    git_command(stage, "add", "-A")
    git_command(stage, "-c", "user.name=Reproducer",
                "-c", "user.email=reproducer@example.invalid", "commit", "-qm",
                "Freeze published source snapshot")
    local_sha = git_command(stage, "rev-parse", "HEAD")
    require(HEX40.fullmatch(local_sha) is not None, "invalid local reproduction commit")
    tracked = set(git_command(stage, "ls-files").splitlines())
    require(tracked == {item["path"] for item in manifest["files"]} | {".gitignore"},
            "local commit tracked file set mismatch")
    require(git_command(stage, "status", "--porcelain") == "",
            "local reproduction commit has a dirty worktree")
    return local_sha


def create_raw_links(root: Path, stage: Path, mapped: dict[str, dict]) -> None:
    for public, spec in mapped.items():
        historical_root = stage / "results" / f"raw_{spec['legacy_id']}"
        historical_root.mkdir(parents=True, exist_ok=True)
        for scenario, historical in spec["scenarios"].items():
            target = root / "data/raw" / public / scenario
            require(target.is_dir() and target.resolve().is_relative_to(root / "data/raw"),
                    f"unsafe or missing published raw input: {target}")
            link = historical_root / historical
            link.symlink_to(target, target_is_directory=True)
            require(link.is_symlink() and link.resolve() == target and link.is_dir(),
                    f"historical raw link mismatch: {link}")
    require(git_command(stage, "status", "--porcelain") == "",
            "raw input links changed the local source worktree")


def rename_without_replace(source: Path, destination: Path) -> None:
    """Linux renameat2 provides an atomic move that cannot replace a target."""
    require(sys.platform.startswith("linux"), "atomic no-replace rename requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    require(function is not None, "atomic no-replace renameat2 unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    status = function(at_fdcwd, os.fsencode(source), at_fdcwd,
                      os.fsencode(destination), rename_noreplace)
    if status != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def prepare(campaign: str, *, root: Path = PACKAGE_ROOT,
            destination: Path | None = None,
            output_root: Path | None = None) -> PreparedSnapshot:
    root = root.resolve()
    mapped = load_public_map(root)
    require(campaign in mapped, f"unknown public campaign ID: {campaign}")
    crosswalk = load_source_crosswalk(root, mapped)
    final, output = safe_paths(root, campaign, destination, output_root)
    verify_public_inventory(root, mapped, crosswalk)
    manifests = {public: verify_source(root, public, spec, crosswalk)
                 for public, spec in mapped.items()}
    final.parent.mkdir(parents=True, exist_ok=True)
    require(any(final.parent.resolve().is_relative_to(allowed)
                for allowed in allowed_execution_roots(root)) and
            not final.exists() and not final.is_symlink(),
            f"unsafe or occupied execution directory: {final}")
    stage = Path(tempfile.mkdtemp(prefix=f".{campaign}.staging-", dir=final.parent))
    try:
        manifest = manifests[campaign]
        copy_verified_source(root, campaign, manifest, stage)
        local_sha = commit_reproduction_source(stage, manifest)
        create_raw_links(root, stage, mapped)
        staged_output = stage / output.relative_to(final)
        require(staged_output.resolve().is_relative_to(stage / "generated"),
                "staged output root is unsafe")
        require(not staged_output.exists() and not staged_output.is_symlink(),
                "staged output root already exists")
        require(not final.exists() and not final.is_symlink(),
                f"execution directory already exists: {final}")
        rename_without_replace(stage, final)
        stage = None
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
    return PreparedSnapshot(campaign, final, output,
                            manifest["original_implementation_sha"], local_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", help="public campaign ID from provenance/public_id_map.json")
    parser.add_argument("--destination", type=Path,
                        help="final directory under scratch/sls/ or scratch/reproduction-validation/ (default: scratch/sls/<campaign>)")
    parser.add_argument("--output-root", type=Path,
                        help="relative runner output under generated/ (default: generated/<campaign>)")
    args = parser.parse_args(argv)
    try:
        result = prepare(args.campaign, destination=args.destination,
                         output_root=args.output_root)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError,
            json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"PREPARE-SNAPSHOT-FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PREPARE-SNAPSHOT-PASS campaign={result.campaign}")
    print(f"execution_dir={result.execution_dir}")
    print(f"checked_output_root={result.output_root}")
    print(f"original_implementation_sha={result.original_implementation_sha}")
    print(f"local_reproduction_sha={result.local_reproduction_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
