# Uplink Access and Recovery for 6G: Fast ARQ and HARQ-Preserving Scheduled Recovery

This repository provides the simulation source, 136 accepted run records,
analysis and figure-generation code, and the three result figures for the
manuscript named above. The manuscript source and full PDF are not included.
The distributed records support figure regeneration and independent numerical
reanalysis without rerunning Sionna.

## Manuscript figures

| Figure | Result file |
|---|---|
| Fig. 3 — recovery-domain tail latency | [PDF](figures/fig3a_recovery_domain_tail.pdf) |
| Fig. 4 — scheduled-recovery admission timing | [PDF](figures/fig4a_delay_scheduled_recovery_admission.pdf) |
| Fig. 5 — high-load packet delivery versus latency | [PDF](figures/fig5a_high_load_packet_delivery_vs_latency.pdf) |

The figure filenames retain historical `a` suffixes. The manuscript uses
these three figures only. [Figure inputs and analysis](docs/FIGURES_AND_ANALYSIS.md)
describes the accepted data path and the 1 ms per slot time base.

## Quick verification

From the repository root, use Python 3.12.3 to create a separate analysis
environment:

```bash
python3.12 -m venv .venv-analysis
. .venv-analysis/bin/activate
python -m pip install -r requirements-analysis-lock.txt
sha256sum -c SHA256SUMS
python scripts/release/verify_integrity.py
python scripts/release/rebuild_figures.py --output scratch/figures
```

The rebuilt PDF, SVG, and PNG figures appear in `scratch/figures/`. For raw
record reanalysis and manuscript evidence checks, follow
[Figures and accepted-record analysis](docs/FIGURES_AND_ANALYSIS.md).
For the separate Sionna environment and frozen campaign commands, follow
[SLS reproduction](SLS_REPRODUCTION.md). The
[campaign inventory](docs/CAMPAIGNS.md) maps public names to accepted runs.

## Validation scope

Distributed-file integrity, Fig. 3–5 regeneration, and reanalysis of all
136 recorded production runs passed. Representative seed-9101 Sionna runs for
CB FARQ (the internal `Fast-CB` policy), H(K=3), and F(K=3) also completed
and matched the selected published seed-level KPIs. The full 136-run
simulation campaign was not re-executed during release validation. [VALIDATION_REPORT.md](VALIDATION_REPORT.md)
records the checks and their limits.

## Sionna, citation, and reuse

The frozen simulation code uses the public [Sionna 2.0.1](https://github.com/NVlabs/sionna/tree/v2.0.1)
Python package, without a modified Sionna checkout. Please cite
Sionna (Hoydis et al., 2022, version 2.0.1) using its
[official citation entry](https://github.com/NVlabs/sionna/blob/v2.0.1/README.md#license-and-citation).
Sionna is licensed separately under
[Apache-2.0](https://github.com/NVlabs/sionna/blob/v2.0.1/LICENSE).

The selected terms are Apache-2.0 for original code and CC BY 4.0 for
distributed data and figures; see the project [LICENSE](LICENSE) and
[license scope](docs/LICENSE_SCOPE.md). Coauthor and institutional rights
confirmation remains open before remote publication. Sionna's license
does not set the terms for this repository's original work. The
[AI assistance statement](AI_ASSISTANCE.md) records the manuscript
acknowledgment wording.
