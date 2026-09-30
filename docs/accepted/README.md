# Accepted validation records

Profile: README_DOC

The files in this directory and data/validation/phase_a are byte-preserved
copies of selected accepted research records. Their source-relative names and
SHA-256 hashes are in provenance/accepted_audit_sha256.csv.

COMMMAG_PHASE_A_DECISION.md reports the read-only audit of 128 V0.5–V0.7
production runs. metric_contract.md defines packet latency, delivery,
conditional percentiles, and the scheduled-attempt denominator. The Phase A
audit combines raw packet/attempt/HARQ records, implementation semantics, and
reconstructed invariants. It does not claim a complete event log or
steady-state stability.

V0_8_TEST_D_REPORT.md records the eight-run randomized-reinjection
diagnostic, including its paired intervals, one worsening seed, and
finite-batch interpretation. The existing V0.5 Fast-CB reference is reused
for that comparison. Its raw records and numeric analysis are packaged
separately under data/raw and data/accepted_analysis.
