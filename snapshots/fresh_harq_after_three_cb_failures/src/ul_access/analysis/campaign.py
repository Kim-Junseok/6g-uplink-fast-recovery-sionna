"""Generic readers for immutable campaign artifact directories."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class RunArtifact:
    scenario: str
    scheme: str
    load: float
    seed: int
    directory: Path

    def manifest(self) -> dict:
        return json.loads((self.directory / "run_manifest.json").read_text())

    def summary(self) -> dict:
        return json.loads((self.directory / "summary.json").read_text())

    def rows(self, table: str) -> list[dict[str, str]]:
        if table not in {"packets", "attempts", "slot_prb",
                         "recovery_timing"}:
            raise ValueError(f"unsupported raw table: {table}")
        with gzip.open(self.directory / f"{table}.csv.gz", "rt",
                       encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def verify_hashes(self) -> bool:
        manifest = self.manifest()
        return all(expected == sha256(self.directory / name)
                   for name, expected in manifest["output_sha256"].items())


class CampaignDataset:
    """A scheme-agnostic collection of immutable run artifacts."""

    def __init__(self, runs: list[RunArtifact]):
        keys = {(run.scheme, run.load, run.seed) for run in runs}
        if len(keys) != len(runs):
            raise ValueError("campaign contains duplicate scheme/load/seed keys")
        self.runs = tuple(runs)

    def select(self, *, schemes=None, loads=None, seeds=None) -> list[RunArtifact]:
        scheme_set = None if schemes is None else set(schemes)
        load_set = None if loads is None else set(loads)
        seed_set = None if seeds is None else set(seeds)
        return [run for run in self.runs
                if (scheme_set is None or run.scheme in scheme_set)
                and (load_set is None or run.load in load_set)
                and (seed_set is None or run.seed in seed_set)]
