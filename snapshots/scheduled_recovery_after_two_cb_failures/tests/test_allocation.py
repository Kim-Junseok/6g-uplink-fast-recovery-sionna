"""Fast tests for the research-owned activity representation."""

from __future__ import annotations

import unittest
from pathlib import Path

from ul_access.resource import ResourceAllocation
from ul_access.simulation.system import (
    assert_scenario_controls_match,
    load_config,
)


REPOSITORY = Path(__file__).resolve().parents[1]
ORTHOGONAL = REPOSITORY / "configs" / "orthogonal_ul.yaml"
OVERLAPPING = REPOSITORY / "configs" / "overlapping_ul.yaml"


class ResourceAllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.orthogonal_config = load_config(ORTHOGONAL)
        self.overlapping_config = load_config(OVERLAPPING)

    def test_expected_shape(self) -> None:
        for config in (self.orthogonal_config, self.overlapping_config):
            allocation = ResourceAllocation.from_config(config)
            self.assertEqual(allocation.shape, (1, 2, 4, 2, 1))
            self.assertEqual(allocation.allocated_re_per_ue().tolist(), [[4, 4]])

    def test_orthogonal_allocation_has_no_overlap(self) -> None:
        allocation = ResourceAllocation.from_config(self.orthogonal_config)
        self.assertFalse(allocation.has_overlap())
        self.assertEqual(allocation.overlap_coordinates(), [])

    def test_overlapping_allocation_reuses_resources(self) -> None:
        allocation = ResourceAllocation.from_config(self.overlapping_config)
        self.assertTrue(allocation.has_overlap())
        self.assertEqual(
            allocation.overlap_coordinates(),
            [[0, 0, 0], [0, 0, 1], [0, 1, 0], [0, 1, 1]],
        )

    def test_configuration_loading_is_deterministic_and_returns_copies(self) -> None:
        first = load_config(ORTHOGONAL)
        second = load_config(ORTHOGONAL)
        self.assertEqual(first, second)
        first["seed"] = -1
        self.assertEqual(second["seed"], 1234)

    def test_non_allocation_controls_match(self) -> None:
        assert_scenario_controls_match(
            self.orthogonal_config, self.overlapping_config
        )


if __name__ == "__main__":
    unittest.main()

