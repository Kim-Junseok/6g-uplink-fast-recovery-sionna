# V0.8 Test D: Fast-CB post-termination reinjection timing

Profile: `RESEARCH_SUMMARY`

## Decision

**Classification: PARTIAL-MEAN-IMPROVEMENT-WITH-UNRESOLVED-DELIVERY-GAIN.**
The `U{0,1,2,3}` additional-opportunity backoff reduced mean RLC and CB work
in the eight paired high-load seeds. Mean packet delivery increased from
0.7325 to 0.8839, but the paired 95% interval for that increase includes
zero. One seed deteriorated sharply. The tested short backoff therefore
changes the observed recovery feedback and reduces average work, but it does
not remove the high-load delivery degradation under this finite experiment.
The result neither establishes steady-state stability nor covers other
backoff windows.

## Executed comparison

The new policy changes only post-termination fresh-recovery reinjection
from the first originally eligible CB opportunity `o0` to `oB`, where
`B ~ Uniform{0,1,2,3}` is drawn once per recovery episode. It applies during
warm-up and measurement. It does not delay a packet's initial CB access or a
retransmission within an active HARQ episode. Both conditions use the same
`rho=0.90`, Medium TDL-C, independent-Poisson, six-PRB Shared-Cap2, direct
public-Sionna HARQ LLS, 160-bit modeled TB payload, RV sequence
`0→2→3→1`, four HARQ attempts, and six RLC retransmission cap. Receiver
activity is known, CSI is perfect, and the receiver uses joint LMMSE.
The accepted reference uses the original immediate reinjection policy.

Eight new clean-SHA production runs used seeds 9101–9108. Each run measured
2,500 exogenous packets after 500 warm-up packets and drained to terminal
state. The implementation SHA was
`c3119e74f4cb5db8c9d8523b80b489549063287d`; the Test D configuration
SHA-256 was
`d77465ce116833921ec329468f1d8fd49f614ff3a5a04f8c32ef4415915519db`.
The accepted reference implementation SHA was
`d66574649168feeb4a48d95784ea7ecf1255babc` and its configuration
SHA-256 was
`23e70da2233671875735f91613709ebc57d803409b5ac7f646dae49c49e9a834`.
No accepted V0.5–V0.7 result artifact was modified.

## Primary outcomes

Each mean is the arithmetic mean of eight seed-level values. The difference
is backoff minus reference within the same seed. Intervals are untruncated
two-sided 95% Student-t intervals over eight paired differences. Exact
sign-flip values and leave-one-out means are included in
`results/v0_8_test_d/paired_statistics.csv`. These metric-wise intervals
and p-values are unadjusted for multiple comparisons; they do not support a
family-wise error claim.

| Metric | Reference | Backoff | Paired difference | 95% interval | Exact sign-flip p |
|---|---:|---:|---:|---:|---:|
| Packet delivery probability | 0.7325 | 0.8839 | +0.1514 | [−0.0067, 0.3095] | 0.0938 |
| Terminal failure probability | 0.2672 | 0.1160 | −0.1512 | [−0.3092, 0.0068] | 0.0938 |
| RLC retransmissions / exogenous packet | 1.7760 | 0.7968 | −0.9792 | [−1.9391, −0.0192] | 0.0391 |
| CB attempts / exogenous packet | 10.0639 | 5.8483 | −4.2156 | [−8.2987, −0.1325] | 0.0391 |
| CB attempts / injection-window slot | 45.05 | 28.09 | −16.96 | [−33.79, −0.13] | 0.0391 |
| Measured packets unresolved at injection end | 153.38 | 67.88 | −85.50 | [−156.57, −14.43] | 0.0391 |
| P99 packet latency conditioned on delivery [slots] | 49.49 | 32.81 | −16.67 | [−43.34, 9.99] | 0.1953 |

Correctly delivered packets divided by all 2,500 measured packets define
packet delivery probability. Protocol drops divided by the same denominator
define terminal failure; undetected errors are separate. The new policy had
mean undetected-error probability 0.0001 versus 0.0003 for the reference.
RLC and CB work per exogenous packet also divide by 2,500. The
injection-window CB rate uses **all** CB attempts during the arrival horizon,
including warm-up traffic, divided by that horizon's slot count. It is distinct
from the accepted historical `CB attempts per slot` KPI, which divides
measured-packet CB attempts by all recorded slots including drain.

## Seed dependence and latency distribution

| Seed | Reference delivery | Backoff delivery | Reference CB attempts / packet | Backoff CB attempts / packet |
|---:|---:|---:|---:|---:|
| 9101 | 0.9996 | 0.9996 | 3.04 | 2.92 |
| 9102 | 0.8128 | 1.0000 | 7.82 | 2.78 |
| 9103 | 0.6180 | 1.0000 | 13.03 | 2.84 |
| 9104 | 0.1624 | 0.0720 | 24.86 | 27.02 |
| 9105 | 0.9120 | 1.0000 | 5.67 | 2.85 |
| 9106 | 0.9996 | 0.9996 | 3.13 | 2.76 |
| 9107 | 0.7984 | 1.0000 | 8.53 | 2.73 |
| 9108 | 0.5572 | 1.0000 | 14.43 | 2.88 |

Delivery improved in five seeds, tied in two, and worsened in seed 9104.
The paired delivery improvement remains positive under every single-seed
omission (leave-one-out range 0.1098–0.1859), but its eight-seed 95% interval
crosses zero. The observed 9104 deterioration is substantive: terminal
failure increased from 0.8372 to 0.9280, CB attempts per measured packet
rose from 24.86 to 27.02, and conditional P99 grew from 84.90 to 138.51
slots. Its injection-end unresolved measured packets rose from 292 to 341.
This counterexample prevents a claim that this short backoff reliably restores
high-load delivery.

The all-measured-packet delivery-versus-latency curve reaches 0.6930 for the
reference and 0.8731 for backoff by 20 slots. At 100 slots it reaches
0.7324 and 0.8835, respectively. Its final values are 0.7325 and 0.8839;
unsuccessful packets remain in the denominator. The source CSV and
PDF/SVG/300-dpi PNG are under `results/v0_8_test_d/`. Conditional P99 does
not have a paired interval wholly below zero. A lower conditional P99 cannot
substitute for the reliability and terminal-failure comparison.

## Mechanism evidence and limits

The injection-window 95th percentile of per-slot CB attempts fell from
114.07 to 42.00 on the eight-seed mean. Mean per-slot CB-attempt variance
fell from 1940.78 to 360.33. Same-PRB overlap probability among measured CB
attempts fell from 0.9709 to 0.9277. Resource overlap denotes simultaneous
reuse, not automatic collision or decoding failure. These observations,
together with lower RLC/CB work in seven seeds, are consistent with reduced
recovery amplification under the randomized timing rule. Seed 9104 follows
the opposite workload and delivery direction. The experiment changes actual
transmission slots and can change channel/noise event identities and
interference; it does not isolate a fixed-PHY-sample latency shift. The
specific causal link from timing spread to each PHY outcome remains an
interpretation, not a separately identified effect.

## Validation and reproduction

The forced-`B=0` engineering gate compared seed 9101 with the accepted
reference. All 2,500 packet rows, 560 slot rows, and all shared discrete
fields across 7,589 attempt rows matched exactly. Four CPU floating-point
SINR diagnostics differed by at most 0.000153 dB, below the documented
0.001-dB diagnostic limit, without any ACK or packet-outcome difference.
The full fast suite passed 166 tests before production. The independent
post-run audit checked eight reference and eight Test D cells: packet
outcomes, HARQ RV order and spacing, feedback slots, RLC cap, PRB budget,
reinjection decisions, source hashes, and terminal drain had zero recorded
violations. All new manifests recorded the same clean implementation SHA.
The eight new runs consumed 1.13 CPU-wall hours sequentially and occupy
about 12 MB compressed; peak recorded RSS was 2.17 GiB.

From the repository root, reproduce the new results and the read-only
post-analysis with the commands in `docs/V0_8_TEST_D_METHOD.md` and
`results/v0_8_test_d/README.md`. The aggregate analysis classification is
`V0.8-TEST-D-ANALYSIS-COMPLETE`; the independent audit classification is
`TEST-D-INTEGRITY-PASS`. This is the one predeclared Test D window. No
additional backoff window, load, channel, or retry-policy sweep was run.
