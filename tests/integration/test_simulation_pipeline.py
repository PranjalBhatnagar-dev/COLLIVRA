"""   
ZORBIT - Full Vertical Simulation & Remediation Pipeline Integration Tests.

Validates the top-level package interface and the complete vertical execution pipeline
from collision breakup through Cartesian numerical batch propagation to Clohessy-Wiltshire
two-impulse rendezvous planning.
"""

from datetime import datetime, timedelta, timezone
import math
import numpy as np
import pytest
from pydantic import ValidationError

import simulation
from simulation import (
    BreakupAndPropagateRequest,
    BreakupAndPropagateResponse,
    BreakupRemediationRequest,
    BreakupRemediationResponse,
    CollisionScenario,
    FragmentationConfig,
    FragmentationResult,
    FragmentPropagationSummary,
    FragmentTrajectory,
    FragmentTrajectoryBatch,
    NumericalPropagationConfig,
    ParentBodyState,
    PropagateBatchRequest,
    PropagateSingleFragmentRequest,
    RendezvousConfig,
    RendezvousContract,
    RendezvousPlanRequest,
    RendezvousResult,
    SyntheticFragmentRecord,
    TargetingMode,
    plan_breakup_remediation,
    prepare_fragment_rendezvous_contract,
    select_highest_mass_cw_compliant_fragment,
    simulate_breakup_and_propagate,
)
from simulation.constants import MU_EARTH_KM3_S2


@pytest.fixture
def collision_epoch() -> datetime:
    return datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def circular_collision_scenario(collision_epoch: datetime) -> CollisionScenario:
    """Controlled circular orbit collision scenario at r = 7200 km."""
    r0 = 7200.0
    v_circ = math.sqrt(MU_EARTH_KM3_S2 / r0)

    parent_a = ParentBodyState(
        object_id="SAT-ALPHA",
        object_name="Satellite Alpha",
        mass_kg=500.0,
        position_km=(r0, 0.0, 0.0),
        velocity_kms=(0.0, v_circ, 0.001),
    )
    parent_b = ParentBodyState(
        object_id="SAT-BETA",
        object_name="Satellite Beta",
        mass_kg=500.0,
        position_km=(r0, 0.05, 0.0),
        velocity_kms=(0.0, v_circ, -0.001),
    )
    frag_cfg = FragmentationConfig(
        fragment_count=20,
        min_fragment_mass_kg=1.0,
        max_fragment_mass_kg=25.0,
        max_delta_v_kms=0.002,  # 2 m/s bound guarantees 100% CW compliance
        v_median_kms=0.001,
        seed=42,
    )
    return CollisionScenario(
        collision_epoch=collision_epoch,
        parent_a=parent_a,
        parent_b=parent_b,
        config=frag_cfg,
    )


def test_top_level_package_exports():
    """Verify that all Day 11 integration schemas and functions are exported at simulation root."""
    expected_exports = [
        # Schemas
        "PropagateSingleFragmentRequest",
        "PropagateBatchRequest",
        "BreakupAndPropagateRequest",
        "BreakupAndPropagateResponse",
        "RendezvousPlanRequest",
        "BreakupRemediationRequest",
        "BreakupRemediationResponse",
        # Functions
        "simulate_breakup_and_propagate",
        "select_highest_mass_cw_compliant_fragment",
        "prepare_fragment_rendezvous_contract",
        "plan_breakup_remediation",
    ]

    for sym in expected_exports:
        assert hasattr(simulation, sym), f"simulation package is missing public export: {sym}"
        assert sym in simulation.__all__, f"{sym} is missing from simulation.__all__"


def test_end_to_end_breakup_remediation_pipeline(circular_collision_scenario):
    """Verify end-to-end execution of plan_breakup_remediation with full diagnostic checks."""
    epoch = circular_collision_scenario.collision_epoch
    v_circ = math.sqrt(MU_EARTH_KM3_S2 / 7200.0)

    request = BreakupRemediationRequest(
        scenario=circular_collision_scenario,
        chaser_id="CHASER-INSPECTOR-1",
        chaser_epoch_utc=epoch,
        chaser_gcrs_position_km=[7200.0, 0.1, 0.0],
        chaser_gcrs_velocity_kms=[0.0, v_circ, 0.0],
        propagation_config=NumericalPropagationConfig(
            duration_seconds=7200.0,
            step_seconds=60.0,
        ),
        rendezvous_config=RendezvousConfig(
            targeting_mode=TargetingMode.STANDOFF_HOLD,
            standoff_distance_m=100.0,
            transfer_time_s=3600.0,
            delta_v_budget_max_ms=100.0,
        ),
    )

    response = plan_breakup_remediation(request)

    # 1. Output types
    assert isinstance(response, BreakupRemediationResponse)
    assert isinstance(response.fragmentation, FragmentationResult)
    assert isinstance(response.trajectories_summary, FragmentPropagationSummary)
    assert isinstance(response.selected_target, SyntheticFragmentRecord)
    assert isinstance(response.contract, RendezvousContract)
    assert isinstance(response.rendezvous, RendezvousResult)

    # 2. Fragmentation & Propagation integrity
    expected_count = circular_collision_scenario.config.fragment_count
    assert len(response.fragmentation.fragments) == expected_count
    assert response.trajectories_summary.total_fragments == expected_count
    assert response.trajectories_summary.completed_count == expected_count

    # 3. Target Selection & Contract
    assert response.selected_target.fragment_id == response.contract.target_id
    assert response.contract.chaser_id == "CHASER-INSPECTOR-1"
    assert response.selected_target.mass_kg > 0.0

    # 4. Two-Impulse CW Solution & Feasibility
    res = response.rendezvous
    assert res.is_mathematically_solvable is True
    assert res.is_budget_feasible is True
    assert res.is_feasible is True
    assert res.total_delta_v_ms > 0.0
    assert len(res.burn_1_dv_cw_ms) == 3
    assert len(res.burn_2_dv_cw_ms) == 3
    assert res.terminal_position_error_m < 1e-3  # Sub-millimeter terminal precision
    assert res.terminal_velocity_error_ms < 1e-3

    # 5. Telemetry & Scientific Disclaimer
    assert response.execution_wall_time_s > 0.0
    assert "RESEARCH PROTOTYPE" in response.scientific_disclaimer


def test_end_to_end_pipeline_deterministic_reproducibility(circular_collision_scenario):
    """Verify deterministic reproducibility excluding execution wall-clock duration."""
    epoch = circular_collision_scenario.collision_epoch
    v_circ = math.sqrt(MU_EARTH_KM3_S2 / 7200.0)

    req1 = BreakupRemediationRequest(
        scenario=circular_collision_scenario,
        chaser_id="CHASER-01",
        chaser_epoch_utc=epoch,
        chaser_gcrs_position_km=[7200.0, 0.1, 0.0],
        chaser_gcrs_velocity_kms=[0.0, v_circ, 0.0],
    )
    req2 = BreakupRemediationRequest(
        scenario=circular_collision_scenario,
        chaser_id="CHASER-01",
        chaser_epoch_utc=epoch,
        chaser_gcrs_position_km=[7200.0, 0.1, 0.0],
        chaser_gcrs_velocity_kms=[0.0, v_circ, 0.0],
    )

    resp1 = plan_breakup_remediation(req1)
    resp2 = plan_breakup_remediation(req2)

    # Physical determinism
    assert resp1.selected_target.fragment_id == resp2.selected_target.fragment_id
    assert resp1.selected_target.mass_kg == resp2.selected_target.mass_kg
    assert resp1.contract.target_gcrs_position_km == resp2.contract.target_gcrs_position_km
    assert resp1.contract.target_gcrs_velocity_kms == resp2.contract.target_gcrs_velocity_kms
    assert resp1.rendezvous.burn_1_dv_cw_ms == resp2.rendezvous.burn_1_dv_cw_ms
    assert resp1.rendezvous.burn_2_dv_cw_ms == resp2.rendezvous.burn_2_dv_cw_ms
    assert resp1.rendezvous.total_delta_v_ms == resp2.rendezvous.total_delta_v_ms

    # Timing recorded but excluded from equality
    assert resp1.execution_wall_time_s > 0.0
    assert resp2.execution_wall_time_s > 0.0


def test_end_to_end_pipeline_differential_epochs(circular_collision_scenario):
    """Verify Hermite spline epoch synchronization when target_epoch_utc != chaser_epoch_utc."""
    epoch = circular_collision_scenario.collision_epoch
    v_circ = math.sqrt(MU_EARTH_KM3_S2 / 7200.0)
    target_dt = epoch + timedelta(seconds=3.0)

    request = BreakupRemediationRequest(
        scenario=circular_collision_scenario,
        chaser_id="CHASER-SYNC",
        chaser_epoch_utc=epoch,
        target_epoch_utc=target_dt,
        chaser_gcrs_position_km=[7200.0, 0.1, 0.0],
        chaser_gcrs_velocity_kms=[0.0, v_circ, 0.0],
        rendezvous_config=RendezvousConfig(epoch_tolerance_s=10.0),
    )

    response = plan_breakup_remediation(request)
    assert response.rendezvous.is_mathematically_solvable is True
    assert response.rendezvous.epoch_synchronization_applied is True
    assert response.contract.target_epoch_utc == target_dt
    assert response.contract.chaser_epoch_utc == epoch


def test_end_to_end_rejection_no_compliant_fragments(collision_epoch):
    """Verify ValueError is raised when fragmentation parameters produce zero CW-eligible fragments."""
    v_circ = math.sqrt(MU_EARTH_KM3_S2 / 7200.0)

    # Dispersal with large delta-v (e.g. 1.0 km/s) produces orbits with e > 0.05 >> 0.005
    parent_a = ParentBodyState(
        object_id="SAT-A",
        mass_kg=500.0,
        position_km=(7200.0, 0.0, 0.0),
        velocity_kms=(0.0, v_circ, 0.0),
    )
    parent_b = ParentBodyState(
        object_id="SAT-B",
        mass_kg=500.0,
        position_km=(7200.0, 0.05, 0.0),
        velocity_kms=(0.0, v_circ + 0.8, 0.0),
    )
    cfg = FragmentationConfig(
        fragment_count=5,
        min_fragment_mass_kg=1.0,
        max_fragment_mass_kg=10.0,
        max_delta_v_kms=1.0,
        v_median_kms=0.8,
        seed=42,
    )
    scenario = CollisionScenario(
        collision_epoch=collision_epoch,
        parent_a=parent_a,
        parent_b=parent_b,
        config=cfg,
    )

    request = BreakupRemediationRequest(
        scenario=scenario,
        chaser_id="CHASER-FAIL",
        chaser_epoch_utc=collision_epoch,
        chaser_gcrs_position_km=[7200.0, 0.1, 0.0],
        chaser_gcrs_velocity_kms=[0.0, v_circ, 0.0],
    )

    with pytest.raises(ValueError, match="No CW-compliant, lifecycle-valid fragments found in batch"):
        plan_breakup_remediation(request)


def test_end_to_end_rejection_unphysical_chaser(circular_collision_scenario):
    """Verify rejection when chaser orbit is unphysical or non-circular."""
    epoch = circular_collision_scenario.collision_epoch

    # Chaser exceeding near-circular threshold e > 0.005
    with pytest.raises(ValueError, match="exceeds near-circular limit"):
        BreakupRemediationRequest(
            scenario=circular_collision_scenario,
            chaser_id="CHASER-ECCENTRIC",
            chaser_epoch_utc=epoch,
            chaser_gcrs_position_km=[7200.0, 0.0, 0.0],
            chaser_gcrs_velocity_kms=[0.0, 8.5, 0.0],  # Hyperbolic or high eccentricity
        )
