# F5 profiles and contributions: operation contract v1 (#9, RG-18)

Owner: revel-f5-profiles. Status: **provisional** until #9's reviewed merge (v1 as implemented on the RG-18 branch). Published before
implementation so F6, F7, F8 (#10) and F9 (#11) can draft against it; changes are announced.
Product policy lives in Notion (main page "Profile fact data model", Privacy page "Contribution
policy"); this file is the implementation contract only.

Code layout: shared behavior in `backend/src/revel/services/categories.py` (category list),
`backend/src/revel/services/profiles.py` (policy, contributions, owner reads, corrections; claim
reads only through F4's `visible_claims` / `owner_claims`) and
`backend/src/revel/services/claim_writes.py` (the only claim/claim_event write module, F4's guard
allowlist); thin adapters in `revel/api/profiles.py` and `revel/mcp/profile_tools.py`. Nothing
filters claims for visibility itself. Writes run only after F4's `authz.require_contribute`.

## Categories (F5-owned, stable ids)

| id | default audience | public-eligible after opt-in | `details` |
| --- | --- | --- | --- |
| `interest` | connections | yes | none |
| `style` | connections | yes | none |
| `favorite_brand` | connections | yes | none |
| `wish` | connections | yes | none |
| `dislike` | connections | no | none |
| `owned_item` | connections | no | none |
| `birthday` | connections | no | `{"month": 1-12, "day": 1-31}` (no year) |
| `location` | connections | no | `{"city"?, "region"?, "country"?}` (no street address) |
| `size` | trusted_circle | no | `{"item": str, "size": str}` |
| `life_event` | trusted_circle | no | none |
| `anniversary` | trusted_circle | no | `{"month", "day"}` |
| `sentimental` | trusted_circle | no | none |
| `diet_exclusion` | trusted_circle | no | `{"exclude": str}`; never the reason |
| `sensitive` (label "Sensitive (volunteered)") | only_me | no | none |

Always rejected: unknown categories, home/street address (`forbidden_category` for address
categories, `invalid_details`/`invalid_field` for address keys or street addresses in text) and
birth year (`forbidden_category` for birth-year categories, `invalid_details` for a year in
`details`, `invalid_field` for a year in a birthday statement). `details` rejects unknown keys.
Sensitive information (health, religion, politics, orientation, finances) is NOT rejected
outright: it goes in `sensitive`, whose `allowed_audiences` are `[only_me]` only (no setting or
edit can widen it), which needs `evidence_type = explicit` (otherwise rejected,
`sensitive_not_explicit`) and is ALWAYS `pending_review` until the owner approves it in the UI,
even when review is otherwise off and even for the owner's own entry. A category id naming a
sensitive topic (e.g. `health`) is `unknown_category` with a hint to use `sensitive`.
Health-related diet exclusions stay in `diet_exclusion`, with no reason.

## Contribution policy

Append-only versions (current = highest `version`). Fields:

- `public_opt_in` (default false), `share_provisional` (default true),
  `require_review_all` (default false), `enrichment_enabled` (default true);
- `category_settings: {<category>: {"audience"?: Audience, "require_review"?: bool}}`. A category
  audience may be anything from `only_me` up to its default; `public` only for public-eligible
  categories while `public_opt_in` is true.

**Effective default audience** for a category = its `category_settings` audience, else
`public` for public-eligible categories while opted in, else the category default.
**Maximum auto-accepted audience** = the effective default.

Policy writes are human-only (browser UI, `via="ui"`): widening access beyond the approved policy
needs owner approval. Agents read the policy. Writes take `expected_version` (the version the UI
read; `null` before the first); a mismatch returns **409** with the current policy. The first
approval creates version 1. Omitted fields keep their current values (`category_settings`, when
sent, replaces the whole map; when omitted it is carried forward, minus Public audiences if the
opt-in is withdrawn). F7's `questions_enabled` / `question_settings` are carried forward
unchanged. Tightening a category's effective audience also narrows that category's existing
live and pending facts to the new audience (explicit `audience_changed` events under the new
version); loosening never widens existing facts.

Provisional sharing defaults on: the policy response carries `provisional_caveat` (text for the
UI) and the owner can turn it off (`share_provisional: false`); F4's `visible_claims` then hides
provisional facts from everyone but the owner.

Opt-in changes call F4's `on_public_opt_in_changed`: withdrawal after the policy INSERT and
before commit (raises → rollback, 503); enabling after commit (a failed grant never fails the
committed change; `fga_reconcile` restores it). Serialized per profile: policy changes hold the
`profiles` row `FOR UPDATE` across the version computation; the post-commit grant re-takes that
lock, re-reads the current policy and grants only if it is still opted in. Contributions and fact
changes hold the row `FOR SHARE`, so they cite the current version.

## add_facts

Input per fact: `category`, `statement` (1-500 chars), `details?`, `audience?` (default =
effective default), `evidence_type` (`explicit|observed|inferred`), `confidence?`
(`low|medium|high|unknown`, default `unknown`), `observed_at?` (ISO date) with
`observed_precision` (`day|month|year|unknown`; `unknown` iff no date; defaults to `day` with a
date), `supersedes_id?`, `involves_others?` (bool, default false; input only, not stored).
Batch of 1-20; each item gets its own outcome, one bad item does not fail the batch (semantic
checks are per item; a structurally invalid body, e.g. a wrong enum value or a missing
`evidence_type`, is a 400 / MCP validation error for the whole request).

Outcome per item (`accepted | pending_review | rejected`):

- **rejected** with `reason_code`: `unknown_category`, `forbidden_category`, `invalid_details`,
  `invalid_field` (also: Public requested for a non-eligible category or without the opt-in, a
  non-Only-Me audience for `sensitive`), `enrichment_disabled` (agents only; the owner's own
  entries still work), `sensitive_not_explicit`, `supersedes_not_found` (not a live fact of
  this profile).
- **pending_review** (stored, never shared) for agent contributions: no approved policy yet;
  audience wider than the effective default; `require_review_all`; category `require_review`;
  `involves_others: true` (whatever the audience or policy; the item `message` says it awaits the
  owner's review because it involves other people); and always for `sensitive`.
- `involves_others: true` with no `audience` defaults the audience to `only_me` (tightest
  plausible), not the category default.
- The owner's own entries (browser, `via="ui"`) are explicitly accepted (`active`/`provisional`
  by the same evidence rule), except `sensitive`, which stays `pending_review`.
- **accepted**: status `provisional` when `evidence_type = inferred` or `confidence = low`
  (shared with a caveat while `share_provisional`), else `active`.
- `supersedes_id`: if the replacement is accepted, the old fact becomes `superseded` in the same
  transaction; if pending, the old fact stays live until the owner approves the replacement.

Contributors: the owner (UI, explicit acceptance) and the owner's own agents (MCP/API).
Third-party contributions are out of MVP scope and denied. `claim_events` record actor user and
delegation, policy version and `automatic`/`explicit` acceptance, never statement text.
Actions: `auto_accepted` (agent, automatic), `created` (owner entry: explicit; held: acceptance
null), `approved`, `rejected`, `audience_changed`, `superseded`, `removed`; `changes` holds only
`{"status": {old,new}}` / `{"audience": {old,new}}`.

## Python service API (for F6 and F7)

```python
# revel.services.profiles
async def ensure_profile(session: Session, *, user_id: UUID) -> UUID
    """Get or create the user's profile in the caller's transaction (calls F4's
    on_profile_created when it creates one). Caller commits."""

async def add_facts(session: Session, principal: Principal, facts: Sequence[FactInput]
                    ) -> AddFactsResult  # contributes to principal's own profile; commits

async def effective_max_audience(session: Session, profile_id: UUID, category: str) -> Audience
    """Widest audience the owner's CURRENT policy allows for the category without review.
    Raises UnknownCategory / ForbiddenCategory."""

def validate_category(category: str) -> CategorySpec  # pure; same errors
```

`FactInput`, `AddFactsResult`, `FactOutcome` are Pydantic models in `revel.services.profiles`.
Do not call `ensure_profile` inside F2's delegation transaction. Also exported (used by the
adapters): `get_policy`, `set_policy`, `get_my_profile`, `get_summary`, `set_fact_audience`,
`remove_fact`, `approve_fact`, `reject_fact`, `patch_fact`, `session_scope` (sessions from the
shared `get_sessionmaker`; `configure_sessionmaker` overrides it in tests). Errors:
`FactNotFound` (404), `NotAllowed` / `AuthorizationDenied` (403), `FactConflict` (409, carries
the fact), `PolicyVersionConflict` (409), `PolicyInvalid` / `InvalidRequest` (400),
`AuthorizationUnavailable` (503).

## Operations

| Operation | MCP tool (agent) | HTTP bearer (agent API) | HTTP browser (F8; writes same-origin) |
| --- | --- | --- | --- |
| Categories + current policy | `get_contribution_policy` | `GET /api/v1/profile/policy` | `GET /api/ui/profile/policy` |
| Approve/change policy | none | none | `PUT /api/ui/profile/policy` |
| Contribute facts | `add_facts` | `POST /api/v1/profile/facts` | `POST /api/ui/profile/facts` |
| Owner read (facts + summary) | `get_my_profile` | `GET /api/v1/profile` | `GET /api/ui/profile` |
| Contribution summary | in `get_my_profile` | `GET /api/v1/profile/summary?since=` | `GET /api/ui/profile/summary?since=` |
| Correct (new statement/details) | `add_facts` with `supersedes_id` | same | `PATCH /api/ui/profile/facts/{id}` |
| Change audience | `set_fact_audience` (narrow only) | `PATCH /api/v1/profile/facts/{id}/audience` (narrow only) | `PATCH /api/ui/profile/facts/{id}` |
| Remove (undo) | `remove_fact` | `DELETE /api/v1/profile/facts/{id}` | `DELETE /api/ui/profile/facts/{id}` |
| Approve/reject pending | none | none | `POST /api/ui/profile/facts/{id}/approve`, `/reject` |

Agent principals may only narrow an audience; widening is a UI action (explicit acceptance).
A UI correction creates the replacement (status `active`, evidence `explicit`, confidence
`high`, explicit acceptance) and supersedes the old fact atomically; only live facts can be
corrected (a pending fact is approved or rejected instead: 409). Approving makes a pending fact
`active` and supersedes a replacement's target in the same transaction (409 if the target is no
longer live, or if its audience is no longer allowed, e.g. Public after a withdrawal). Rejecting
sets status `removed` (event `rejected`); a rejected replacement leaves its target live.
Removal works on live and pending facts; removing a retired fact is 409. Reads never create a
profile (no profile yet: `profile_id: null`, empty facts, `policy: null`); `add_facts` and
policy writes create it.

### JSON shapes

`GET .../policy` →
```json
{"policy": null | {"version": 3, "approved_at": "...", "public_opt_in": false,
  "share_provisional": true, "require_review_all": false, "enrichment_enabled": true,
  "category_settings": {"size": {"audience": "only_me"}}},
 "provisional_caveat": "Provisional facts (inferred or low confidence) are shared with a caveat ...",
 "categories": [{"id": "interest", "label": "Interests and hobbies", "default_audience": "connections",
   "public_eligible": true, "effective_audience": "connections",
   "allowed_audiences": ["only_me", "trusted_circle", "connections"], "require_review": false,
   "details_schema": null}]}
```

`PUT /api/ui/profile/policy` body: `{"expected_version": 2 | null, "public_opt_in": ...,
"share_provisional": ..., "require_review_all": ..., "enrichment_enabled": ...,
"category_settings": {...}}` → the `GET .../policy` shape. 409 `{"detail": "policy_version_conflict", "policy": {...}, "categories": [...], "provisional_caveat": "..."}` (the current `GET .../policy` shape plus `detail`).

`POST .../facts` body `{"facts": [FactInput, ...]}` →
`{"results": [{"index": 0, "outcome": "accepted", "claim_id": "...", "status": "active",
"audience": "connections", "reason_code": null, "message": null}]}`.

`GET .../profile` →
```json
{"profile_id": "..." | null, "policy_version": 3 | null, "policy": null | {...policy...},
 "facts": [{"id": "...", "category": "owned_item", "statement": "...", "details": null,
   "audience": "connections", "status": "active", "provisional": false,
   "evidence_type": "explicit", "confidence": "high", "observed_at": "2026-10-20",
   "observed_precision": "day", "contributed_via": "mcp", "contributor": "Muse" | "you",
   "supersedes_id": null, "created_at": "...", "updated_at": "..."}],
 "summary": {...}}
```
Default `facts` = live and pending; `?status=` repeats to filter (any status). `contributor` is
`"you"` for the owner's own entries, else the agent's name (or `"Your assistant"`).

Summary: `{"since": "...", "counts": {"by_status": {...}, "by_audience": {...}},
"pending_review": 2, "recent": [{"claim_id", "action", "category", "audience", "acceptance",
"actor": "Muse" | "you", "occurred_at"}]}` (events carry no statement text). `since` defaults
to 7 days ago; `by_status` counts all facts, `by_audience` counts live facts; `recent` is the
latest 50 events (`audience` is the new audience of an audience change, else null).

`PATCH /api/ui/profile/facts/{id}` body: any of `audience`, `statement`, `details`,
`observed_at`/`observed_precision` → the updated/replacement fact. Statement/details/date
changes create a replacement; audience-only changes update in place. Fact-level 409 bodies are
`{"detail": "<reason>", "fact": {...current fact...}}` (reasons: `fact_not_editable`,
`fact_not_removable`, `fact_not_pending`, `fact_not_live`, `superseded_fact_not_live`,
`audience_not_allowed`). `PATCH /api/v1/profile/facts/{id}/audience` body `{"audience": ...}`.

### Errors

400 validation (`{"detail": [...]}` field errors); 401 not signed in / bad token; 403 not allowed
(e.g. an agent widening or approving, policy write by an agent, an OpenFGA contribute check that
fails, a cross-origin UI write); 404 fact not found **or not the
caller's** (never reveals existence); 409 policy version conflict or a fact that changed state;
503 authorization/identity service unavailable (generic text, no SDK detail).
MCP tools return the same outcomes as tool results or `ToolError` with generic text.

## Not in #9

Reads of other people's profiles (Sam reading Alex) need F6's handle resolution. Planned F5
follow-up: service `read_shared_profile(session, principal, *, profile_id)` over `visible_claims`
and MCP tool `get_profile(handle)` / `GET /api/v1/profiles/{handle}`, wired once F6 exposes
handle → profile resolution.
