"""In-memory model-v1 OpenFGA fake and synthetic principals for F4 authorization tests."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from revel.auth.fga import RelationTuple, StoredTuple


@dataclass(frozen=True)
class Principal:
    """Stand-in for F2's revel.auth.identity.Principal."""

    user_id: UUID
    agent_id: UUID | None = None
    delegation_id: UUID | None = None
    via: Literal["mcp", "api", "ui"] = "ui"


# Computed relations of model v2: never directly writable (no direct type restriction).
COMPUTED = frozenset(
    {
        "owner_agent",
        "contribute",
        "view_only_me",
        "trusted_member",
        "connection_member",
        "blocked_any",
        "view_trusted",
        "view_connections",
        "view_public",
        "requester_agent",
        "respond",
        "read_answer",
    }
)


class FakeFga:
    """In-memory FgaClient evaluating model v2 (``authorization/model.fga``).

    Mirrors the real client's idempotency: duplicate writes and missing deletes are ignored.
    The CLI tests (``authorization/tests``) and the ``openfga`` integration tests check the real
    model; this fake only keeps unit tests fast.
    """

    def __init__(self, *, configured: bool = True) -> None:
        self.tuples: set[RelationTuple] = set()
        # Write time per tuple (defaults to now when first written); tests may backdate entries.
        self.written_at: dict[RelationTuple, datetime] = {}
        self.calls: list[str] = []
        self.closed = False
        self.configured = configured
        # When set, every OpenFGA operation raises it (a configured but failing server).
        self.error: Exception | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.error is not None:
            raise self.error

    async def write_tuples(self, tuples: Iterable[RelationTuple]) -> None:
        self._call("write")
        tuples = list(tuples)
        if any(t.relation in COMPUTED for t in tuples):
            # Like the real server: computed relations have no direct type restriction.
            raise ValueError("computed relations are not directly writable")
        for t in tuples:
            if t not in self.tuples:
                self.written_at[t] = datetime.now(UTC)
            self.tuples.add(t)

    async def delete_tuples(self, tuples: Iterable[RelationTuple]) -> None:
        self._call("delete")
        self.tuples.difference_update(tuples)

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        self._call("check")
        return self._eval(user, relation, object)

    # --- model v2 evaluation ---

    def _has(self, user: str, relation: str, object: str) -> bool:
        return RelationTuple(user, relation, object) in self.tuples

    def _direct(self, user: str, relation: str, object: str) -> bool:
        if self._has(user, relation, object):
            return True
        if relation == "public_viewer":
            if user.startswith("user:") and self._has("user:*", relation, object):
                return True
            # platform:revel#connected_agent userset.
            return self._has("platform:revel#connected_agent", relation, object) and self._has(
                user, "connected_agent", "platform:revel"
            )
        return False

    def _from(self, user: str, relation: str, tupleset: str, object: str) -> bool:
        """``relation from tupleset`` on ``object``."""
        return any(
            self._eval(user, relation, t.user)
            for t in list(self.tuples)
            if t.relation == tupleset and t.object == object
        )

    def _eval(self, user: str, relation: str, object: str) -> bool:
        e = self._eval
        if object.startswith("profile:"):
            if relation == "owner_agent":
                return self._from(user, "delegate", "owner", object)
            if relation in ("contribute", "view_only_me"):
                return e(user, "owner", object) or e(user, "owner_agent", object)
            if relation in ("trusted_member", "connection_member", "blocked_any"):
                base = {"trusted_member": "trusted", "connection_member": "connection"}.get(
                    relation, "blocked"
                )
                return self._direct(user, base, object) or self._from(
                    user, "delegate", base, object
                )
            if relation == "view_trusted":
                return e(user, "view_only_me", object) or (
                    e(user, "trusted_member", object) and not e(user, "blocked_any", object)
                )
            if relation == "view_connections":
                return e(user, "view_trusted", object) or (
                    e(user, "connection_member", object) and not e(user, "blocked_any", object)
                )
            if relation == "view_public":
                return e(user, "view_connections", object) or (
                    self._direct(user, "public_viewer", object)
                    and not e(user, "blocked_any", object)
                )
        if object.startswith("question:"):
            if relation == "requester_agent":
                return self._from(user, "delegate", "requester", object)
            if relation == "respond":
                return self._from(user, "owner_agent", "about", object)
            if relation == "read_answer":
                return (
                    self._from(user, "owner", "about", object)
                    or self._from(user, "owner_agent", "about", object)
                    or (
                        (e(user, "requester", object) or e(user, "requester_agent", object))
                        and not self._from(user, "blocked_any", "about", object)
                    )
                )
        return self._direct(user, relation, object)

    async def read_tuples(
        self, *, relation: str | None = None, object: str | None = None, user: str | None = None
    ) -> list[RelationTuple]:
        self._call("read")
        return [
            t
            for t in self.tuples
            if (user is None or t.user == user)
            and (relation is None or t.relation == relation)
            and (
                object is None
                or t.object == object
                or (object.endswith(":") and t.object.startswith(object))
            )
        ]

    async def read_stored_tuples(self) -> list[StoredTuple]:
        self._call("read")
        epoch = datetime.fromtimestamp(0, UTC)
        return [StoredTuple(t, self.written_at.get(t, epoch)) for t in self.tuples]

    async def close(self) -> None:
        self.closed = True


@dataclass
class Person:
    user_id: UUID
    profile_id: UUID
    delegation_id: UUID

    def as_user(self) -> Principal:
        return Principal(user_id=self.user_id, via="ui")

    def as_agent(self, agent_id: UUID) -> Principal:
        return Principal(
            user_id=self.user_id, agent_id=agent_id, delegation_id=self.delegation_id, via="mcp"
        )


def make_person() -> Person:
    return Person(user_id=uuid4(), profile_id=uuid4(), delegation_id=uuid4())
