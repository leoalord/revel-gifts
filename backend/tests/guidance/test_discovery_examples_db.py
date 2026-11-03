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


def _owner_example(tool: str, **match: Any) -> dict[str, Any]:
    from .test_owner_controls_f5 import owner_examples

    found = [
        d
        for t, d in owner_examples()
        if t == tool and all(k in d and d[k] == v for k, v in match.items())
    ]
    assert found, (tool, match)
    return found[0]


def _policy_update(data: dict[str, Any]) -> tuple[PolicyUpdate, bool]:
    body = profiles.AgentPolicyUpdate.model_validate(data)
    return PolicyUpdate(**body.model_dump(exclude={"owner_confirmed"})), body.owner_confirmed


async def test_owner_controls_refused_without_flag_then_succeed(session: Any, world: Any) -> None:
    agent = world.alex.mcp
    first, confirmed = _policy_update(
        _owner_example("set_contribution_policy", expected_version=None)
    )
    assert confirmed is True

    # Without the flag: refused with the documented changes list, and nothing is written.
    with pytest.raises(profiles.OwnerConfirmationRequired) as refused:
        await profiles.set_policy(session, agent, first)
    assert refused.value.reason_code == "owner_confirmation_required"
    assert refused.value.changes[0].startswith("Approve the contribution policy for the first")
    assert (await profiles.get_policy(session, agent)).policy is None

    # The same call with the owner's confirmation succeeds.
    approved = await profiles.set_policy(session, agent, first, owner_confirmed=True)
    assert approved.policy is not None and approved.policy.version == 1

    # A stale expected_version is a version conflict (re-read before asking again).
    with pytest.raises(profiles.PolicyVersionConflict):
        await profiles.set_policy(session, agent, first, owner_confirmed=True)

    # The documented tightening needs no flag.
    tighten, confirmed = _policy_update(
        _owner_example("set_contribution_policy", expected_version=1)
    )
    assert confirmed is False
    tightened = await profiles.set_policy(session, agent, tighten)
    assert tightened.policy is not None and tightened.policy.version == 2

    # Held facts: approving needs the flag, rejecting does not.
    held = next(i for a, i in examples() if i.get("involves_others"))
    other = held | {"statement": "Is planning a trip with Sam"}
    first_fact, second_fact = (await profiles.add_facts(session, agent, [held, other])).results
    assert {first_fact.outcome, second_fact.outcome} == {"pending_review"}
    approve = _owner_example("review_pending_fact", decision="approve")
    with pytest.raises(profiles.OwnerConfirmationRequired) as refused:
        await profiles.approve_fact(session, agent, first_fact.claim_id)
    assert refused.value.changes[0].startswith("Approve the pending")
    approved_fact = await profiles.approve_fact(
        session, agent, first_fact.claim_id, owner_confirmed=approve["owner_confirmed"]
    )
    assert approved_fact.status.value == "active"
    assert "owner_confirmed" not in _owner_example("review_pending_fact", decision="reject")
    rejected = await profiles.reject_fact(session, agent, second_fact.claim_id)
    assert rejected.status.value != "pending_review"
