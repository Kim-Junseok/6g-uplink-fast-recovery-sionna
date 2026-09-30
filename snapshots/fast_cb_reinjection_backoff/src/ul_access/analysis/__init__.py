"""Research analysis utilities kept separate from access/protocol logic."""

from ul_access.analysis.harq_support import (
    HarqSupportClass,
    classify_history,
    collect_stage_conditioned_histories,
    support_coverage,
)

from ul_access.analysis.campaign import CampaignDataset, RunArtifact
from ul_access.analysis.curves import evaluate_ecdf, seed_averaged_curve
from ul_access.analysis.statistics import (
    exact_sign_flip_pvalue,
    leave_one_out_means,
    mean_student_t_ci95,
    paired_difference_summary,
)

__all__ = [
    "CampaignDataset",
    "HarqSupportClass",
    "RunArtifact",
    "classify_history",
    "collect_stage_conditioned_histories",
    "evaluate_ecdf",
    "exact_sign_flip_pvalue",
    "leave_one_out_means",
    "mean_student_t_ci95",
    "paired_difference_summary",
    "seed_averaged_curve",
    "support_coverage",
]
