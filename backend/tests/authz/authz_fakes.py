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
    via: Literal["mcp", "api", "ui"] = "api"


class FakeFga:
    """In-memory FgaClient evaluating model v1 (contribute/view_only_me = owner or owner_agent).

    Mirrors the real client's idempotency: duplicate writes and missing deletes are ignored.
    """

    def __init__(self) -> None:
        self.tuples: set[RelationTuple] = set()
        self.calls: list[str] = []
        self.closed = False

    async def write_tuples(self, tuples: Iterable[RelationTuple]) -> None:
        self.calls.append("write")
        self.tuples.update(tuples)

    async def delete_tuples(self, tuples: Iterable[RelationTuple]) -> None:
        self.calls.append("delete")
        self.tuples.difference_update(tuples)

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        self.calls.append("check")
        if relation in ("contribute", "view_only_me"):
            return RelationTuple(user, "owner", object) in self.tuples or (
                RelationTuple(user, "owner_agent", object) in self.tuples
            )
        return RelationTuple(user, relation, object) in self.tuples

    async def read_tuples(
        self, *, user: str, relation: str, object_type: str
    ) -> list[RelationTuple]:
        self.calls.append("read")
        return [
            t
            for t in self.tuples
            if t.user == user and t.relation == relation and t.object.startswith(f"{object_type}:")
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
