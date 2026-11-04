"""Discovery examples through F5's real ``add_facts`` against PostgreSQL (F4's FakeFga).

Skipped unless ``REVEL_TEST_DATABASE_URL`` is set, like F5's own suites; it reuses F5's fixtures
from ``tests/profiles/conftest.py`` (loaded by path, read-only) so setup matches theirs exactly.
Run DB suites serially.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from revel.db.enums import Audience
from revel.services import profiles
from revel.services.profiles import PolicyUpdate

from .test_discovery_f5 import examples

_F5_CONFTEST = Path(__file__).resolve().parents[1] / "profiles" / "conftest.py"
_spec = importlib.util.spec_from_file_location("_f9_reuse_f5_profiles_conftest", _F5_CONFTEST)
assert _spec and _spec.loader
_f5 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _f5  # dataclasses resolve their module through sys.modules
_spec.loader.exec_module(_f5)

# Re-export F5's fixtures into this module so pytest can resolve them here.
pf_engine = _f5.pf_engine
factory = _f5.factory
session = _f5.session
fga = _f5.fga
world = _f5.world

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(("attrs", "item"), examples())
async def test_example_outcome_through_real_service(
    session: Any, world: Any, attrs: dict[str, str], item: dict[str, Any]
) -> None:
    # The examples assume an approved policy at its defaults; the owner approves it in the UI.
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(expected_version=None))
    (result,) = (await profiles.add_facts(session, world.alex.mcp, [item])).results

    assert result.outcome == attrs["outcome"], result
    if "status" in attrs:
        assert result.status is not None and result.status.value == attrs["status"], result
    if attrs["outcome"] == "rejected":
        assert result.reason_code == attrs["reason_code"], result
        assert result.claim_id is None
    else:
        assert result.reason_code is None and result.claim_id is not None
    if item.get("involves_others") and "audience" not in item:
        assert result.audience is not None and result.audience.value == "only_me"
    if item["category"] == "sensitive" and attrs["outcome"] != "rejected":
        assert result.audience is not None and result.audience.value == "only_me"
    if "audience" in item and attrs["outcome"] != "rejected":
        assert result.audience is not None and result.audience.value == item["audience"]


async def test_no_policy_holds_every_example(session: Any, world: Any) -> None:
    # Documented: with no approved policy, everything an assistant adds waits for review.
    accepted = [item for attrs, item in examples() if attrs["outcome"] == "accepted"]
    results = (await profiles.add_facts(session, world.alex.mcp, accepted)).results
    assert {r.outcome for r in results} == {"pending_review"}


async def test_remove_fact_twice_is_an_error(session: Any, world: Any) -> None:
    # Documented: remove_fact is not idempotent.
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(expected_version=None))
    _attrs, item = next((a, i) for a, i in examples() if a["outcome"] == "accepted")
    (result,) = (await profiles.add_facts(session, world.alex.mcp, [item])).results
    await profiles.remove_fact(session, world.alex.mcp, result.claim_id)
    with pytest.raises(profiles.FactConflict):
        await profiles.remove_fact(session, world.alex.mcp, result.claim_id)


# --- Owner controls (F5 #29): the documented examples through the real service ----------------


def _owner_example(tool: str, step: str | None, **match: Any) -> dict[str, Any]:
    from .test_owner_controls_f5 import owner_steps

    found = [
        d
        for t, st, d in owner_steps()
        if t == tool and st == step and all(k in d and d[k] == v for k, v in match.items())
    ]
    assert len(found) == 1, (tool, step, match)
    return found[0]


def _policy_update(data: dict[str, Any]) -> tuple[PolicyUpdate, bool]:
    body = profiles.AgentPolicyUpdate.model_validate(data)
    return PolicyUpdate(**body.model_dump(exclude={"owner_confirmed"})), body.owner_confirmed


async def test_owner_controls_two_step_flow(session: Any, world: Any) -> None:
    agent = world.alex.mcp

    # First approval, step 1 (no flag): refused with the change list the skill quotes.
    step1, flag1 = _policy_update(_owner_example("set_contribution_policy", "1"))
    assert flag1 is False
    with pytest.raises(profiles.OwnerConfirmationRequired) as refused:
        await profiles.set_policy(session, agent, step1, owner_confirmed=flag1)
    assert refused.value.reason_code == "owner_confirmation_required"
    assert len(refused.value.changes) == 1  # defaults, Public off
    assert "first time" in refused.value.changes[0]
    assert (await profiles.get_policy(session, agent)).policy is None  # nothing written

    # Step 2: the identical call with the owner's confirmation succeeds.
    step2, flag2 = _policy_update(_owner_example("set_contribution_policy", "2"))
    assert step2 == step1 and flag2 is True
    approved = await profiles.set_policy(session, agent, step2, owner_confirmed=flag2)
    assert approved.policy is not None and approved.policy.version == 1

    # Repeating it now conflicts on the version: re-read and start over.
    with pytest.raises(profiles.PolicyVersionConflict):
        await profiles.set_policy(session, agent, step2, owner_confirmed=True)

    # The documented tightening succeeds in one call, without the flag.
    tighten, flag = _policy_update(_owner_example("set_contribution_policy", None))
    assert flag is False
    tightened = await profiles.set_policy(session, agent, tighten)
    assert tightened.policy is not None and tightened.policy.version == 2

    # Approving a held fact: step 1 refused, step 2 succeeds. Rejecting needs no flag.
    held = next(i for a, i in examples() if i.get("involves_others"))
    other = held | {"statement": "Is planning a trip with Sam"}
    first_fact, second_fact = (await profiles.add_facts(session, agent, [held, other])).results
    assert {first_fact.outcome, second_fact.outcome} == {"pending_review"}
    approve1 = _owner_example("update_fact", "1", action="approve")
    with pytest.raises(profiles.OwnerConfirmationRequired) as refused:
        await profiles.approve_fact(
            session,
            agent,
            first_fact.claim_id,
            owner_confirmed=approve1.get("owner_confirmed", False),
        )
    assert len(refused.value.changes) == 1
    assert refused.value.changes[0].startswith("Approve the pending")
    approve2 = _owner_example("update_fact", "2", action="approve")
    assert refused.value.expected_updated_at is not None
    approved_fact = await profiles.approve_fact(
        session,
        agent,
        first_fact.claim_id,
        owner_confirmed=approve2["owner_confirmed"],
        expected_updated_at=refused.value.expected_updated_at,
    )
    assert approved_fact.status.value == "active"
    # The documented rejection and live removal are distinct one-call actions.
    reject = _owner_example("remove_fact", None, action="reject")
    assert "owner_confirmed" not in reject
    rejected = await profiles.reject_fact(session, agent, second_fact.claim_id)
    assert rejected.status.value not in ("pending_review", "active")
    with pytest.raises(profiles.FactConflict):
        await profiles.reject_fact(session, agent, second_fact.claim_id)
    remove = _owner_example("remove_fact", None, action="remove")
    assert "owner_confirmed" not in remove
    removed = await profiles.remove_fact(session, agent, approved_fact.id)
    assert removed.status.value == "removed"

    # Widening a fact's audience: step 1 refused with one change, step 2 succeeds.
    narrow = {
        "category": "interest",
        "statement": "Enjoys birdwatching",
        "evidence_type": "explicit",
        "audience": "only_me",
    }
    (added,) = (await profiles.add_facts(session, agent, [narrow])).results
    assert added.outcome == "accepted"
    widen1 = _owner_example("update_fact", "1", action="set_audience")
    with pytest.raises(profiles.OwnerConfirmationRequired) as refused:
        await profiles.set_fact_audience(
            session, agent, added.claim_id, Audience(widen1["audience"]), owner_confirmed=False
        )
    assert len(refused.value.changes) == 1 and refused.value.changes[0].startswith("Widen")
    widen2 = _owner_example("update_fact", "2", action="set_audience")
    assert refused.value.expected_updated_at is not None
    widened = await profiles.set_fact_audience(
        session,
        agent,
        added.claim_id,
        Audience(widen2["audience"]),
        owner_confirmed=widen2["owner_confirmed"],
        expected_updated_at=refused.value.expected_updated_at,
    )
    assert widened.audience.value == widen2["audience"]
