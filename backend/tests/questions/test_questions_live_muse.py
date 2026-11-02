"""Opt-in live Muse Spark check (real network call to Fireworks).

Skipped unless ``REVEL_LIVE_MUSE_SPARK=1``; needs ``FIREWORKS_API_KEY`` (and optionally
``FIREWORKS_MODEL``) in the environment or ``.env``. Synthetic purpose only. The test accepts
either path (the model may be rejected by the guard) but asserts the published decision is
always safe and in-set.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from revel.services.muse_spark import MuseSparkClient
from revel.services.question_routing import (
    Candidate,
    PrivatePurpose,
    find_spoilers,
    route_question,
    tokens,
)
from revel.settings import Settings

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("REVEL_LIVE_MUSE_SPARK") != "1", reason="REVEL_LIVE_MUSE_SPARK != 1"
    ),
]


async def test_live_muse_spark_routing() -> None:
    settings = Settings()
    client = MuseSparkClient.from_settings(settings)
    if not client.available:
        pytest.skip("Muse Spark is not configured (FIREWORKS_API_KEY / provider)")
    purpose = PrivatePurpose(
        occasion="40th birthday",
        occasion_date="June 3",
        budget=120,
        candidate_ideas=["orchid terrarium", "botanical garden membership"],
        notes="Planning a surprise dinner with her sister",
    )
    requested = "What kinds of plants or flowers does Robin like?"
    candidates = (
        Candidate(id="r1", delegation_id=uuid4(), agent_id=uuid4(), label="Muse"),
        Candidate(id="r2", delegation_id=uuid4(), agent_id=uuid4(), label="Claude"),
    )

    # Call the client directly once so a provider failure is visible, not hidden by fallback.
    from revel.services.question_routing import SYSTEM_PROMPT, _build_user_prompt

    raw = await client.complete_json(
        system=SYSTEM_PROMPT, user=_build_user_prompt(requested, purpose, candidates)
    )
    assert isinstance(raw, dict)

    decision = await route_question(
        requested_information=requested,
        purpose=purpose,
        candidates=candidates,
        client=client,
        identity_terms=("Jordan Example", "jordan"),
    )
    print(f"live routing path={decision.path} reason={decision.fallback_reason}")
    assert decision.respondents and set(decision.respondents) <= set(candidates)
    assert decision.minimized_question
    assert not find_spoilers(
        decision.minimized_question,
        purpose,
        all_values_by_token=True,
        allowed_tokens=tokens(requested),
        identity_terms=("Jordan Example", "jordan"),
        check_identity=True,
    )
