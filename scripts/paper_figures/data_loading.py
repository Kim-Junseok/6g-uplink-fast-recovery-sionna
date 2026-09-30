"""Read accepted artifacts; historical field identifiers remain unchanged."""
import csv, hashlib, json, math, statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V05 = ROOT / "data/figure_inputs/high_load_baselines"
V06_K3 = ROOT / "data/figure_inputs/harq_continuation_after_three_cb_failures"
V06_F3 = ROOT / "data/figure_inputs/fresh_harq_after_three_cb_failures"
V07 = ROOT / "data/figure_inputs/lower_load_policy_comparison"
DEFAULT_OUTPUT = ROOT / "figures/rebuilt"

V05_COMMIT = "0f166dab87c3c73f9f2289fae1e3190802376cb0"
V06_COMMIT = "d95059612ffbbf86d6c5e9e78f0b508fedb42079"
V07_IMPLEMENTATION = "39120e373e8e53e6153e5e8fa69bed510e5fc53c"
V07_RESULTS = "c90d1013a7e6e9192ea931bf66b3a6f6a25b1312"
V07_REPORT = "ca5107b371842ad87b84c91f1fe3e9f1214d6818"
SEEDS = tuple(range(9101, 9109))
GENERATION_COMMAND = (
    "python scripts/release/rebuild_figures.py"
)



def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def mean(rows: list[dict[str, str]], field: str) -> float:
    return statistics.fmean(float(row[field]) for row in rows)

def select(rows, key: str, value: str) -> list[dict[str, str]]:
    selected = [row for row in rows if row[key] == value]
    if len(selected) != len(SEEDS) or {int(row["seed"]) for row in selected} != set(SEEDS):
        raise RuntimeError(f"expected eight accepted seeds for {key}={value}")
    return selected

def assert_close(actual: float, expected: float, label: str,
                 tolerance: float = 1e-12) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance):
        raise RuntimeError(f"accepted-number mismatch for {label}: {actual} != {expected}")

def curve_map(rows, scheme_key, scheme, value_key):
    return {float(row["latency_slots"]): float(row[value_key])
            for row in rows if row[scheme_key] == scheme}

def verify_curve(left, right, label):
    common = sorted(set(left) & set(right))
    if not common:
        raise RuntimeError(f"no shared curve support for {label}")
    for x in common:
        assert_close(left[x], right[x], f"{label}@{x}")

def relative(path: Path) -> str:
    return str(path.relative_to(ROOT))

def sources(*paths: Path) -> list[dict[str, str]]:
    return [{"path": relative(path), "sha256": sha256(path)} for path in paths]

INPUTS = {"v05": V05/"run_level_kpis.csv", "v06": V06_F3/"run_level_kpis.csv", "v07": V07/"run_level_kpis.csv", "v05_curve": V05/"v0_5_1_completion_source.csv", "v06_curve": V06_F3/"completion_curve_source.csv", "v06_k3_curve": V06_K3/"completion_curve_source.csv", "v07_curve": V07/"completion_curve_source.csv"}
def load_data():
    data = {key: read_csv(path) for key, path in INPUTS.items()}
    verify_inputs(data)
    # Denominator: unique packets with at least one scheduled PUSCH transmission.
    for row in data["v06"]:
        assert_close(float(row["scheduled_prb_slots"])/int(row["rescued_packet_count"]), float(row["scheduled_attempts_per_rescued_packet"]), "scheduled-recovery packet denominator")
    return data

def verify_inputs(data) -> list[str]:
    v05_manifest = json.loads((V05 / "analysis_manifest.json").read_text())
    v06_manifest = json.loads((V06_F3 / "analysis_manifest.json").read_text())
    v07_manifest = json.loads((V07 / "analysis_manifest.json").read_text())
    if (v05_manifest["classification"] != "V0_5-REBASELINE-ANALYSIS-COMPLETE"
            or v05_manifest["rho"] != 0.9):
        raise RuntimeError("V0.5 accepted analysis provenance is missing")
    if (v06_manifest["execution_classification"] != "V0_6-F3-COMPLETE"
            or v06_manifest["scientific_classification"] !=
            "K3-HARQ-PRESERVATION-SUPPORTED"):
        raise RuntimeError("V0.6 accepted analysis provenance is missing")
    if (v07_manifest["classification"] != "V0_7-RHO070-ANALYSIS-COMPLETE"
            or v07_manifest["implementation_sha"] != V07_IMPLEMENTATION
            or v07_manifest["results_sha"] != V07_RESULTS):
        raise RuntimeError("V0.7 accepted analysis provenance is missing")
    for root, manifest, names in (
        (V06_F3, v06_manifest, ("run_level_kpis.csv", "completion_curve_source.csv")),
        (V07, v07_manifest, ("run_level_kpis.csv", "completion_curve_source.csv")),
    ):
        for name in names:
            if sha256(root / name) != manifest["artifact_sha256"][name]:
                raise RuntimeError(f"accepted artifact hash mismatch: {root/name}")

    v05_rows, v06_rows, v07_rows = data["v05"], data["v06"], data["v07"]
    integrated = {(row["scheme"], row["seed"]): row for row in v07_rows
                  if row["rho"] == "0.90"}
    v05_mapping = {"Legacy-20": "Legacy-20", "Fast-CB": "Fast-CB", "Fast-SB": "B"}
    fields05 = {
        "correct_delivery_probability": "correct_delivery_probability",
        "protocol_terminal_failure_probability": "terminal_failure_probability",
        "p99_correct_latency_slots": "p99_latency",
        "scheduled_attempts_per_slot": "scheduled_attempts_per_slot",
        "mean_scheduled_queue_length": "mean_queue",
        "mean_scheduled_queue_wait_slots": "mean_wait",
    }
    for old, new in v05_mapping.items():
        for row in select(v05_rows, "policy", old):
            combined = integrated[(new, row["seed"])]
            for historical_field, integrated_field in fields05.items():
                assert_close(float(row[historical_field]),
                             float(combined[integrated_field]),
                             f"V0.5/V0.7 {old}/{row['seed']}/{historical_field}")
            assert_close(float(row["rlc_retransmissions"])/float(row["measured_packets"]),
                         float(combined["rlc_retransmissions_per_packet"]),
                         f"V0.5/V0.7 {old}/{row['seed']}/rlc")
    fields06 = (
        "correct_delivery_probability", "terminal_failure_probability",
        "p99_latency", "scheduled_attempts_per_packet", "mean_queue",
        "mean_wait", "first_scheduled_success_probability",
        "scheduled_attempts_per_rescued_packet",
    )
    for scheme in ("H2", "F2", "H3", "F3"):
        for row in select(v06_rows, "scheme", scheme):
            combined = integrated[(scheme, row["seed"])]
            for field in fields06:
                assert_close(float(row[field]), float(combined[field]),
                             f"V0.6/V0.7 {scheme}/{row['seed']}/{field}")

    v05_curve, v06_curve = data["v05_curve"], data["v06_curve"]
    v06_k3_curve, v07_curve = data["v06_k3_curve"], data["v07_curve"]
    verify_curve(
        curve_map(v05_curve, "policy", "Fast-SB", "seed_mean"),
        curve_map(v06_curve, "scheme", "B", "mean"),
        "V0.5 Fast-SB / V0.6 B completion",
    )
    verify_curve(
        curve_map(v06_k3_curve, "scheme", "H3", "mean"),
        curve_map(v06_curve, "scheme", "H3", "mean"),
        "V0.6 K3/F3 H3 completion",
    )
    for scheme, historical in (
        ("Legacy-20", curve_map(v05_curve, "policy", "Legacy-20", "seed_mean")),
        ("B", curve_map(v06_curve, "scheme", "B", "mean")),
        ("H3", curve_map(v06_curve, "scheme", "H3", "mean")),
        ("F3", curve_map(v06_curve, "scheme", "F3", "mean")),
    ):
        verify_curve(historical,
                     {float(row["latency_slots"]): float(row["mean"])
                      for row in v07_curve if row["rho"] == "0.90"
                      and row["scheme"] == scheme},
                     f"historical/V0.7 {scheme} completion")

    # Values explicitly accepted in the milestone reports.
    v05_fast = select(v05_rows, "policy", "Fast-CB")
    assert_close(mean(v05_fast, "correct_delivery_probability"), 0.7325,
                 "V0.5 Fast-CB correct delivery")
    assert_close(mean(v05_fast, "protocol_terminal_failure_probability"), 0.2672,
                 "V0.5 Fast-CB terminal failure")
    assert_close(statistics.fmean(float(row["rlc_retransmissions"])/2500
                                  for row in v05_fast), 1.77595,
                 "V0.5 Fast-CB RLC retransmissions")
    assert_close(mean(select(v06_rows, "scheme", "H3"), "p99_latency"),
                 201.6375, "V0.6 H3 P99")
    assert_close(mean(select(v06_rows, "scheme", "F3"), "p99_latency"),
                 427.025, "V0.6 F3 P99")
    v07_070 = [row for row in v07_rows if row["rho"] == "0.70"]
    assert_close(mean(select(v07_070, "scheme", "Fast-CB"),
                      "correct_delivery_probability"), 0.99995,
                 "V0.7 Fast-CB correct delivery")
    assert_close(mean(select(v07_070, "scheme", "Fast-CB"),
                      "terminal_failure_probability"), 0.0,
                 "V0.7 Fast-CB terminal failure")
    return [
        "accepted manifest classifications",
        "accepted artifact hashes",
        "400 historical-to-integrated run metrics",
        "historical completion-curve identity",
        "accepted report anchor values",
    ]
