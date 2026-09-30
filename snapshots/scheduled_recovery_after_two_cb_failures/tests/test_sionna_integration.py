"""Lightweight checks of the public Sionna integration boundary."""

from __future__ import annotations

import subprocess
import unittest
from importlib.metadata import version
from pathlib import Path

import torch

import sionna
from sionna.phy.utils import dbm_to_watt
from sionna.sys import spread_across_subcarriers

from ul_access.phy import PhyBackend
from ul_access.phy.sionna_backend import SionnaPhyBackend
from ul_access.resource import ResourceAllocation
from ul_access.protocol import GrantFreeAccess, Packet, SimulationParameters, SlotSimulator
from ul_access.recovery import FastArqPolicy, HarqController
from ul_access.simulation.system import load_config
from ul_access.config import load_yaml
from ul_access.provenance import software_environment


REPOSITORY = Path(__file__).resolve().parents[1]


class SionnaIntegrationTest(unittest.TestCase):
    def test_software_environment_uses_installed_sionna_metadata(self) -> None:
        environment = software_environment()
        self.assertEqual(environment["sionna"], "2.0.1")
        self.assertIn("python", environment)
        self.assertIn("device", environment)

    def test_sionna_backend_implements_public_protocol_interface(self) -> None:
        self.assertTrue(issubclass(SionnaPhyBackend, PhyBackend))

    def test_sionna_is_not_imported_from_sibling_source_checkout(self) -> None:
        import_path = Path(sionna.__file__).resolve()
        sibling_checkout = REPOSITORY.parent / "sionna"
        self.assertFalse(import_path.is_relative_to(sibling_checkout.resolve()))
        self.assertEqual(version("sionna"), "2.0.1")

    def test_sibling_sionna_checkout_is_unmodified_when_present(self) -> None:
        sibling_checkout = REPOSITORY.parent / "sionna"
        if not (sibling_checkout / ".git").exists():
            self.skipTest("no sibling Sionna checkout in this environment")
        status = subprocess.run(
            ["git", "-C", str(sibling_checkout), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(status.stdout, "")

    def test_public_power_spreading_accepts_overlap(self) -> None:
        config = load_config(REPOSITORY / "configs" / "overlapping_ul.yaml")
        allocation = ResourceAllocation.from_config(config)
        total_power = dbm_to_watt(torch.tensor([[0.0, 0.0]])).unsqueeze(1)
        total_power = total_power.expand(1, 2, 2)
        power_grid = spread_across_subcarriers(
            total_power, allocation.mask, num_tx=2
        )

        self.assertEqual(tuple(power_grid.shape), (1, 2, 1, 2, 4))
        self.assertTrue(torch.all(power_grid[:, :, :, :, 0:2] > 0))
        self.assertTrue(torch.all(power_grid[:, :, :, :, 2:4] == 0))
        # Each UE's configured per-symbol total is conserved independently,
        # including when both UEs reuse the same two subcarriers.
        expected = dbm_to_watt(torch.tensor(0.0))
        actual = power_grid.sum(dim=(-1, -2, -3))
        self.assertTrue(torch.allclose(actual, torch.full_like(actual, expected * 2)))

    def test_recovery_engine_executes_through_real_sionna_backend(self) -> None:
        config = load_yaml(REPOSITORY / "configs" / "common.yaml")
        backend = SionnaPhyBackend(config)

        class Traffic:
            def arrivals(self, slot: int) -> tuple[Packet, ...]:
                return (Packet(99, 0, 0, 24),) if slot == 0 else ()

        result = SlotSimulator(
            parameters=SimulationParameters(
                3, backend.num_ues, 4401,
                tuple(config["transmit_power"]["per_ue_per_ofdm_symbol_dbm"]),
                tuple(config["phy_abstraction"]["mcs_index"])),
            traffic=Traffic(),
            access=GrantFreeAccess(resource_pool_size=backend.num_subcarriers,
                                   opportunity_period_slots=1, max_retries=0,
                                   retry_backoff_slots=0),
            phy_backend=backend,
            harq=HarqController(max_attempts=1, feedback_delay_slots=1),
            recovery=FastArqPolicy(failure_indication_delay_slots=1),
            max_rlc_retransmissions=0,
        ).run()
        self.assertEqual(result["packets"]["arrived"], 1)
        self.assertEqual(result["recovery"]["total_phy_transmissions"], 1)


if __name__ == "__main__":
    unittest.main()
