#!/usr/bin/env python3
"""Check the figure and numerical evidence map against packaged inputs."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, ROUND_HALF_UP
import glob
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def close(actual: float, expected: float, label: str, tolerance: float = 1e-9) -> None:
    if abs(actual - expected) > tolerance:
        raise RuntimeError(f'{label}: {actual} != {expected}')


def format_value(value: str | float | Decimal, places: int) -> str:
    number = Decimal(str(value))
    quantum = Decimal(1).scaleb(-places)
    return f"{number.quantize(quantum, rounding=ROUND_HALF_UP):.{places}f}"


def require_printed(claims: dict[str, dict[str, str]], claim_id: str, expected: str) -> None:
    actual = claims[claim_id]["expected_printed_value"]
    if actual != expected:
        raise RuntimeError(f"{claim_id}: manuscript-map value {actual!r} != source-derived {expected!r}")


def check_test_d_claims(claims: dict[str, dict[str, str]], accepted: Path) -> set[str]:
    """Check printed paired estimates, their intervals, and the adverse seed."""
    paired = {r["metric"]: r for r in rows(accepted / "fast_cb_reinjection_backoff/paired_statistics.csv")}
    def printed(metric: str, label: str, difference_sign: bool = False) -> str:
        row = paired[metric]
        difference = format_value(row["paired_mean_difference"], 4)
        if difference_sign and not difference.startswith("-"):
            difference = "+" + difference
        return (
            f"{label} {format_value(row['reference_mean'], 4)} to "
            f"{format_value(row['backoff_mean'], 4)}; paired {difference}; "
            f"95% CI [{format_value(row['ci95_lower'], 4)},"
            f"{format_value(row['ci95_upper'], 4)}]"
        )
    require_printed(
        claims, "R11",
        printed("cb_attempts_per_exogenous_packet", "CB attempts/packet"),
    )
    require_printed(
        claims, "R12",
        printed("packet_delivery_probability", "delivery", True),
    )
    delivery = paired["packet_delivery_probability"]
    if not (float(delivery["ci95_lower"]) < 0 < float(delivery["ci95_upper"])):
        raise RuntimeError("R12: delivery interval no longer includes zero")
    for metric in (
        "rlc_retransmissions_per_exogenous_packet",
        "cb_all_attempts_per_injection_slot",
        "packets_unresolved_at_injection_end",
    ):
        row = paired[metric]
        if not (float(row["paired_mean_difference"]) < 0 and float(row["ci95_upper"]) < 0):
            raise RuntimeError(f"R13: workload/backlog interval does not lie below zero: {metric}")
    require_printed(
        claims, "R13",
        "RLC retransmissions; injection-window CB rate; injection-end backlog decrease with intervals below zero",
    )
    differences: dict[int, dict[str, float]] = {}
    for row in rows(accepted / "fast_cb_reinjection_backoff/paired_seed_differences.csv"):
        differences.setdefault(int(row["seed"]), {})[row["metric"]] = float(
            row["backoff_minus_reference"]
        )
    if set(differences) != set(range(9101, 9109)):
        raise RuntimeError("R14: paired seed set changed")
    adverse = {
        seed for seed, values in differences.items()
        if values["packet_delivery_probability"] < 0
        and values["rlc_retransmissions_per_exogenous_packet"] > 0
        and values["cb_attempts_per_exogenous_packet"] > 0
        and values["p99_packet_latency_conditioned_on_delivery_slots"] > 0
    }
    if adverse != {9104}:
        raise RuntimeError(f"R14: expected the seed-9104 delivery/work/P99 counterexample; got {adverse}")
    require_printed(claims, "R14", "one of eight seeds worsens in delivery, work and P99")
    return {"R11", "R12", "R13", "R14"}


def check_numerical_claims(claims: list[dict[str, str]]) -> tuple[int, int]:
    """Recompute all mapped figure-data and numerical prose claims from accepted CSVs.

    Reanalysis independently checks these accepted CSVs against the 136 raw runs.
    Methodological claims are locator/provenance checks, not numerical checks.
    """
    by_id = {row["claim_id"]: row for row in claims}
    accepted = ROOT / "data/accepted_analysis"
    figroot = ROOT / "data/figure_inputs/accepted_figure_sources"
    checked: set[str] = set()
    def claim(claim_id: str, expected: str) -> None:
        require_printed(by_id, claim_id, expected)
        checked.add(claim_id)

    fig3_rows = rows(figroot / "fig3a_recovery_domain_tail_source.csv")
    fig3 = {(r["rho"], r["scheme"]): r["conditional_p99_slots"] for r in fig3_rows}
    v05_run = rows(accepted / "high_load_baselines/run_level_kpis.csv")
    v07_run = rows(accepted / "lower_load_policy_comparison/run_level_kpis.csv")
    if set(fig3) != {
        (rho, scheme) for rho in ("0.70", "0.90")
        for scheme in ("Legacy-20", "B", "Fast-CB")
    }:
        raise RuntimeError("Fig. 3 source point set changed")
    for (rho, scheme), source in fig3.items():
        if rho == "0.70":
            values = [float(r["p99_latency"]) for r in v07_run
                      if r["rho"] == rho and r["scheme"] == scheme]
        else:
            policy = "Fast-SB" if scheme == "B" else scheme
            values = [float(r["p99_correct_latency_slots"]) for r in v05_run
                      if r["policy"] == policy]
        if len(values) != 8:
            raise RuntimeError(f"Fig. 3: expected eight P99 seed values for {rho}/{scheme}")
        close(float(source), statistics.fmean(values), f"Fig. 3 {rho}/{scheme}")
    reduction = lambda rho: 100 * (
        Decimal(fig3[rho, "Legacy-20"]) - Decimal(fig3[rho, "B"])
    ) / Decimal(fig3[rho, "Legacy-20"])
    claim(
        "R01",
        f"{format_value(reduction('0.70'), 1)}% at rho 0.70; "
        f"{format_value(reduction('0.90'), 1)}% at rho 0.90",
    )
    for claim_id, scheme in (("R02", "Legacy-20"), ("R03", "Fast-CB"), ("R04", "B")):
        claim(
            claim_id,
            f"{scheme} P99 {format_value(fig3['0.70', scheme], 2)} at rho 0.70; "
            f"{format_value(fig3['0.90', scheme], 2)} at rho 0.90",
        )

    high = {r["policy"]: r for r in rows(accepted / "high_load_baselines/policy_seed_means.csv")}
    low = {
        (r["rho"], r["scheme"], r["metric"]): r["mean"]
        for r in rows(accepted / "lower_load_policy_comparison/scheme_summary.csv")
    }
    high_fast = high["Fast-CB"]
    claim("R05", f"P_del = {format_value(high_fast['correct_delivery_probability'], 2)}")
    claim(
        "R06",
        f"Fast-CB delivery {low['0.70', 'Fast-CB', 'correct_delivery_probability']} "
        f"at rho 0.70; {high_fast['correct_delivery_probability']} at rho 0.90",
    )
    claim(
        "R07",
        "Fast-CB terminal failure "
        f"{format_value(high_fast['protocol_terminal_failure_probability'], 4)} at rho 0.90",
    )
    high_retx_per_packet = (
        Decimal(high_fast["rlc_retransmissions"]) / Decimal(high_fast["measured_packets"])
    )
    claim(
        "R08",
        "Fast-CB RLC retransmissions/packet "
        f"{format_value(low['0.70', 'Fast-CB', 'rlc_retransmissions_per_packet'], 4)} "
        f"at rho 0.70; {format_value(high_retx_per_packet, 3)} at rho 0.90",
    )
    if not (
        float(low["0.70", "Fast-CB", "correct_delivery_probability"]) >= 0.9999
        and float(low["0.70", "B", "correct_delivery_probability"]) >= 0.9999
        and float(high["Fast-SB"]["correct_delivery_probability"]) >= 0.9999
    ):
        raise RuntimeError("R09: near-unity delivery description is unsupported")
    claim(
        "R09",
        "Fast-CB/B near-unity delivery at rho 0.70 and B near-unity at rho 0.90",
    )
    checked.update(check_test_d_claims(by_id, accepted))

    fig4_rows = rows(figroot / "fig4a_delay_scheduled_recovery_admission_source.csv")
    fig4 = {r["metric"]: r for r in fig4_rows}
    f3_run = rows(accepted / "fresh_harq_after_three_cb_failures/run_level_kpis.csv")
    if set(fig4) != {
        "scheduled_attempts_per_packet", "mean_queue", "mean_wait", "p99_latency"
    }:
        raise RuntimeError("Fig. 4 metric set changed")
    descriptions = {
        "scheduled_attempts_per_packet": ("R15", "scheduled-attempt ratio"),
        "mean_queue": ("R16", "mean-queue ratio"),
        "mean_wait": ("R17", "mean-wait ratio"),
        "p99_latency": ("R18", "P99 ratio"),
    }
    for metric, row in fig4.items():
        for family in ("H", "F"):
            for k in (2, 3):
                values = [float(r[metric]) for r in f3_run if r["scheme"] == f"{family}{k}"]
                if len(values) != 8:
                    raise RuntimeError(f"Fig. 4 expected eight seed rows for {family}{k}")
                close(float(row[f"{family}{k}_mean"]), statistics.fmean(values),
                      f"Fig. 4 {metric} {family}{k}")
            ratio = float(row[f"{family}3_mean"]) / float(row[f"{family}2_mean"])
            close(float(row[f"{family}3_over_{family}2"]), ratio,
                  f"Fig. 4 {metric} {family} ratio")
            if ratio >= 1:
                raise RuntimeError(f"R20: expected K3/K2 below one for {family}/{metric}")
        claim_id, label = descriptions[metric]
        claim(
            claim_id,
            f"{label} H3/H2 {format_value(row['H3_over_H2'], 6)}; "
            f"F3/F2 {format_value(row['F3_over_F2'], 6)}",
        )
    claim("R20", "K3 lowers scheduled demand; queue; wait and P99 under H and F at rho 0.90")
    for family in ("H", "F"):
        for metric in ("scheduled_attempts_per_packet", "mean_queue", "mean_wait", "p99_latency"):
            if float(low["0.70", f"{family}3", metric]) >= float(low["0.70", f"{family}2", metric]):
                raise RuntimeError(f"R21: K3 did not lower {metric} for {family} at rho 0.70")
    claim("R21", "same K3 directional effect at lower load under H and F")

    f3 = {
        r["scheme"]: r
        for r in rows(accepted / "fresh_harq_after_three_cb_failures/scheme_seed_means.csv")
    }
    paired_f3 = {
        (r["comparison"], r["metric"]): r
        for r in rows(accepted / "fresh_harq_after_three_cb_failures/paired_statistics.csv")
    }
    claim(
        "R19",
        f"H2 P99 {format_value(f3['H2']['p99_latency'], 2)} to "
        f"H3 P99 {format_value(f3['H3']['p99_latency'], 2)} slots",
    )
    claim(
        "R22",
        "first scheduled-attempt ACK "
        f"F3 {format_value(100 * Decimal(f3['F3']['first_scheduled_success_probability']), 2)}% "
        f"to H3 {format_value(100 * Decimal(f3['H3']['first_scheduled_success_probability']), 2)}%",
    )
    service = paired_f3["H3-F3", "scheduled_attempts_per_rescued_packet"]
    claim(
        "R23",
        "scheduled attempts/scheduled-recovery packet "
        f"F3 {format_value(f3['F3']['scheduled_attempts_per_rescued_packet'], 4)} "
        f"to H3 {format_value(f3['H3']['scheduled_attempts_per_rescued_packet'], 4)}; "
        f"paired {format_value(service['mean_difference'], 5)}; "
        f"CI [{format_value(service['ci95_low'], 5)},"
        f"{format_value(service['ci95_high'], 5)}]",
    )
    for metric in ("mean_queue", "p99_latency"):
        if float(f3["H3"][metric]) >= float(f3["F3"][metric]):
            raise RuntimeError(f"R24: high-load H3 is not below F3 for {metric}")
        if float(low["0.70", "H3", metric]) >= float(low["0.70", "F3", metric]):
            raise RuntimeError(f"R24: low-load H3 is not below F3 for {metric}")
    claim("R24", "H3 lower queueing and P99 than F3 at both loads")
    claim(
        "R25",
        "high-load conditional P99 "
        f"B {format_value(f3['B']['p99_latency'], 2)}; "
        f"H3 {format_value(f3['H3']['p99_latency'], 2)}; "
        f"F3 {format_value(f3['F3']['p99_latency'], 2)} slots",
    )
    claim(
        "R26",
        "low-load "
        f"H3 P99 {format_value(low['0.70', 'H3', 'p99_latency'], 2)}; "
        f"F3 P99 {format_value(low['0.70', 'F3', 'p99_latency'], 2)} slots",
    )

    fig5_rows = rows(figroot / "fig5a_high_load_packet_delivery_vs_latency_source.csv")
    legacy_curve = {
        (r["policy"], Decimal(r["latency_slots"])): r["seed_mean"]
        for r in rows(accepted / "high_load_baselines/v0_5_1_completion_source.csv")
    }
    scheduled_curve = {
        (r["scheme"], Decimal(r["latency_slots"])): r["mean"]
        for r in rows(accepted / "fresh_harq_after_three_cb_failures/completion_curve_source.csv")
    }
    counts: dict[str, int] = {}
    curves: dict[str, list[tuple[float, float]]] = {}
    for row in fig5_rows:
        scheme, latency = row["scheme"], Decimal(row["latency_slots"])
        origin = legacy_curve if scheme == "Legacy-20" else scheduled_curve
        if (scheme, latency) not in origin:
            raise RuntimeError(f"Fig. 5 source has an unmatched point: {scheme}/{latency}")
        close(float(row["packet_delivery_probability"]), float(origin[scheme, latency]),
              f"Fig. 5 point {scheme}/{latency}")
        counts[scheme] = counts.get(scheme, 0) + 1
        curves.setdefault(scheme, []).append(
            (float(latency), float(row["packet_delivery_probability"]))
        )
    if counts != {"Legacy-20": 638, "B": 763, "H3": 763, "F3": 763}:
        raise RuntimeError(f"Fig. 5 curve point counts changed: {counts}")
    for claim_id, label in (
        ("R27", "Legacy-20"), ("R28", "B"), ("R29", "H3"), ("R30", "F3")
    ):
        claim(claim_id, f"{label} delivery-versus-latency curve")
    metadata = json.loads(
        (figroot / "fig5a_high_load_packet_delivery_vs_latency_metadata.json").read_text(
            encoding="utf-8"
        )
    )
    if "truncated at 0.55" not in metadata.get("caveat", ""):
        raise RuntimeError("R31: displayed probability axis lower bound changed")
    fig5_raw_scenarios = {
        "Legacy-20": ("high_load_baselines", "legacy_poll_20_slots"),
        "B": ("high_load_baselines", "post_termination_scheduled"),
        "H3": ("harq_continuation_after_three_cb_failures", "harq_preserving"),
        "F3": ("fresh_harq_after_three_cb_failures", "fresh_harq"),
    }
    for scheme, (campaign, scenario) in fig5_raw_scenarios.items():
        raw_scenario = ROOT / "data/raw" / campaign / scenario
        for seed in range(9101, 9109):
            run = raw_scenario / f"seed_{seed}"
            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
            packets = summary["packets"]
            stopping = manifest["resolved_configuration"]["common_contract"]["stopping"]
            if (packets["measured"] != 2500 or packets["warmup"] != 500
                    or packets["generated_total"] != 3000
                    or stopping["measured_new_packets"] != 2500
                    or stopping["warmup_new_packets"] != 500):
                raise RuntimeError(f"R31: measured-packet cohort changed for {scheme}/seed_{seed}")
    claim("R31", "2500 measured packets per seed; probability axis starts at 0.55")
    def crossing(scheme: str, threshold: float) -> float:
        return next(x for x, y in curves[scheme] if y >= threshold)
    for threshold in (0.95, 0.99):
        if not (
            crossing("B", threshold) < crossing("F3", threshold)
            and crossing("H3", threshold) < crossing("F3", threshold)
        ):
            raise RuntimeError("R32: accepted high-load curve tail ordering changed")
    claim("R32", "B and H3 shorter displayed tails than F3 at rho 0.90")
    expected_checked = {f"R{i:02d}" for i in range(1, 33)} - {"R10"}
    if checked != expected_checked:
        raise RuntimeError(f"numerical claim coverage changed: {checked ^ expected_checked}")
    return len(checked), len(fig5_rows)



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=Path('scratch/evidence_map_validation.json'))
    args = parser.parse_args()
    map_path = ROOT / 'provenance/manuscript_evidence_map.csv'
    claims = rows(map_path)
    if len(claims) != 52 or len({c['claim_id'] for c in claims}) != 52:
        raise RuntimeError('evidence-map row count or claim IDs changed')
    columns = {'claim_id', 'manuscript_locator',
               'claim_type', 'expected_printed_value', 'metric_or_rule',
               'condition', 'denominator_and_aggregation', 'accepted_source',
               'raw_group', 'analysis_code', 'verification'}
    if set(claims[0]) != columns:
        raise RuntimeError('evidence-map schema changed')
    resolved = 0
    for claim in claims:
        for column in ('accepted_source', 'raw_group', 'analysis_code'):
            for item in claim[column].split('; '):
                if item.startswith(('data/', 'configs/', 'snapshots/', 'docs/',
                                    'figures/', 'provenance/', 'scripts/', 'experiments/')):
                    if not glob.glob(str(ROOT / item)):
                        raise RuntimeError(f"missing {column} locator in {claim['claim_id']}: {item}")
                    resolved += 1
    figure_files = {
        'F03': 'fig3a_recovery_domain_tail.pdf',
        'F04': 'fig4a_delay_scheduled_recovery_admission.pdf',
        'F05': 'fig5a_high_load_packet_delivery_vs_latency.pdf',
    }
    figure_rows = {claim['claim_id']: claim for claim in claims
                   if claim['claim_type'] == 'FIGURE_FILE'}
    if set(figure_rows) != set(figure_files):
        raise RuntimeError('evidence-map figure-file rows changed')
    for claim_id, filename in figure_files.items():
        claim = figure_rows[claim_id]
        if claim['expected_printed_value'] != filename:
            raise RuntimeError(f'{claim_id}: figure filename changed')
        if f'figures/{filename}' not in claim['accepted_source'].split('; '):
            raise RuntimeError(f'{claim_id}: figure-file locator missing')
        if not (ROOT / 'figures' / filename).is_file():
            raise RuntimeError(f'{claim_id}: figure PDF missing')

    fig3 = rows(ROOT / 'data/figure_inputs/accepted_figure_sources/fig3a_recovery_domain_tail_source.csv')
    fig3_value = {(r['rho'], r['scheme']): float(r['conditional_p99_slots']) for r in fig3}
    if len(fig3_value) != 6:
        raise RuntimeError('Fig. 3 must have six conditional-P99 values')
    for load, legacy, b, fast in [
        ('0.70', 28.62625, 10.25, 11.37625),
        ('0.90', 288.82125, 213.39625, 49.4875),
    ]:
        close(fig3_value[load, 'Legacy-20'], legacy, f'Fig. 3 Legacy {load}')
        close(fig3_value[load, 'B'], b, f'Fig. 3 B {load}')
        close(fig3_value[load, 'Fast-CB'], fast, f'Fig. 3 Fast-CB {load}')
    close(100 * (fig3_value['0.70', 'Legacy-20'] - fig3_value['0.70', 'B']) /
          fig3_value['0.70', 'Legacy-20'], 64.2, 'Abstract low-load reduction', 0.05)
    close(100 * (fig3_value['0.90', 'Legacy-20'] - fig3_value['0.90', 'B']) /
          fig3_value['0.90', 'Legacy-20'], 26.1, 'Abstract high-load reduction', 0.05)

    fig4 = rows(ROOT / 'data/figure_inputs/accepted_figure_sources/fig4a_delay_scheduled_recovery_admission_source.csv')
    expected_ratios = {
        'scheduled_attempts_per_packet': (0.8186451672690205, 0.8133470830878021),
        'mean_queue': (0.42381694632294753, 0.583107936568573),
        'mean_wait': (0.42675729300961307, 0.5847460702020993),
        'p99_latency': (0.5161804375581991, 0.6129835081920743),
    }
    if len(fig4) != 4:
        raise RuntimeError('Fig. 4 must have four ratio categories')
    for row in fig4:
        a, b = expected_ratios[row['metric']]
        close(float(row['H3_over_H2']), a, 'Fig. 4 H ratio')
        close(float(row['F3_over_F2']), b, 'Fig. 4 F ratio')

    f3 = rows(ROOT / 'data/accepted_analysis/fresh_harq_after_three_cb_failures/run_level_kpis.csv')
    first = {scheme: statistics.fmean(float(r['first_scheduled_success_probability'])
                                     for r in f3 if r['scheme'] == scheme)
             for scheme in ('H3', 'F3')}
    close(first['H3'], 0.908195553997645, 'H3 first scheduled ACK')
    close(first['F3'], 0.5007070120856696, 'F3 first scheduled ACK')
    test_d = {r['metric']: r for r in rows(ROOT / 'data/accepted_analysis/fast_cb_reinjection_backoff/paired_statistics.csv')}
    close(float(test_d['packet_delivery_probability']['reference_mean']), 0.7325, 'Test D reference delivery')
    close(float(test_d['packet_delivery_probability']['backoff_mean']), 0.8839, 'Test D backoff delivery')
    close(float(test_d['cb_attempts_per_exogenous_packet']['paired_mean_difference']), -4.2156, 'Test D CB-work difference')
    if not (float(test_d['packet_delivery_probability']['ci95_lower']) < 0 <
            float(test_d['packet_delivery_probability']['ci95_upper'])):
        raise RuntimeError('Test D delivery interval no longer crosses zero')

    fig5 = rows(ROOT / 'data/figure_inputs/accepted_figure_sources/fig5a_high_load_packet_delivery_vs_latency_source.csv')
    if {r['scheme'] for r in fig5} != {'Legacy-20', 'B', 'H3', 'F3'}:
        raise RuntimeError('Fig. 5 curve scheme set changed')
    if not all(0 <= float(r['packet_delivery_probability']) <= 1 for r in fig5):
        raise RuntimeError('Fig. 5 probability range changed')

    numerical_claims, figure_curve_points = check_numerical_claims(claims)

    receipt = {
        'classification': 'EVIDENCE-MAP-PASS',
        'evidence_map_sha256': sha256(map_path),
        'public_id_map_sha256': sha256(ROOT / 'provenance/public_id_map.json'),
        'claim_rows': len(claims),
        'resolved_source_locators': resolved,
        'figure_files_checked': len(figure_files),
        'numeric_sentinel_groups_checked': 5,
        'numerical_prose_claims_checked': numerical_claims,
        'figure_curve_points_checked': figure_curve_points,
        'scope': 'Mapped numerical prose and figure-data claims checked from accepted CSVs; method claims use provenance locators. Full raw-record CSV reproduction is checked by reanalyze.py.',
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f"EVIDENCE-MAP-PASS rows={len(claims)} locators={resolved} receipt={output}")


if __name__ == '__main__':
    main()
