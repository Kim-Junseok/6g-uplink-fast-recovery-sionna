# Publication-candidate release review

Profile: README_DOC

This is a local review candidate. It has no Git remote and has not been
published. The private research repository and its accepted artifacts remain
the authoritative historical record.

## Scope

The current manuscript uses exactly three result-figure files: the existing
fig3a, fig4a, and fig5a PDFs. The old 3(b), 4(b), and 5(b) PDFs are not selected
for publication. Their underlying numerical evidence remains relevant to
manuscript prose. The randomized Fast-CB reinjection-backoff campaign is a
supporting diagnostic with numerical claims in the text. Its eight runs
supplement the 128 main-comparison runs. The high-load Fast-CB baseline is
reused, not counted as eight new runs.

The candidate includes three standalone figure PDFs. It does not include
manuscript source or the full manuscript PDF. Figure verification uses the
underlying numerical inputs rather than manuscript-PDF hashes.

The local candidate passed the independent-path checks recorded in
VALIDATION_REPORT.md: 136 run manifests and 752 output hashes, 59 reanalyzed
CSV files with zero numerical difference, and regeneration of the three
selected figures. Those Level 1–3 checks did not execute SLS. A later Level-4 check executed one seed-9101 cell each for Fast-CB, H(K=3), and F(K=3); all three passed and their selected published seed-level KPIs matched.

## Review gates before public release

| Item | Candidate treatment | Release decision |
|---|---|---|
| Original research history | Private; exact executed source snapshots and a SHA crosswalk are selected for this candidate | Verify snapshot completeness |
| Accepted raw results | Retain original hashes and document every public-copy transformation | Verify the public-copy integrity report |
| Personal paths and identifiers | Scan source, manifests, metadata, scripts, and generated output | Review remaining matches and decide redactions |
| Sionna and other dependencies | Install public packages; do not bundle or patch a Sionna checkout | Review third-party notices and environment lock |
| Code license | No license has been selected by the author | Decide before publication |
| Data and figure reuse terms | No reuse terms have been selected by the author | Decide before publication |
| Citation metadata | No final public version, DOI, or repository URL exists | Add CITATION.cff when release identity is fixed |
| AI disclosure | Proposed Acknowledgment text is in AI_ASSISTANCE.md | Confirm against the final article before submission |
| Reviewer access | Local Git repository only | Test unauthenticated access after a separate publication decision |

This candidate must not be described as a full SLS rerun. Its validation
covers accepted-file integrity, reanalysis of retained raw records,
regeneration of the three selected figures, and representative execution
of the three core SLS policy paths. Full 136-run reproduction commands are
documented separately, but the complete campaign was not rerun.
