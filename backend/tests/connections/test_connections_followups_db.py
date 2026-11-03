"""#15 follow-ups: the requester needs a handle; incoming requests name the acting assistant."""

import pytest
from authz_fakes import FakeFga
from conn_helpers import Person, add_person
from sqlalchemy.orm import Session
from test_connections_service_db import rows

from revel.api.connections import error_response
from revel.auth.identity import Principal
from revel.db.models import Agent, AgentDelegation
from revel.services import connections as svc

pytestmark = pytest.mark.anyio


async def test_requester_without_handle_is_refused_for_every_target(
    session: Session, sessions, fake_fga: FakeFga, monkeypatch: pytest.MonkeyPatch
) -> None:
    nohandle = add_person(session, "nohandle", handle=False)
    add_person(session, "alex")
    add_person(session, "hidden", discoverable=False)
    outcomes = set()
    for handle in ("alex_demo", "hidden_demo", "nobody_here", "x"):
        with pytest.raises(svc.HandleRequired) as exc:
            await svc.lookup_and_request(nohandle.ui, handle=handle, sessions=sessions)
        outcomes.add(exc.value.args)
    assert outcomes == {("Set your handle before sending a connection request.",)}
    assert rows(session) == []
    response = error_response(svc.HandleRequired())
    assert response.status_code == 409
    assert response.body == (
        b'{"detail":"Set your handle before sending a connection request.","code":"invalid_state"}'
    )

    # Rate limits are still checked first.
    monkeypatch.setattr(svc, "LOOKUP_LIMIT", 0)
    with pytest.raises(svc.RateLimited):
        await svc.lookup_and_request(nohandle.ui, handle="alex_demo", sessions=sessions)
    monkeypatch.undo()

    await svc.set_handle(nohandle.ui, handle="now_named", sessions=sessions)
    created = await svc.lookup_and_request(nohandle.ui, handle="alex_demo", sessions=sessions)
    assert created.created


async def test_incoming_request_shows_assistant_label(
    session: Session, sessions, fake_fga: FakeFga
) -> None:
    sam: Person = add_person(session, "sam")
    alex = add_person(session, "alex")
    dots = Agent(oauth_client_id="https://chatgpt.com/oauth/client.json", name=None)
    session.add(dots)
    session.flush()
    delegation = AgentDelegation(user_id=sam.user.id, agent_id=dots.id)
    session.add(delegation)
    session.commit()
    via_dots = Principal(
        user_id=sam.user.id, agent_id=dots.id, delegation_id=delegation.id, via="mcp"
    )
    await svc.lookup_and_request(via_dots, handle="alex_demo", sessions=sessions)
    (incoming,) = await svc.list_incoming(alex.ui, sessions=sessions)
    assert incoming.requester.assistant == svc.AssistantInfo(
        name="chatgpt.com", client_id="https://chatgpt.com/oauth/client.json"
    )
