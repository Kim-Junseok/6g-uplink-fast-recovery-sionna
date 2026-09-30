# Local publication-candidate validation

Profile: README_DOC

The candidate was validated on 2026-09-29 from a copy at a different
filesystem path, using a fresh Python 3.12.3 environment installed from
requirements-analysis-lock.txt. The private research repository remained
unchanged. The candidate contains three standalone figure PDFs and does not
contain manuscript source or the full manuscript PDF.

| Check | Result |
|---|---|
| Package file integrity | SHA256SUMS checks distributed files, including raw records and source snapshots |
| Accepted-file integrity | PASS: 136 run manifests, 752 run outputs, six source snapshots, six frozen configurations |
| Paper evidence map | PASS: 52 claims and 240 resolved source locators; selected numeric anchors checked |
| Raw-record reanalysis | PASS: 136 runs, 59 accepted CSVs, maximum absolute numeric difference 0 |
| Fig. 3–5 regeneration | PASS: three source CSVs match byte-for-byte; three PDFs generated |
| Figure tests | PASS: exact three-figure set and independent selection |
| Path and token scan | No personal path or obvious token marker in distributed records; the verifier contains one intentional generic path-detection pattern |

On 2026-09-30, the candidate adopted descriptive public campaign and scenario
paths. A before/after SHA-256 comparison found the contents of all 1,359
protected raw, accepted-analysis, frozen source/configuration, and supplied
figure files unchanged. The current paths again passed the 136-run integrity
check, 59-CSV reanalysis with zero numeric difference, 52-claim/240-locator
evidence-map check, and three-figure numerical rebuild. Preparation of all
six frozen execution snapshots also passed without running SLS.

The raw-record reanalysis compares every field in the generated CSVs with the
accepted copies. The figure check compares source CSVs and accepted numeric
anchors. It confirms that the included and regenerated PDFs exist. PDF byte
identity is not a scientific acceptance criterion because fonts and rendering
software can change PDF bytes. SHA256SUMS checks that distributed files have
not changed; it does not tie the package to a particular manuscript PDF.

The scan covers plain and gzip-compressed candidate files. Its generic
home-directory match is the regular expression used by
scripts/release/verify_integrity.py to reject personal paths. Automated
scanning cannot establish ownership or detect every sensitive item.

Reproduction commands, in order, are:

~~~bash
sha256sum -c SHA256SUMS
python scripts/release/verify_integrity.py
python scripts/release/check_evidence_map.py
python scripts/release/reanalyze.py --output scratch/reanalysis
python scripts/release/rebuild_figures.py --output scratch/figures
python -m unittest discover -s tests -v
~~~

The full six-campaign SLS procedure is documented in SLS_REPRODUCTION.md.
It was not executed for this candidate. A public-Sionna/PyTorch environment
with an independently tested wheel lock is not distributed here. The author
authorized the code, data, and figure distribution terms described in
[License scope](docs/LICENSE_SCOPE.md). This validation did not provide an
independent legal review or fix final citation/version metadata. It did not
create a remote, release, or DOI.

The `clean_room_*` receipts document the earlier independent-path validation
and retain its historical path names. Current-path machine-readable receipts
are `provenance/validation/public_id_reanalysis_receipt.json`,
`provenance/validation/public_id_figure_receipt.json`, and
`provenance/manuscript_evidence_map_validation.json`.

## Reproducibility-path validation (2026-09-30)

The publication candidate now checks the numerical path between the accepted
raw records, analysis tables, figure inputs, three manuscript figures, and
indexed manuscript claims. The checks use public campaign IDs from
`provenance/public_id_map.json`; they assert six campaigns and 17 scenarios.

| Level | Result | Checked scope |
|---|---|---|
| 1. Distributed-file integrity | PASS | Package checksum list and integrity verifier; 136 manifests, 752 run outputs, six source snapshots, six configs |
| 2. Figure regeneration | PASS | Fig. 3–5 PDFs generated; all three generated source CSVs match frozen SHA-256 values |
| 3. Raw-record reanalysis and manuscript evidence | PASS | 136 runs, 59 accepted CSV comparisons, seven exact accepted-analysis/figure-input links, 52 evidence-map claims and 240 locators |
| 4. Representative SLS execution | PASS | Fast-CB, H(K=3), and F(K=3), each at rho=0.90 and seed 9101, executed from frozen snapshots |
| Full 136-run SLS rerun | NOT PERFORMED | Documented procedure; outside release validation |

The final reanalysis recorded a maximum absolute numeric difference of 0 across
59 CSVs. The comparison retains the existing absolute tolerance `1e-9` and
relative tolerance `1e-12` for recalculating the **same raw records**. The
manuscript check verified 31 numerical or directional result claims and all
2,927 source points of Fig. 5. It checked Test D printed confidence intervals,
its seed-9104 counterexample, prose results outside the three figures, and
the 2,500-measured-packet denominator for each of the 32 Fig. 5 runs.

Ten snapshot-preparation tests and nine evidence/figure tests passed. The
snapshot tests prepared all six mapped campaign IDs without running SLS. They
also rejected missing or corrupt inputs, occupied or unsafe destinations, and
incomplete preparation. A before/after SHA-256 comparison found all 1,423
fixed raw, analysis, figure, source, configuration, and provenance files
unchanged. The refreshed package checksum list covers the updated release
scripts and documentation separately.

The machine-readable receipts from this validation are generated under
`scratch/` by the commands in
[Figures and accepted-record analysis](docs/FIGURES_AND_ANALYSIS.md). They
distinguish the four validation levels. The author selected Apache-2.0 for
project-authored software and CC BY 4.0 for distributed data and figures.
Final article citation metadata remains unset. The complete 136-run SLS
campaign was not rerun.
No new-SLS numerical tolerance was inferred from accepted-record reanalysis
or the representative executions.

## Representative SLS execution (2026-09-30)

Three preselected seed-9101 cells at rho=0.90 were re-executed from their
published frozen sources and internal configurations: Fast-CB, H(K=3), and
F(K=3). Each prepared source tree was clean, imported public Sionna 2.0.1,
and completed on CPU without exception. The new records passed cohort, drain,
HARQ order, Shared-Cap2 resource, outcome-partition, provenance, and
policy-transition checks. All selected diagnostic KPIs matched the
corresponding published seed-9101 values.

This Level-4 result validates representative executability, not the full
136-run campaign or cross-environment statistical equivalence. The runs used
an existing version-matched SLS environment separate from the analysis
environment; a fresh installation on another machine was not tested. The
detailed execution records and validator receipts remain in the local
`scratch/reproduction-validation/` directory and are not distributed in
this candidate. The H(K=3) episode-table preservation flag is misleading
for this frozen scheme name, so continuation was checked from TB and HARQ
episode IDs, RV progression, and LLR history.
