# Phase A executable metric contract

Profile: `TECHNICAL_NOTE`

The simulation records `arrival_slot` when the modeled packet enters the
system. `completion_slot` is set on protocol ACK or terminal drop.
`completion_latency_slots = completion_slot - arrival_slot`; this is the
accepted packet latency endpoint. Correct-delivery latency uses packets whose
receiver accepted the TB **and** whose offline payload comparison is correct.
Terminal failures and undetected errors remain in the measured cohort but are
excluded from conditional P95/P99. Higher-layer retransmission, queueing,
HARQ feedback, and scheduled recovery time remain inside the interval.

Packet delivery probability is the count of correctly delivered measured
packets divided by 2500 measured exogenous packets per run. A CRC-pass with a
wrong payload is an undetected error, not a correct delivery. Terminal failure
and undetected error occupy mutually exclusive outcome categories with correct
delivery. The audit reconstructed this partition for all 128 runs.

Fig. 5(a) at latency bound L is the number of correctly delivered measured
packets with `completion_latency_slots <= L`, divided by **all 2500 measured
packets**, averaged across eight seeds. Failed and undetected-error packets
stay in the denominator. The figure source matches this reconstruction at
every plotted bound.

`scheduled attempts / scheduled-recovery packet` is the count of recorded
scheduled PUSCH attempts divided by the number of unique payload IDs with at
least one recorded scheduled PUSCH. Merely entering a scheduled queue does
not enter this denominator. The historical input field name
`scheduled_attempts_per_rescued_packet` implements this formula; the current
figure uses the manuscript-facing term.

The simulator definitions are in `src/ul_access/protocol/models.py` and
`src/ul_access/protocol/simulator.py`. Accepted campaign aggregators are
`experiments/35_v0_5_rebaseline_analysis.py`,
`38_v0_6_p3_analysis.py`, `40_v0_6_k3_analysis.py`,
`42_v0_6_f3_analysis.py`, and `44_v0_7_load_robustness_analysis.py`.
