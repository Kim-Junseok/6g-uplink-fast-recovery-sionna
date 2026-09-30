"""Software and device metadata for reproducible result files."""

from __future__ import annotations

import platform
from importlib.metadata import version
from pathlib import Path
from typing import Any
import json

import sionna
import torch


def software_environment() -> dict[str, Any]:
    """Return versions and compute-device metadata for result provenance."""
    cuda_available = torch.cuda.is_available()
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "sionna": version("sionna"),
        "sionna_import_path": str(Path(sionna.__file__).resolve()),
        "torch": torch.__version__,
        "cuda_available": cuda_available,
        "torch_cuda_version": torch.version.cuda,
        "device": torch.cuda.get_device_name(0) if cuda_available else "cpu",
    }


def require_compatible_configuration_hashes(manifest_paths: list[str | Path]) -> str:
    """Reject aggregation of run manifests with different configurations."""
    if not manifest_paths:
        raise ValueError("at least one manifest is required")
    hashes = {
        json.loads(Path(path).read_text(encoding="utf-8"))["configuration_hash"]
        for path in manifest_paths
    }
    if len(hashes) != 1:
        raise ValueError(f"incompatible configuration hashes: {sorted(hashes)}")
    return hashes.pop()
