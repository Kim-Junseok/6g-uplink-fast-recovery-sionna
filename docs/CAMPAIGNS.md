# Campaign inventory

The campaign names below label the public data package. Historical
scenario names remain in the frozen configurations and accepted
manifests. [`public_id_map.json`](../provenance/public_id_map.json)
records the exact mapping.

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

The full execution commands are in
[SLS_REPRODUCTION.md](../SLS_REPRODUCTION.md). The three published figures
and accepted-record analysis are described in
[FIGURES_AND_ANALYSIS.md](FIGURES_AND_ANALYSIS.md).
