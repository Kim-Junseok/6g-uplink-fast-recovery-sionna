# Reproduce the Sionna simulation runs

This guide documents the full 136-cell SLS reproduction procedure from six
published source snapshots. Release validation checked the distributed files,
redrew Figs. 3–5, and reanalyzed the accepted raw records. Its Level-4 check
re-executed one seed-9101 cell each for Fast-CB, H(K=3), and F(K=3). All three
runs completed, and their selected diagnostic KPIs matched the corresponding
accepted seed-level values. The full 136-run SLS campaign was not re-executed.

## Environment

The accepted run manifests record Python 3.12.3, public Sionna 2.0.1, PyTorch
2.11.0+cu130, CPU execution, one Torch thread, one worker, and deterministic algorithms.
The frozen `pyproject.toml` files specify PyYAML 6.0.3; the production manifests do
**not** record its version. The PyTorch build includes CUDA support, but the
accepted simulations selected the CPU device. The source imports Sionna from the Python
environment and does not require a local Sionna checkout.

From the publication repository root, create a separate environment:

```bash
python3.12 -m venv .venv-sls
. .venv-sls/bin/activate
python -m pip install 'torch==2.11.0+cu130' \
  --index-url https://download.pytorch.org/whl/cu130
python -m pip install 'sionna==2.0.1' 'PyYAML==6.0.3'
python -m pip check
```

`pip check` checks installed dependency compatibility. These commands do not constitute
a complete lock of the original SLS environment's transitive dependencies. The
representative runs used an existing SLS environment separate from the analysis
environment. No independent installation lock has been tested on another machine.

Check the versions that can be compared with the manifests and frozen source:

```bash
python - <<'PY'
import platform
from importlib.metadata import version
import torch

assert platform.python_version() == "3.12.3"
assert version("sionna") == "2.0.1"
assert version("PyYAML") == "6.0.3"  # Frozen pyproject.toml, not run manifests
assert torch.__version__ == "2.11.0+cu130"
print("RECORDED-SLS-VERSIONS-MATCH")
PY
```

Use Linux for this preparation path: its atomic, no-overwrite publication of a
prepared tree uses Linux `renameat2`. The frozen runner also imports Python's
`resource` module. The version check does not verify every installed dependency
or numerical SLS equivalence.

## Prepare a source snapshot

Set the package root and interpreter while the SLS environment is active:

```bash
PACKAGE_ROOT="$(pwd -P)"
SLS_PYTHON="$PACKAGE_ROOT/.venv-sls/bin/python"
```

For each public campaign ID, the corresponding block below runs `prepare_snapshot.py` before
starting SLS. An existing prepared directory is rejected, so run each preparation
command only once. The script reads `provenance/public_id_map.json`, checks the distributed snapshot
against its manifest, copies the frozen source to a temporary directory, checks the
copied file hashes, and prepares the historical raw-data input links. It checks the
destination and output path before moving the complete tree to `scratch/sls/<public campaign ID>/`. It fails
without overwriting an existing destination. The script leaves the current shell
directory unchanged.

The prepared tree excludes the packaging-only `source_manifest.json` from its working copy and
adds a `.gitignore` for generated outputs. Its simulation source and internal
configuration files retain their manifest hashes. The script makes a clean **local reproduction commit**
because the frozen runner requires a clean Git worktree. The original implementation SHA
in the manifest identifies the accepted execution. The new local commit SHA identifies
only this prepared copy; the two SHAs are not interchangeable. Preparation reports both
SHAs, its execution directory, and the checked output path.

Historical paths under the prepared tree's `results/raw_*` directory are symlinks to
published records in `data/raw/`. They are inputs, not output directories. For example,
the Fast-CB backoff snapshot expects its reference under
`results/raw_v0_5/V0_5_RHO090_FAST_CB`. That path points to
`data/raw/high_load_baselines/fast_cb`. A symlink does not make the target
read-only. The preparation script accepts `scratch/sls/` for the documented full-campaign
commands and `scratch/reproduction-validation/` for bounded execution checks.
It rejects other destinations and output paths outside the prepared tree's
`generated/` directory. The commands below use those checked output locations.
Hand-edited runner commands remain the user's responsibility.

## Run the six campaigns

The commands below use historical `--group` values and internal config filenames
retained by the frozen snapshots. Public campaign IDs label the prepared directories and
new output roots. Run each block from the publication repository root after setting
`PACKAGE_ROOT` and `SLS_PYTHON` above. The `&&` after preparation prevents that
campaign's SLS command from starting if preparation fails. Each subshell returns to the
package root when it ends.

```bash
"$SLS_PYTHON" scripts/release/prepare_snapshot.py high_load_baselines &&
(
  cd "$PACKAGE_ROOT/scratch/sls/high_load_baselines" &&
  PYTHONPATH=src "$SLS_PYTHON" experiments/24_commmag_priority_campaign.py \
    --config configs/timing_rebaseline_v0_5.yaml \
    --group V0.5-RB --all-main-seeds \
    --output-root generated/high_load_baselines
)

"$SLS_PYTHON" scripts/release/prepare_snapshot.py scheduled_recovery_after_two_cb_failures &&
(
  cd "$PACKAGE_ROOT/scratch/sls/scheduled_recovery_after_two_cb_failures" &&
  PYTHONPATH=src "$SLS_PYTHON" experiments/24_commmag_priority_campaign.py \
    --config configs/p3_bhf_v0_6.yaml \
    --group V0.6-P3 --all-main-seeds \
    --output-root generated/scheduled_recovery_after_two_cb_failures
)

"$SLS_PYTHON" scripts/release/prepare_snapshot.py harq_continuation_after_three_cb_failures &&
(
  cd "$PACKAGE_ROOT/scratch/sls/harq_continuation_after_three_cb_failures" &&
  PYTHONPATH=src "$SLS_PYTHON" experiments/24_commmag_priority_campaign.py \
    --config configs/p3_hk3_v0_6.yaml \
    --group V0.6-K3 --all-main-seeds \
    --output-root generated/harq_continuation_after_three_cb_failures
)

"$SLS_PYTHON" scripts/release/prepare_snapshot.py fresh_harq_after_three_cb_failures &&
(
  cd "$PACKAGE_ROOT/scratch/sls/fresh_harq_after_three_cb_failures" &&
  PYTHONPATH=src "$SLS_PYTHON" experiments/24_commmag_priority_campaign.py \
    --config configs/p3_fk3_v0_6.yaml \
    --group V0.6-F3 --all-main-seeds \
    --output-root generated/fresh_harq_after_three_cb_failures
)

"$SLS_PYTHON" scripts/release/prepare_snapshot.py lower_load_policy_comparison &&
(
  cd "$PACKAGE_ROOT/scratch/sls/lower_load_policy_comparison" &&
  PYTHONPATH=src "$SLS_PYTHON" experiments/24_commmag_priority_campaign.py \
    --config configs/load_robustness_v0_7.yaml \
    --group V0.7-RHO070 --all-main-seeds \
    --output-root generated/lower_load_policy_comparison
)

"$SLS_PYTHON" scripts/release/prepare_snapshot.py fast_cb_reinjection_backoff &&
(
  cd "$PACKAGE_ROOT/scratch/sls/fast_cb_reinjection_backoff" &&
  PYTHONPATH=src "$SLS_PYTHON" experiments/45_v0_8_fastcb_backoff.py \
    --config configs/fastcb_reinjection_backoff_v0_8.yaml \
    --all-main-seeds \
    --output-root generated/fast_cb_reinjection_backoff
)
```

The public raw-data paths use the following campaign and scenario IDs. Original scenario
IDs remain in the internal configs and manifests; use `provenance/public_id_map.json` for the exact
mapping.

| Public campaign ID | Public scenario IDs | Runs |
|---|---|---:|
| `high_load_baselines` | `legacy_poll_20_slots`, `legacy_poll_15_slots`, `fast_cb`, `post_termination_scheduled` | 32 |
| `scheduled_recovery_after_two_cb_failures` | `harq_preserving`, `fresh_harq` | 16 |
| `harq_continuation_after_three_cb_failures` | `harq_preserving` | 8 |
| `fresh_harq_after_three_cb_failures` | `fresh_harq` | 8 |
| `lower_load_policy_comparison` | `legacy_poll_20_slots`, `legacy_poll_15_slots`, `fast_cb`, `post_termination_scheduled`, `harq_preserving_after_two_cb_failures`, `fresh_harq_after_two_cb_failures`, `harq_preserving_after_three_cb_failures`, `fresh_harq_after_three_cb_failures` | 64 |
| `fast_cb_reinjection_backoff` | `randomized_reinjection_backoff` | 8 |

## Outputs and comparison boundary

A new run writes under:

```text
scratch/sls/<public campaign ID>/generated/<public campaign ID>/<original scenario ID>/seed_<seed>/
```

The representative Level-4 runs used the corresponding directories under
`scratch/reproduction-validation/`. Their detailed report and receipts remain
in that local, Git-ignored scratch directory. The runs are separate from the
accepted records and the full-campaign output location above.

Each seed directory contains a run manifest, a summary, and compressed packet,
attempt, and slot-resource records. Some campaigns also write HARQ-episode,
recovery-timing, or reinjection-decision tables. Distributed
accepted records stay under `data/raw/` and are not replaced by these commands.

The original wall times sum to about 12.37 hours; another machine may differ. New
manifests record the local reproduction commit, timestamp, interpreter path, and
runtime, so manifest bytes need not match accepted manifests. The supplied `reanalyze.py`
reads the **accepted** `data/raw/` records; it does not automatically analyze newly
generated records. The `1e-9` absolute and `1e-12` relative tolerances apply
to reanalysis of the same accepted raw records only. A new SLS numerical-equivalence
criterion has not been established. The three representative seed-9101 runs
matched selected published KPIs, but this diagnostic match does not establish
cross-environment equivalence. A figure rebuild or a matching package hash
therefore does not by itself establish a successful full SLS reproduction.
