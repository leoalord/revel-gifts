"""In-memory model-v1 OpenFGA fake and synthetic principals for F4 authorization tests."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from revel.auth.fga import RelationTuple


@dataclass(frozen=True)
class Principal:
    """Stand-in for F2's revel.auth.identity.Principal."""

    user_id: UUID
    agent_id: UUID | None = None
    delegation_id: UUID | None = None
    via: Literal["mcp", "api", "ui"] = "ui"


class FakeFga:
    """In-memory FgaClient evaluating model v1 (contribute/view_only_me = owner or owner_agent).

    Mirrors the real client's idempotency: duplicate writes and missing deletes are ignored.
    """

    def __init__(self, *, configured: bool = True) -> None:
        self.tuples: set[RelationTuple] = set()
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
        self.tuples.update(tuples)

    async def delete_tuples(self, tuples: Iterable[RelationTuple]) -> None:
        self._call("delete")
        self.tuples.difference_update(tuples)

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        self._call("check")
        if relation in ("contribute", "view_only_me"):
            return RelationTuple(user, "owner", object) in self.tuples or (
                RelationTuple(user, "owner_agent", object) in self.tuples
            )
        return RelationTuple(user, relation, object) in self.tuples

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
