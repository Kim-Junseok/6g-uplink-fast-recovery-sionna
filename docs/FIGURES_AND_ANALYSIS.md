# Figures and accepted-record analysis

Run the commands in this guide from the repository root. They read
distributed accepted records and write new files only below `scratch/`.
They do not run Sionna simulations.

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

The accepted timing contract sets 1 slot = 1 ms. Manuscript Figs. 3 and 5
display latency in ms. Archived source CSV columns retain their `_slots`
names, and the numeric coordinates are unchanged because the unit
conversion factor is one. This changes the displayed unit only; it does
not recalculate accepted results.

The manuscript legends use `CB FARQ` for the archived `Fast-CB` key and
`SB FARQ` for the archived `B` / `Fast-SB` key. The internal keys, source
CSV columns, and historical analysis metadata remain unchanged so that
the published run-to-figure links stay verifiable.

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

For simulation execution from the frozen source snapshots, see
[SLS reproduction](../SLS_REPRODUCTION.md). For validation evidence and
its limits, see the [validation report](../VALIDATION_REPORT.md).
