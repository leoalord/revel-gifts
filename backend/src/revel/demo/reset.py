"""Demo reset (F10, RG-45 / Build Tasks #23): restore the three demo personas to a known baseline.

Run (dry run by default; nothing is written)::

    python -m revel.demo.reset --target dev
    python -m revel.demo.reset --target dev --apply --no-active-hold

On dev it runs through the ``revel-fga-admin`` Cloud Run job
(``--args=-m,revel.demo.reset,--target,dev``), which carries ``PUBLIC_BASE_URL``,
``DATABASE_URL`` and the OpenFGA settings.

Safety:

- ``--target dev`` is required and must equal ``PUBLIC_BASE_URL`` (``DEMO_TARGETS``).
- ``--apply`` also needs ``--no-active-hold``: the operator confirms no verification hold is
  announced (a reset changes shared test state).
- Only the allowlisted handles (``revel.demo.seed.DEMO_HANDLES``) are in scope, each pinned to its
  expected user id (``DEMO_USER_IDS``). A missing handle, a handle resolving to another user,
  a missing recipient profile, a connections plan with errors (e.g. a missing or wrong-tier
  baseline: the run would fail, so the dry run and an apply both refuse), or any row in the
  computed clear scope that names a user outside the allowlist aborts before any write (fail
  closed). Connection requests with a non-demo party are left alone and counted.
- Output carries counts, request ids, claim ids and seed keys only: never fact text, emails,
  tokens or names. Uses the shared ``get_sessionmaker`` (no extra engines or pools).

Phases (``--apply``):

A. Clear, ONE transaction (``lock_timeout`` 3s): the demo questions (profile or requester in the
   demo scope) with their answer evidence, answers, events and assignments; the demo profiles'
   claim events and claims (leaf-first through the ``supersedes_id`` RESTRICT self-FK, bounded);
   every contribution policy version except each demo profile's earliest. Hard deletes only.
   Users, agents, delegations, delegation sessions, handles and profiles are never touched.
B. Connections, through F6's service functions only (each its own transaction; F4's hook
   contract): among the demo users, pending requests are declined by the target (cancelled by
   the requester if only the requester's list shows them), approved ones other than the
   Leo <-> Jordan Trusted Circle baseline are revoked by the target, blocks are unblocked. The
   baseline is never created (a missing one refuses before Phase A). Then ``fga_reconcile``
   (window 0, no prune).
C. Re-seed the recipient's prepared facts through F5's ``add_facts`` as the owner (browser UI
   principal) and print the disclosure manifest (seed key, claim id, audience, "prepared").

Verify: before/after counts and read-only authorization checks through F4 (Sam denied on
Leo's profile; Jordan at trusted_circle sees the Trusted Circle seed); no demo questions remain;
only the baseline connection is live among the demo users. A database error in a phase prints
the phase and exception class only (that phase's transaction rolls back) and exits 1. Exit
codes: 0 success, 1 a phase or verification failed, 2 refused (nothing written).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TextIO
from uuid import UUID

import anyio.to_thread
from sqlalchemy import delete, exists, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker

from revel.auth import authz, fga_reconcile
from revel.auth.authz import AuthorizationDenied, ViewerTier
from revel.auth.fga import FgaClient, FgaConfigurationError, create_fga_client
from revel.auth.identity import Principal
from revel.auth.visibility import SharedClaim, visible_claims
from revel.db.enums import LIVE_CONNECTION_REQUEST_STATUSES, QuestionStatus
from revel.db.models import (
    Agent,
    AgentDelegation,
    AgentDelegationSession,
    Answer,
    AnswerEvidence,
    Assignment,
    Claim,
    ClaimEvent,
    ConnectionRequest,
    ContributionPolicy,
    Handle,
    Profile,
    Question,
    QuestionEvent,
    User,
)
from revel.demo.seed import (
    BASELINE_REQUESTER,
    BASELINE_TARGET,
    BASELINE_TIER,
    DEMO_HANDLES,
    DEMO_USER_IDS,
    JORDAN_HANDLE,
    KEPT_CONNECTIONS,
    PREPARED_LABEL,
    RECIPIENT_HANDLE,
    SAM_HANDLE,
    SEED_FACTS,
    TRUSTED_ONLY_SEED,
)
from revel.services import connections, profiles
from revel.settings import Settings, get_settings

# --target name -> the PUBLIC_BASE_URL it must match.
DEMO_TARGETS: dict[str, str] = {"dev": "https://api.revel.gifts"}
LOCK_TIMEOUT = "3s"
RECONCILE_ATTEMPTS = 3

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

MANIFEST_BEGIN = "----- BEGIN DEMO DISCLOSURE MANIFEST (JSON) -----"
MANIFEST_END = "----- END DEMO DISCLOSURE MANIFEST -----"

SessionFactory = Callable[[], sessionmaker[Session]]


def _default_sessionmaker() -> sessionmaker[Session]:
    from revel.db.session import get_sessionmaker

    return get_sessionmaker()


class ResetRefused(Exception):
    """A safety check failed; nothing was written. The message carries no user payloads."""


# --- Scope ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoScope:
    users: dict[str, UUID]  # handle -> user id (all three)
    profiles: dict[str, UUID]  # handle -> profile id (users that have one; recipient required)

    @property
    def user_ids(self) -> frozenset[UUID]:
        return frozenset(self.users.values())

    @property
    def profile_ids(self) -> frozenset[UUID]:
        return frozenset(self.profiles.values())

    @property
    def recipient_profile(self) -> UUID:
        return self.profiles[RECIPIENT_HANDLE]

    def principal(self, handle: str) -> Principal:
        """The user's own browser-UI principal (the user acting directly, no delegation)."""
        return Principal(user_id=self.users[handle], agent_id=None, delegation_id=None, via="ui")


def resolve_scope(
    session: Session, expected_user_ids: Mapping[str, UUID] = DEMO_USER_IDS
) -> DemoScope:
    rows = session.execute(
        select(Handle.handle, Handle.user_id).where(Handle.handle.in_(DEMO_HANDLES))
    ).all()
    users = {h: u for h, u in rows}
    missing = [h for h in DEMO_HANDLES if h not in users]
    if missing:
        raise ResetRefused(f"demo handle(s) not found: {', '.join(missing)}")
    moved = [h for h in DEMO_HANDLES if users[h] != expected_user_ids.get(h)]
    if moved:
        raise ResetRefused(f"demo handle(s) resolve to an unexpected user: {', '.join(moved)}")
    by_user = dict(
        session.execute(
            select(Profile.user_id, Profile.id).where(Profile.user_id.in_(users.values()))
        ).all()
    )
    profs = {h: by_user[u] for h, u in users.items() if u in by_user}
    if RECIPIENT_HANDLE not in profs:
        raise ResetRefused(f"the recipient ({RECIPIENT_HANDLE}) has no profile")
    return DemoScope(users=users, profiles=profs)


# --- Phase A plan -----------------------------------------------------------------------------

# Deletion order (children first). Claims are deleted leaf-first separately.
CLEAR_TABLES = (
    "answer_evidence",
    "answers",
    "question_events",
    "assignments",
    "questions",
    "claim_events",
    "claims",
    "contribution_policies",
)


@dataclass
class ClearPlan:
    ids: dict[str, frozenset[UUID]]
    violations: dict[str, int]
    profiles_without_policy: int
    open_questions: int

    def counts(self) -> dict[str, int]:
        return {t: len(self.ids[t]) for t in CLEAR_TABLES}

    @property
    def violation_total(self) -> int:
        return sum(self.violations.values())


def plan_clear(session: Session, scope: DemoScope) -> ClearPlan:
    """Read the rows Phase A would delete and every user they name (read-only)."""
    users, profs = scope.user_ids, scope.profile_ids
    v: Counter[str] = Counter()

    def outside(user_id: UUID | None) -> bool:
        return user_id is not None and user_id not in users

    q_rows = session.execute(
        select(Question.id, Question.profile_id, Question.requester_user_id, Question.status).where(
            or_(Question.profile_id.in_(profs), Question.requester_user_id.in_(users))
        )
    ).all()
    qids = [r.id for r in q_rows]
    v["questions_about_non_demo_profile"] = sum(r.profile_id not in profs for r in q_rows)
    v["questions_by_non_demo_requester"] = sum(outside(r.requester_user_id) for r in q_rows)
    open_questions = sum(r.status == QuestionStatus.OPEN.value for r in q_rows)

    a_rows = session.execute(
        select(Assignment.id, AgentDelegation.user_id)
        .join(AgentDelegation, AgentDelegation.id == Assignment.respondent_delegation_id)
        .where(Assignment.question_id.in_(qids))
    ).all()
    v["assignments_to_non_demo_respondent"] = sum(outside(r.user_id) for r in a_rows)

    e_rows = session.execute(
        select(QuestionEvent.id, QuestionEvent.actor_user_id).where(
            QuestionEvent.question_id.in_(qids)
        )
    ).all()
    v["question_events_by_non_demo_actor"] = sum(outside(r.actor_user_id) for r in e_rows)

    ans_rows = session.execute(
        select(Answer.id, Answer.respondent_user_id).where(Answer.question_id.in_(qids))
    ).all()
    v["answers_by_non_demo_respondent"] = sum(outside(r.respondent_user_id) for r in ans_rows)
    ans_ids = [r.id for r in ans_rows]
    ev_ids = session.execute(
        select(AnswerEvidence.id).where(AnswerEvidence.answer_id.in_(ans_ids))
    ).scalars()
    ev_ids = frozenset(ev_ids)

    c_rows = session.execute(
        select(Claim.id, Claim.contributor_user_id).where(Claim.profile_id.in_(profs))
    ).all()
    v["claims_by_non_demo_contributor"] = sum(outside(r.contributor_user_id) for r in c_rows)
    claim_ids = [r.id for r in c_rows]
    # Evidence citing a demo claim from an answer outside the plan would block the delete.
    v["answer_evidence_outside_scope_citing_demo_claims"] = session.execute(
        select(func.count())
        .select_from(AnswerEvidence)
        .where(AnswerEvidence.claim_id.in_(claim_ids), AnswerEvidence.id.not_in(ev_ids))
    ).scalar_one()

    ce_rows = session.execute(
        select(ClaimEvent.id, ClaimEvent.actor_user_id).where(ClaimEvent.profile_id.in_(profs))
    ).all()
    v["claim_events_by_non_demo_actor"] = sum(outside(r.actor_user_id) for r in ce_rows)

    p_rows = session.execute(
        select(
            ContributionPolicy.id,
            ContributionPolicy.profile_id,
            ContributionPolicy.version,
            ContributionPolicy.approved_by_user_id,
        ).where(ContributionPolicy.profile_id.in_(profs))
    ).all()
    earliest: dict[UUID, int] = {}
    for r in p_rows:
        earliest[r.profile_id] = min(earliest.get(r.profile_id, r.version), r.version)
    extra = [r for r in p_rows if r.version != earliest[r.profile_id]]
    v["policies_approved_by_non_demo_user"] = sum(outside(r.approved_by_user_id) for r in extra)

    return ClearPlan(
        ids={
            "answer_evidence": ev_ids,
            "answers": frozenset(ans_ids),
            "question_events": frozenset(r.id for r in e_rows),
            "assignments": frozenset(r.id for r in a_rows),
            "questions": frozenset(qids),
            "claim_events": frozenset(r.id for r in ce_rows),
            "claims": frozenset(claim_ids),
            "contribution_policies": frozenset(r.id for r in extra),
        },
        violations={k: n for k, n in v.items() if n},
        profiles_without_policy=len(profs - earliest.keys()),
        open_questions=open_questions,
    )


def _delete_ids(session: Session, model: Any, ids: frozenset[UUID], table: str) -> int:
    if not ids:
        return 0
    deleted = session.execute(
        delete(model)
        .where(model.id.in_(ids))
        .returning(model.id)
        .execution_options(synchronize_session=False)
    ).scalars()
    n = len(list(deleted))
    if n != len(ids):
        raise ResetRefused(f"{table}: deleted {n} of {len(ids)} planned rows; rolled back")
    return n


def _delete_claims_leaf_first(session: Session, ids: frozenset[UUID]) -> tuple[int, int]:
    """Delete claims whose replacement (``supersedes_id`` child) is already gone, repeatedly.

    Bounded: at most ``len(ids) + 1`` rounds; a round that deletes nothing while planned rows
    remain aborts (a child outside the plan still supersedes one of them)."""
    remaining = set(ids)
    rounds = 0
    child = aliased(Claim)
    while remaining:
        rounds += 1
        if rounds > len(ids) + 1:
            raise ResetRefused("claims: supersede chain did not converge; rolled back")
        deleted = set(
            session.execute(
                delete(Claim)
                .where(
                    Claim.id.in_(remaining),
                    ~exists().where(child.supersedes_id == Claim.id),
                )
                .returning(Claim.id)
                .execution_options(synchronize_session=False)
            ).scalars()
        )
        if not deleted:
            raise ResetRefused("claims: no progress (blocked by a superseding claim); rolled back")
        remaining -= deleted
    return len(ids), rounds


def apply_clear(session: Session, scope: DemoScope, expected: ClearPlan) -> dict[str, int]:
    """Phase A in the caller's open transaction. Re-plans under the transaction and refuses if
    the scope changed or names a non-demo user; the caller commits."""
    session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    plan = plan_clear(session, scope)
    if plan.violation_total:
        raise ResetRefused("scope check failed under the clear transaction; rolled back")
    if plan.ids != expected.ids:
        raise ResetRefused("demo scope changed since planning; re-run the reset")
    ids = plan.ids
    done = {
        "answer_evidence": _delete_ids(
            session, AnswerEvidence, ids["answer_evidence"], "answer_evidence"
        ),
        "answers": _delete_ids(session, Answer, ids["answers"], "answers"),
        "question_events": _delete_ids(
            session, QuestionEvent, ids["question_events"], "question_events"
        ),
        "assignments": _delete_ids(session, Assignment, ids["assignments"], "assignments"),
        "questions": _delete_ids(session, Question, ids["questions"], "questions"),
        "claim_events": _delete_ids(session, ClaimEvent, ids["claim_events"], "claim_events"),
    }
    done["claims"], done["claim_delete_rounds"] = _delete_claims_leaf_first(session, ids["claims"])
    done["contribution_policies"] = _delete_ids(
        session, ContributionPolicy, ids["contribution_policies"], "contribution_policies"
    )
    return done


# --- Phase B plan (connections) ---------------------------------------------------------------


@dataclass(frozen=True)
class ConnAction:
    action: str  # decline | cancel | revoke | unblock
    request_id: UUID
    actor: str  # handle of the demo user acting
    requester: str
    target: str


@dataclass
class ConnPlan:
    actions: list[ConnAction] = field(default_factory=list)
    kept: list[UUID] = field(default_factory=list)
    non_demo: int = 0
    errors: list[str] = field(default_factory=list)


async def plan_connections(scope: DemoScope, sessions: SessionFactory) -> ConnPlan:
    """Read each demo user's requests through F6's list functions (read-only) and plan."""
    # request id -> (requester handle, target handle, status, tier, seen_by_target)
    seen: dict[UUID, dict[str, Any]] = {}

    def note(rid: UUID, requester: str | None, target: str | None, status: str, **kw: Any) -> None:
        row = seen.setdefault(
            rid, {"requester": requester, "target": target, "status": status, "tier": None}
        )
        row.update(kw)

    for handle in DEMO_HANDLES:
        me = scope.principal(handle)
        incoming = await connections.list_incoming(me, sessions=sessions)
        outgoing = await connections.list_outgoing(me, sessions=sessions)
        approved = await connections.list_connections(me, sessions=sessions)
        blocked = await connections.list_blocked(me, sessions=sessions)
        if any(len(x) >= connections.LIST_LIMIT for x in (incoming, outgoing, approved, blocked)):
            raise ResetRefused(f"{handle}: a connection list hit its limit; cannot see all rows")
        for r in incoming:
            note(r.id, r.requester.handle, handle, r.status, seen_by_target=True)
        for o in outgoing:
            note(o.id, handle, o.target.handle, o.status)
        for c in approved:
            note(c.request_id, c.person.handle, handle, "approved", tier=c.tier)
        for b in blocked:
            note(b.request_id, b.person.handle, handle, "blocked", seen_by_target=True)

    plan = ConnPlan()
    baseline_ok = False
    for rid, row in sorted(seen.items(), key=lambda kv: str(kv[0])):
        req, tgt, status, tier = row["requester"], row["target"], row["status"], row["tier"]
        if req not in DEMO_HANDLES or tgt not in DEMO_HANDLES:
            plan.non_demo += 1
            continue
        if status == "approved":
            if tier is None:  # only the requester's list saw it; tier unknown
                plan.errors.append(f"approved request {rid}: tier not visible to the target")
            elif (req, tgt, tier) in KEPT_CONNECTIONS:
                plan.kept.append(rid)
                if (req, tgt) == (BASELINE_REQUESTER, BASELINE_TARGET):
                    baseline_ok = True
            elif (req, tgt) == (BASELINE_REQUESTER, BASELINE_TARGET):
                # Never downgrade/revoke the baseline pair here: it needs a live re-approval.
                plan.errors.append(
                    f"baseline request {rid} is approved at {tier}, not {BASELINE_TIER.value}; "
                    "left unchanged"
                )
            else:
                plan.actions.append(ConnAction("revoke", rid, tgt, req, tgt))
        elif status == "pending":
            if row.get("seen_by_target"):
                plan.actions.append(ConnAction("decline", rid, tgt, req, tgt))
            else:
                plan.actions.append(ConnAction("cancel", rid, req, req, tgt))
        elif status == "blocked":
            plan.actions.append(ConnAction("unblock", rid, tgt, req, tgt))
    if not baseline_ok:
        plan.errors.append(
            f"baseline missing: {BASELINE_REQUESTER} -> {BASELINE_TARGET} approved at "
            f"{BASELINE_TIER.value} (needs a live approval; not created)"
        )
    return plan


_ACTIONS: dict[str, Callable[..., Any]] = {
    "decline": connections.decline,
    "cancel": connections.cancel_request,
    "revoke": connections.revoke,
    "unblock": connections.unblock,
}


async def apply_connections(
    scope: DemoScope, plan: ConnPlan, sessions: SessionFactory, out: TextIO
) -> list[str]:
    errors: list[str] = []
    for a in plan.actions:
        fn = _ACTIONS[a.action]
        try:
            await fn(scope.principal(a.actor), a.request_id, sessions=sessions)
        except connections.ConnectionsError as exc:
            errors.append(f"{a.action} {a.request_id} failed ({type(exc).__name__})")
            continue
        print(f"  {a.action}: {a.request_id} ({a.requester} -> {a.target}) done", file=out)
    return errors


async def run_reconcile(fga: FgaClient, sessions: SessionFactory, out: TextIO) -> str | None:
    """``fga_reconcile`` in-process, window 0, never ``--prune``. Returns an error or ``None``."""
    for attempt in range(1, RECONCILE_ATTEMPTS + 1):
        try:
            summary = await fga_reconcile.run(
                prune=False, window=timedelta(0), fga=fga, sessionmaker_provider=sessions
            )
        except FgaConfigurationError:
            return "reconcile: OpenFGA is not configured"
        except fga_reconcile.BarrierNotApplied:
            print(f"  reconcile attempt {attempt}: barrier not applied; re-running", file=out)
            continue
        print(
            f"  reconcile ok (attempt {attempt}): connection tuples {summary.connection_tuples}, "
            f"blocked pairs {summary.blocked_pairs}, public profiles {summary.public_profiles}",
            file=out,
        )
        return None
    return f"reconcile: barrier not applied after {RECONCILE_ATTEMPTS} attempts"


# --- Phase C (re-seed) ------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    seed_key: str
    claim_id: UUID
    audience: str
    label: str = PREPARED_LABEL


async def reseed(
    scope: DemoScope, sessions: SessionFactory
) -> tuple[list[ManifestEntry], list[str]]:
    session = sessions()()
    try:
        result = await profiles.add_facts(
            session, scope.principal(RECIPIENT_HANDLE), [s.as_fact_input() for s in SEED_FACTS]
        )
    finally:
        await anyio.to_thread.run_sync(session.close)
    manifest: list[ManifestEntry] = []
    errors: list[str] = []
    by_index = {o.index: o for o in result.results}
    for i, seed in enumerate(SEED_FACTS):
        o = by_index.get(i)
        if o is None or o.outcome != "accepted" or o.claim_id is None:
            outcome = "missing" if o is None else o.outcome
            reason = "" if o is None or not o.reason_code else f" ({o.reason_code})"
            errors.append(f"seed {seed.key}: {outcome}{reason}")
            continue
        if o.audience is None or o.audience.value != seed.audience.value:
            errors.append(f"seed {seed.key}: audience mismatch")
        manifest.append(ManifestEntry(seed.key, o.claim_id, seed.audience.value))
    return manifest, errors


# --- Counts and verification ------------------------------------------------------------------


def demo_counts(session: Session, scope: DemoScope) -> dict[str, int]:
    """Per-table row counts for the demo scope (read-only)."""
    users, profs = list(scope.user_ids), list(scope.profile_ids)
    qids = select(Question.id).where(
        or_(Question.profile_id.in_(profs), Question.requester_user_id.in_(users))
    )
    ans = select(Answer.id).where(Answer.question_id.in_(qids))
    dels = select(AgentDelegation.id).where(AgentDelegation.user_id.in_(users))
    live = [s.value for s in LIVE_CONNECTION_REQUEST_STATUSES]

    def count(stmt: Any) -> int:
        return int(session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())

    return {
        "users": count(select(User.id).where(User.id.in_(users))),
        "handles": count(select(Handle.user_id).where(Handle.user_id.in_(users))),
        "profiles": count(select(Profile.id).where(Profile.user_id.in_(users))),
        "agent_delegations": count(dels),
        "agent_delegation_sessions": count(
            select(AgentDelegationSession.sid).where(AgentDelegationSession.delegation_id.in_(dels))
        ),
        "agents": count(
            select(Agent.id).where(
                Agent.id.in_(
                    select(AgentDelegation.agent_id).where(AgentDelegation.user_id.in_(users))
                )
            )
        ),
        "questions": count(qids),
        "open_questions": count(
            select(Question.id).where(
                Question.id.in_(qids), Question.status == QuestionStatus.OPEN.value
            )
        ),
        "assignments": count(select(Assignment.id).where(Assignment.question_id.in_(qids))),
        "question_events": count(
            select(QuestionEvent.id).where(QuestionEvent.question_id.in_(qids))
        ),
        "answers": count(ans),
        "answer_evidence": count(
            select(AnswerEvidence.id).where(AnswerEvidence.answer_id.in_(ans))
        ),
        "claims": count(select(Claim.id).where(Claim.profile_id.in_(profs))),
        "claim_events": count(select(ClaimEvent.id).where(ClaimEvent.profile_id.in_(profs))),
        "contribution_policies": count(
            select(ContributionPolicy.id).where(ContributionPolicy.profile_id.in_(profs))
        ),
        "live_requests_among_demo": count(
            select(ConnectionRequest.id).where(
                ConnectionRequest.requester_user_id.in_(users),
                ConnectionRequest.target_user_id.in_(users),
                ConnectionRequest.status.in_(live),
            )
        ),
    }


def _live_sam_leo(session: Session, scope: DemoScope) -> int:
    sam, leo = scope.users[SAM_HANDLE], scope.users[RECIPIENT_HANDLE]
    live = [s.value for s in LIVE_CONNECTION_REQUEST_STATUSES]
    return int(
        session.execute(
            select(func.count())
            .select_from(ConnectionRequest)
            .where(
                or_(
                    (ConnectionRequest.requester_user_id == sam)
                    & (ConnectionRequest.target_user_id == leo),
                    (ConnectionRequest.requester_user_id == leo)
                    & (ConnectionRequest.target_user_id == sam),
                ),
                ConnectionRequest.status.in_(live),
            )
        ).scalar_one()
    )


async def verify(
    scope: DemoScope,
    sessions: SessionFactory,
    fga: FgaClient,
    manifest: Sequence[ManifestEntry],
    kept: Sequence[UUID],
    out: TextIO,
) -> list[str]:
    """Read-only checks through F4's authorization service. Returns failures."""
    failures: list[str] = []
    profile_id = scope.recipient_profile
    trusted_claim = next((m.claim_id for m in manifest if m.seed_key == TRUSTED_ONLY_SEED), None)
    with sessions()() as session:
        try:
            seen = await visible_claims(session, scope.principal(SAM_HANDLE), profile_id, fga=fga)
            failures.append(f"{SAM_HANDLE} can read the recipient profile (tier {seen.tier})")
        except AuthorizationDenied:
            print(f"  {SAM_HANDLE} read of recipient profile: denied (ok)", file=out)
        try:
            seen = await visible_claims(
                session, scope.principal(JORDAN_HANDLE), profile_id, fga=fga
            )
        except AuthorizationDenied:
            failures.append(f"{JORDAN_HANDLE} is denied on the recipient profile")
        else:
            ids = {c.id for c in seen.claims if isinstance(c, SharedClaim)}
            if seen.tier is not ViewerTier.trusted_circle:
                failures.append(f"{JORDAN_HANDLE} tier is {seen.tier}, not trusted_circle")
            elif trusted_claim is None or trusted_claim not in ids:
                failures.append(f"{JORDAN_HANDLE} cannot see the {TRUSTED_ONLY_SEED} seed")
            else:
                print(
                    f"  {JORDAN_HANDLE} read of recipient profile: permitted at trusted_circle, "
                    f"{len(ids)} facts visible incl. {TRUSTED_ONLY_SEED} (ok)",
                    file=out,
                )

        def sql_checks() -> tuple[dict[str, int], int]:
            try:
                return demo_counts(session, scope), _live_sam_leo(session, scope)
            finally:
                session.rollback()

        counts, sam_leo = await anyio.to_thread.run_sync(sql_checks)
    if counts["open_questions"] or counts["questions"]:
        failures.append(f"demo questions remain: {counts['questions']}")
    if sam_leo:
        failures.append(f"{sam_leo} live request(s) remain between {SAM_HANDLE} and the recipient")
    if counts["live_requests_among_demo"] != len(kept):
        failures.append(
            f"{counts['live_requests_among_demo']} live request(s) among the demo users; "
            f"expected only the {len(kept)} kept baseline row(s)"
        )
    return failures


# --- Output helpers ---------------------------------------------------------------------------


def _print_counts(title: str, counts: dict[str, int], out: TextIO) -> None:
    print(title, file=out)
    for k, n in counts.items():
        print(f"  {k}: {n}", file=out)


def _print_compare(before: dict[str, int], after: dict[str, int], out: TextIO) -> None:
    print("Counts (demo scope, before -> after):", file=out)
    for k in before:
        print(f"  {k}: {before[k]} -> {after.get(k)}", file=out)


def _print_conn_plan(plan: ConnPlan, out: TextIO) -> None:
    print("Connections plan (demo users only):", file=out)
    by_action = Counter(a.action for a in plan.actions)
    for name in ("decline", "cancel", "revoke", "unblock"):
        print(f"  {name}: {by_action.get(name, 0)}", file=out)
    for a in plan.actions:
        print(f"    {a.action} {a.request_id} ({a.requester} -> {a.target}) by {a.actor}", file=out)
    print(f"  kept baseline: {len(plan.kept)} {[str(k) for k in plan.kept]}", file=out)
    print(f"  requests with a non-demo party (left alone): {plan.non_demo}", file=out)
    for e in plan.errors:
        print(f"  ERROR: {e}", file=out)


def manifest_json(manifest: Iterable[ManifestEntry]) -> str:
    return json.dumps(
        {
            "task": "RG-45",
            "profile_handle": RECIPIENT_HANDLE,
            "facts": [
                {
                    "seed_key": m.seed_key,
                    "claim_id": str(m.claim_id),
                    "audience": m.audience,
                    "label": m.label,
                }
                for m in manifest
            ],
        },
        indent=2,
    )


# --- Orchestration ----------------------------------------------------------------------------


def check_target(target: str, settings: Settings) -> None:
    expected = DEMO_TARGETS.get(target)
    if expected is None:
        raise ResetRefused(f"unknown --target {target!r}")
    if settings.public_base_url.rstrip("/") != expected:
        raise ResetRefused(f"--target {target} does not match PUBLIC_BASE_URL; refusing")


async def run(
    *,
    target: str,
    apply: bool,
    no_active_hold: bool,
    settings: Settings | None = None,
    sessions: SessionFactory = _default_sessionmaker,
    fga: FgaClient | None = None,
    out: TextIO = sys.stdout,
    expected_user_ids: Mapping[str, UUID] = DEMO_USER_IDS,
) -> int:
    settings = settings or get_settings()
    try:
        check_target(target, settings)
        if apply and not no_active_hold:
            raise ResetRefused(
                "--apply needs --no-active-hold (confirm no verification hold is announced)"
            )
    except ResetRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    factory = sessions()

    def read_plan() -> tuple[DemoScope, ClearPlan, dict[str, int]]:
        with factory() as session, session.begin():
            session.execute(text("SET TRANSACTION READ ONLY"))
            scope = resolve_scope(session, expected_user_ids)
            return scope, plan_clear(session, scope), demo_counts(session, scope)

    try:
        scope, clear_plan, before = await anyio.to_thread.run_sync(read_plan)
        conn_plan = await plan_connections(scope, sessions)
    except ResetRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"Demo reset ({'APPLY' if apply else 'DRY RUN'}), target {target}", file=out)
    _print_counts("Clear plan (rows to delete):", clear_plan.counts(), out)
    print(f"  open questions among them: {clear_plan.open_questions}", file=out)
    print(f"  demo profiles without a policy: {clear_plan.profiles_without_policy}", file=out)
    if clear_plan.profiles_without_policy:
        print("  NOTE: a demo profile has no contribution policy (kept going)", file=out)
    _print_conn_plan(conn_plan, out)
    print("Seeds to add (prepared):", file=out)
    for s in SEED_FACTS:
        print(f"  {s.key}: category {s.category}, audience {s.audience.value}", file=out)
    if clear_plan.violation_total:
        print("Scope check: FAIL (rows name a user outside the demo allowlist)", file=out)
        for k, n in sorted(clear_plan.violations.items()):
            print(f"  {k}: {n}", file=out)
        print("REFUSED: scope check failed; nothing written", file=sys.stderr)
        return EXIT_REFUSED
    print("Scope check: PASS (only the three demo users are touched)", file=out)
    if conn_plan.errors:
        # E.g. a missing or wrong-tier baseline: the run would fail, so nothing destructive runs.
        print("Connections check: FAIL (see ERROR lines above)", file=out)
        print("REFUSED: connections plan has errors; nothing written", file=sys.stderr)
        return EXIT_REFUSED
    print("Connections check: PASS (baseline present)", file=out)

    if not apply:
        print("Dry run: no writes made. Re-run with --apply --no-active-hold.", file=out)
        return EXIT_OK

    own_client = fga is None
    client = fga if fga is not None else create_fga_client(settings)
    if not client.configured:
        print("REFUSED: OpenFGA is not configured; --apply needs it", file=sys.stderr)
        if own_client:
            await client.close()
        return EXIT_REFUSED
    authz.configure_authz(client)
    errors: list[str] = []
    phase = "A (clear)"
    try:
        # Phase A: one transaction, committed before Phase C.
        def clear() -> dict[str, int]:
            with factory() as session, session.begin():
                return apply_clear(session, scope, clear_plan)

        try:
            done = await anyio.to_thread.run_sync(clear)
        except ResetRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        _print_counts("Phase A: deleted", done, out)

        # Phase B: F6 service calls (own transactions), then reconcile.
        phase = "B (connections)"
        print("Phase B: connections", file=out)
        errors += await apply_connections(scope, conn_plan, sessions, out)
        reconcile_error = await run_reconcile(client, sessions, out)
        if reconcile_error:
            errors.append(reconcile_error)

        # Phase C: re-seed through F5.
        phase = "C (re-seed)"
        print("Phase C: re-seed", file=out)
        manifest, seed_errors = await reseed(scope, sessions)
        errors += seed_errors
        print("Disclosure manifest (prepared facts; later assistant facts are live):", file=out)
        for m in manifest:
            print(f"  {m.seed_key}: claim {m.claim_id}, audience {m.audience}, {m.label}", file=out)

        # Verify.
        phase = "verify"
        print("Verify:", file=out)
        failures = await verify(scope, sessions, client, manifest, conn_plan.kept, out)

        def after_counts() -> dict[str, int]:
            with factory() as session:
                return demo_counts(session, scope)

        _print_compare(before, await anyio.to_thread.run_sync(after_counts), out)
        for e in errors:
            print(f"ERROR: {e}", file=out)
        for f in failures:
            print(f"VERIFY FAILED: {f}", file=out)
        print(MANIFEST_BEGIN, file=out)
        print(manifest_json(manifest), file=out)
        print(MANIFEST_END, file=out)
        return EXIT_FAILED if errors or failures else EXIT_OK
    except (SQLAlchemyError, profiles.ProfileServiceError) as exc:
        # Class name only (no row data). The failing phase's transaction was rolled back;
        # earlier phases stay committed.
        message = f"ERROR: phase {phase} failed ({type(exc).__name__}); its transaction rolled back"
        print(message, file=out)
        print(message, file=sys.stderr)
        return EXIT_FAILED
    finally:
        authz.reset_authz(client)
        if own_client:
            await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m revel.demo.reset",
        description="Restore the demo personas to the recording baseline (dry run by default).",
    )
    parser.add_argument("--target", required=True, choices=sorted(DEMO_TARGETS))
    parser.add_argument("--apply", action="store_true", help="perform the writes")
    parser.add_argument(
        "--no-active-hold",
        action="store_true",
        help="confirm no verification hold is announced (required with --apply)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run(target=args.target, apply=args.apply, no_active_hold=args.no_active_hold)
    )


if __name__ == "__main__":
    raise SystemExit(main())
