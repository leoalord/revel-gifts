"""Opt-in live Muse Spark check (real network call to Fireworks).

Skipped unless ``REVEL_LIVE_MUSE_SPARK=1``; needs ``FIREWORKS_API_KEY`` (and optionally
``FIREWORKS_MODEL``) in the environment or ``.env``. Synthetic purposes only. Either path is
accepted (the guard may reject the model's proposal) but the published decision must always be
safe and in-set; the path taken is printed.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from revel.services.muse_spark import MuseSparkClient
from revel.services.question_routing import (
    SYSTEM_PROMPT,
    Candidate,
    PrivatePurpose,
    _build_user_prompt,
    publishable_question_problems,
    route_question,
)
from revel.settings import Settings

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("REVEL_LIVE_MUSE_SPARK") != "1", reason="REVEL_LIVE_MUSE_SPARK != 1"
    ),
]

IDENTITY = ("Jordan Example", "Jordan", "jordan")
CASES = {
    "plants": (
        "What kinds of plants or flowers does Robin like?",
        PrivatePurpose(
            occasion="40th birthday",
            occasion_date="June 3",
            budget=120,
            candidate_ideas=["orchid terrarium", "botanical garden membership"],
            notes="Planning a surprise dinner with her sister",
        ),
    ),
    "demo_binoculars": (
        "Does Alex already own binoculars?",
        PrivatePurpose(
            occasion="birthday",
            occasion_date="next week",
            budget="$100",
            candidate_ideas=["compact binoculars", "bird field guide"],
        ),
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_live_muse_spark_routing(case: str) -> None:
    client = MuseSparkClient.from_settings(Settings())
    if not client.available:
        pytest.skip("Muse Spark is not configured (FIREWORKS_API_KEY / provider)")
    requested, purpose = CASES[case]
    candidates = (
        Candidate(id="r1", delegation_id=uuid4(), agent_id=uuid4(), label="Muse"),
        Candidate(id="r2", delegation_id=uuid4(), agent_id=uuid4(), label="Claude"),
    )

    # Call the client directly once so a provider failure is visible, not hidden by fallback.
    raw = await client.complete_json(
        system=SYSTEM_PROMPT, user=_build_user_prompt(requested, purpose, candidates)
    )
    assert isinstance(raw, dict)

    decision = await route_question(
        requested_information=requested,
        purpose=purpose,
        candidates=candidates,
        client=client,
        identity_terms=IDENTITY,
    )
    print(f"live routing case={case} path={decision.path} reason={decision.fallback_reason}")
    assert decision.respondents and set(decision.respondents) <= set(candidates)
    assert decision.minimized_question
    assert not publishable_question_problems(
        decision.minimized_question,
        requested_information=requested,
        purpose=purpose,
        identity_terms=IDENTITY,
    )
