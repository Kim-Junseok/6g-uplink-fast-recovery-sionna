# CommMag Phase A decision

Profile: `RESEARCH_SUMMARY`

The audit checked 128 accepted V0.5–V0.7 runs without executing SLS or
changing accepted results. It found zero violations of the tested executable
resource, timing, HARQ identity, retry, duplicate-work, and metric invariants.
The named timing-reset scenario document is absent; historical manifests,
configuration blobs, and the repository timing contract establish the
executed behavior. The manuscript must correct the missing document reference
and keep finite-horizon and H/F budget language precise.

| Audit item | Status | Evidence | Manuscript impact | Simulation impact |
|---|---|---|---|---|
| A1 provenance and named scenario contract | PASS_WITH_DOCUMENTATION_FIX | 128 manifests/hash checks; historical configs; absent named file | Correct the scenario reference | None |
| A2 payload/TB mapping | PASS_WITH_DOCUMENTATION_FIX | 160-bit config, packet/TB/episode IDs, restart code | Do not claim 20-byte application payload **plus** headers | None |
| A3 actual-slot Shared-Cap2 | PASS_WITH_DOCUMENTATION_FIX | 97,006 slot rows; zero pool or visible PRB overlaps; historical scheduler | State warm-up PRB visibility limit | None |
| A4 HARQ and K2 timing | PASS | 995,870 attempts; zero feedback, spacing, RV or DCI violations | Retain timing description | None |
| A5 Legacy timer/SR/grant | PASS | Recorded timing rows, historical policy; zero tested violations | Call it a controlled Legacy path | None |
| A6 H continuation | PASS_WITH_DOCUMENTATION_FIX | 17,238 V0.6 H first transfers plus V0.7; IDs, RV, LLR count, accepted code | Explain code/record proof limit without full event log | None |
| A7 F restart | PASS_WITH_DOCUMENTATION_FIX | 15,774 V0.6 F first transfers plus V0.7; new IDs/RV0, accepted code | Distinguish early abandonment from final failure | None |
| A8 retry accounting | PASS_WITH_DOCUMENTATION_FIX | Six-cap receipts, per-packet RLC and HARQ counts, F code path | State F early restart does not increment RLC retry; H limits are 4, F2 is 6, F3 is 7 | None |
| A9 pending and duplicate work | PASS_WITH_DOCUMENTATION_FIX | Cross-table duplicate/terminal checks; accepted state branches | Avoid claiming exhaustive negative event-log proof | None |
| A10 protocol-visible versus oracle | PASS | CRC/ACK agreement, outcome partition, accepted feedback code | State idealized identity/activity | None |
| A11 executable PHY | PASS | 128 historical config hashes and receipts | State fixed category and assumptions | None |
| A12 metric endpoints and denominators | PASS | Raw packet/attempt/slot reconstruction; `metric_contract.md` | Preserve exact numerator/denominator wording | None |
| B1 work, injection and drain | PASS | `work_horizon_seed_summary.csv`; zero unresolved after drain | Describe finite batch | None |
| B2 sustained-work interpretation | PASS_WITH_DOCUMENTATION_FIX | Injection-end backlog and drain lengths | Do not claim steady-state stability | None |
| B3 scheduled workload | PASS | `scheduled_workload_seed_summary.csv` | Distinguish queue requests from transmitting packets | None |
| B4 K=2 versus K=3 attribution | PASS | Scheduled packet counts and service costs | Attribute reduced work mainly to admission | None |
| B5 H versus F attribution | PASS_WITH_DOCUMENTATION_FIX | Episode/RV and retry differences | Call it an operational policy comparison | None |
| B6 paired seed comparisons | PASS | 19 accepted rows match; 30 rows marked not previously reported | Do not promote new descriptive rows to tested claims | None |
| B7 Fig. 3–5 source consistency | PASS | Six figures recomputed from accepted raw records | Existing plotted values stand | None |

Overall classification: `PHASE-A-PASS-WITH-DOCUMENTATION-FIXES`.
Audit A classification: `PASS_WITH_DOCUMENTATION_FIX`.
Audit B classification: `PASS_WITH_DOCUMENTATION_FIX`.

## Required manuscript corrections

1. Replace the nonexistent `commmag_simulation_scenarios_v0_5_timing_reset.md`
   reference with the actual configuration and timing-contract paths. Do not
   manufacture a replacement file.
2. Describe the 160-bit quantity as the modeled TB payload. MAC/RLC headers
   and an additional application-layer payload are not separately modeled.
3. Describe H/F as continuity versus restart policies; F early restart does
   not consume the RLC retry counter. Avoid a soft-combining-only claim.
4. Qualify high-load conclusions as finite-horizon, drain-to-terminal results.
   Sustained-operation stability has not been established.
5. Preserve all-measured-packet denominator for Fig. 5(a), successful-delivery
   conditioning for P99, and actual scheduled-transmission packet denominator
   for scheduled attempts per scheduled-recovery packet.
6. State that complete event logs and warm-up attempt PRB rows are not stored;
   negative event findings combine available records with historical code.

## Fast-CB Test D

`TEST-D-GO` is the recommendation for a **separately approved** diagnostic.
Fast-CB has no Phase A correctness blocker. Both accepted configs set
`cb_retry_backoff_slots: 0`; historical code uses the next eligible CB
opportunity after the one-slot failure indication. All 7,597 recorded
measured reinjection gaps in V0.5 and V0.7 are one slot. The high-load
0.7325 correct-delivery probability and other accepted KPIs reproduce. A
Uniform{0,1,2,3} additional-opportunity backoff would therefore test a new
assumption rather than duplicate an existing randomized backoff. This audit
does not authorize or execute that simulation. The scheduled-readiness-delay
campaign remains cancelled.

No accepted numerical result changed. No SLS rerun is required by Phase A.
The blueprint copy is byte-identical to the supplied 2026-09-24 file and its
SHA-256 is stored in `audit_manifest.json`. No manuscript or blueprint text
was edited beyond preserving that copy.
