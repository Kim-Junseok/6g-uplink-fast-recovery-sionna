# Uplink access and recovery for 6G: reproduction files

The working manuscript title is *Uplink Access and Recovery for 6G: Fast ARQ
and HARQ-Preserving Scheduled Recovery*. This publication candidate contains
the simulation source, 136 accepted production runs, analysis and plotting
code, and the three PDF figures used as manuscript Figs. 3–5. It does not
contain the manuscript source or PDF. No Sionna run is needed to check the
supplied data or redraw the figures.

## What can be reproduced

| Level | Input → output | Validation in this candidate |
|---|---|---|
| 1. Distributed-file integrity | Packaged files → recorded hashes and provenance | PASS: `sha256sum -c SHA256SUMS` and `verify_integrity.py` |
| 2. Figure regeneration | Published figure-input CSVs → Figs. 3–5 | PASS: three generated source CSV hashes match and three PDFs were rebuilt; rendering bytes may differ |
| 3. Raw-record reanalysis | 136 recorded runs → accepted KPI tables, figure inputs, and manuscript evidence | PASS: 59 analysis CSVs, seven figure-input links, and the evidence map checked |
| 4. Representative SLS execution | Frozen source, configuration, and SLS environment → three new seed-9101 run records | PASS: Fast-CB, H(K=3), and F(K=3) completed; selected seed-level KPIs matched the accepted records |

All four release-validation levels passed. The analysis and figure-generation
workflow is reproducible from the distributed records. Representative SLS
paths reproduced their selected published seed-level KPIs. The complete
136-run campaign was not re-executed during release validation. Level 4 does
not establish cross-environment numerical equivalence or a new statistical
replication test.

The analysis below compares recomputed floating-point values with absolute
tolerance `1e-9` and relative tolerance `1e-12`. This is the contract for
*reanalyzing the same distributed raw records*. It is not a demonstrated
numerical-equivalence criterion for a new SLS run. The checks never widen a
tolerance to pass a mismatch.

## Repository layout

```text
figures/                 The three PDFs included in the manuscript
data/figure_inputs/      Accepted figure-input and numerical source CSVs
data/accepted_analysis/  Accepted KPI and paired-statistics tables
data/raw/                Packet, attempt, and resource records from 136 runs
scripts/paper_figures/    Figure-generation code
scripts/release/          Integrity, evidence, reanalysis, and source-preparation checks
experiments/ and src/     Raw-record KPI-analysis entry points and package scaffold
snapshots/                Frozen executable simulation source and internal configs
configs/                  Public copies of the six campaign configurations
provenance/               Public-to-original IDs, source mappings, and receipts
scratch/                  Local outputs from the commands below (Git-ignored)
```

The documented commands use published raw records and figure inputs only as
inputs. New files go under `scratch/`. The source manifest and
`provenance/public_id_map.json` identify each frozen source and its original
campaign and scenario names.

## Quick start: check and redraw the figures

From the repository root, create an analysis environment with Python 3.12.3:

```bash
python3.12 -m venv .venv-analysis
. .venv-analysis/bin/activate
python -m pip install -r requirements-analysis-lock.txt
sha256sum -c SHA256SUMS
python scripts/release/verify_integrity.py
python scripts/release/rebuild_figures.py --output scratch/figures
```

The rebuilt PDF, SVG, and 300-dpi PNG files appear under `scratch/figures/`.
The figure selector names retain historical suffixes:

| Manuscript figure | Included PDF | Selector |
|---|---|---|
| Fig. 3 | `figures/fig3a_recovery_domain_tail.pdf` | `fig3a` |
| Fig. 4 | `figures/fig4a_delay_scheduled_recovery_admission.pdf` | `fig4a` |
| Fig. 5 | `figures/fig5a_high_load_packet_delivery_vs_latency.pdf` | `fig5a` |

To regenerate only Fig. 4, use an empty output directory:

```bash
python scripts/release/rebuild_figures.py --only fig4a --output scratch/fig4a
```

The figure builder reads published `data/figure_inputs/`. It does not read
`scratch/reanalysis/` or overwrite the included PDFs. The generated numerical
source CSVs must match their accepted hashes. PDF rendering bytes need not
match across machines.

## Recalculate results from recorded runs

Use the analysis environment from the previous section, then run:

```bash
python scripts/release/reanalyze.py --output scratch/reanalysis
python scripts/release/check_evidence_map.py
python scripts/release/check_evidence_chain.py \
  --reanalysis scratch/reanalysis \
  --figures scratch/figures \
  --output scratch/evidence_chain_validation.json
```

`reanalyze.py` recomputes the campaign tables from `data/raw/` and compares
59 CSVs with `data/accepted_analysis/`. The evidence-chain check verifies the
six-campaign, 17-scenario, 136-run inventory; compares the seven figure-input
CSVs with their accepted-analysis counterparts; and checks the three generated
figure source CSVs and manuscript evidence, including Test D confidence
intervals, the seed-9104 counterexample, and prose values outside the figures.
`check_evidence_map.py` checks its indexed manuscript anchors and file
locators. Together, these commands connect the raw records to the accepted
numbers and figure inputs without changing the figure builder's input path.
Results and receipts stay under `scratch/`.

## Campaigns

All campaigns use seeds 9101–9108. The first five provide 128 main-result
runs. The last provides eight supporting-diagnostic runs.

| Public campaign ID | Runs | Comparison |
|---|---:|---|
| `high_load_baselines` | 32 | Legacy polling, immediate Fast-CB reinjection, and post-termination scheduled recovery at load ρ=0.90 |
| `scheduled_recovery_after_two_cb_failures` | 16 | HARQ-preserving and fresh-HARQ scheduled recovery after two contention-based (CB) failures at ρ=0.90 |
| `harq_continuation_after_three_cb_failures` | 8 | HARQ-preserving scheduled recovery after three CB failures at ρ=0.90 |
| `fresh_harq_after_three_cb_failures` | 8 | Fresh-HARQ scheduled recovery at the same trigger point |
| `lower_load_policy_comparison` | 64 | Eight recovery policies at ρ=0.70 |
| `fast_cb_reinjection_backoff` | 8 | Fast-CB with 0–3 additional eligible CB opportunities skipped before reinjection at ρ=0.90 |

HARQ-preserving scheduled recovery continues the active HARQ episode on a
scheduled resource. Fresh-HARQ scheduled recovery terminates the active
episode, discards its soft information, and starts a new scheduled HARQ episode
for the same modeled higher-layer payload. The public campaign IDs describe
the comparisons. Frozen configurations and manifests retain their original
internal IDs; `provenance/public_id_map.json` maps the two name sets.

## Reproduce the simulation runs

[SLS_REPRODUCTION.md](SLS_REPRODUCTION.md) gives the separate SLS environment,
all six preparation-and-run commands, outputs, and recorded-environment checks.
It uses public Sionna 2.0.1, with no local Sionna source checkout. For each
campaign, `prepare_snapshot.py` validates the frozen source and raw-input
links, then creates a clean local Git commit under
`scratch/sls/<public campaign ID>/`. It reports both
the **original implementation SHA** and the **new local reproduction commit
SHA**. The local commit satisfies the frozen runner's clean-worktree
requirement; it is not the commit used for the accepted production run. The
preparation script leaves your working directory unchanged.

The Level-4 check re-executed one rho=0.90, seed-9101 cell for each of Fast-CB,
H(K=3), and F(K=3) from the corresponding frozen snapshots. The three runs
completed and passed packet-outcome, HARQ, resource, drain, and provenance
checks. Their selected diagnostic KPIs matched the published seed-9101 values.
The check used an existing SLS environment separate from the analysis
environment; it did not test a fresh installation on another machine.

Documented SLS commands write only inside each prepared directory's
`generated/` tree. Historical symlinks to `data/raw/` provide baseline inputs;
they are not output locations or read-only filesystem protection. The
preparation script rejects destinations or output paths that resolve into
accepted data, figures, provenance, or source snapshots. Release validation
did not execute the full SLS campaign or establish a tolerance for comparing
newly simulated metrics with accepted metrics.

The final author list, DOI, and license remain open before public release. A
complete transitive SLS dependency lock and cross-environment SLS equivalence
criterion were not established; these are limits of the documented validation.
The manuscript's AI-use statement is reproduced in
[AI_ASSISTANCE.md](AI_ASSISTANCE.md).
