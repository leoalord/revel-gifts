# F5 profiles and contributions: operation contract v1 (#9, RG-18; shared reads #28, RG-56; agent-first owner controls #29)

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
even when review is otherwise off and even for the owner's own entry (approved in the UI, or by
the owner's agent with `owner_confirmed`, #29). A category id naming a
sensitive topic (e.g. `health`) is `unknown_category` with a hint to use `sensitive`.
Health-related diet exclusions stay in `diet_exclusion`, with no reason. Assistants save
sensitive topics only when the owner volunteers them (`sensitive`, Only Me, owner approval)
and never infer or ask for them; the MCP tool descriptions say so.

## Contribution policy

Append-only versions (current = highest `version`). Fields:

- `public_opt_in` (default false), `share_provisional` (default true),
  `require_review_all` (default false), `enrichment_enabled` (default true);
- `category_settings: {<category>: {"audience"?: Audience, "require_review"?: bool}}`. A category
  audience may be anything from `only_me` up to its default; `public` only for public-eligible
  categories while `public_opt_in` is true. A write whose resulting `public_opt_in` is false
  **normalizes** an explicit `public` audience of a public-eligible category: the audience key
  is dropped (the category falls back to its default; other keys such as `require_review` are
  kept) instead of a 400 (#28). `public` for a never-eligible category is still a 400.

**Effective default audience** for a category = its `category_settings` audience, else
`public` for public-eligible categories while opted in, else the category default.
**Maximum auto-accepted audience** = the effective default.

Policy writes come from the owner in the browser (`via="ui"`) or, since #29, from the owner's
own agent (MCP `set_contribution_policy`, bearer `PUT /api/v1/profile/policy`); an agent write
that widens access needs `owner_confirmed: true` (see "Owner confirmation" below). Writes take
`expected_version` (the version the caller read; `null` before the first); a mismatch returns
**409** with the current policy. The first
approval creates version 1. A later write whose effective content (all flags, the normalized
`category_settings` and F7's carried-forward fields) equals the current policy creates **no**
new version and returns the current policy unchanged (#28); the version check still applies. Omitted fields keep their current values (`category_settings`, when
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
`evidence_type`, is FastAPI's **422** / an MCP validation error for the whole request).

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
  For agents, a replacement without `audience` inherits the replaced fact's audience (capped at
  the category's effective default), and one wider than the replaced fact is held for review
  (see "Owner confirmation").

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
`FactNotFound` (404), `NotAllowed` / `AuthorizationDenied` (403),
`OwnerConfirmationRequired` (403, a `NotAllowed` subclass carrying `changes`; #29: `set_policy`,
`approve_fact`, `reject_fact` and `set_fact_audience` take keyword `owner_confirmed=False`), `FactConflict` (409, carries
the fact), `PolicyVersionConflict` (409), `PolicyInvalid` / `InvalidRequest` (400, field errors),
`AuthorizationUnavailable` (503).

## Operations

| Operation | MCP tool (agent) | HTTP bearer (agent API) | HTTP browser (F8; writes same-origin) |
| --- | --- | --- | --- |
| Categories + current policy | `get_contribution_policy` | `GET /api/v1/profile/policy` | `GET /api/ui/profile/policy` |
| Approve/change policy | `set_contribution_policy` (widening needs `owner_confirmed`) | `PUT /api/v1/profile/policy` (same) | `PUT /api/ui/profile/policy` |
| Contribute facts | `add_facts` | `POST /api/v1/profile/facts` | `POST /api/ui/profile/facts` |
| Owner read (facts + summary) | `get_my_profile` | `GET /api/v1/profile` | `GET /api/ui/profile` |
| Contribution summary | in `get_my_profile` | `GET /api/v1/profile/summary?since=` | `GET /api/ui/profile/summary?since=` |
| Correct (new statement/details) | `add_facts` with `supersedes_id` | same | `PATCH /api/ui/profile/facts/{id}` |
| Change audience | `set_fact_audience` (widening needs `owner_confirmed`) | `PATCH /api/v1/profile/facts/{id}/audience` (same) | `PATCH /api/ui/profile/facts/{id}` |
| Remove (undo) | `remove_fact` | `DELETE /api/v1/profile/facts/{id}` | `DELETE /api/ui/profile/facts/{id}` |
| Approve/reject pending | `review_pending_fact(fact_id, decision)` (approve needs `owner_confirmed`) | `POST /api/v1/profile/facts/{id}/approve` (needs `owner_confirmed`), `/reject` | `POST /api/ui/profile/facts/{id}/approve`, `/reject` |

### Owner confirmation (agent-first owner controls, #29)

Decision: Notion Privacy "Agent-first owner controls" (3 Nov 2026). The owner's OWN agent may
write the policy, approve/reject pending facts and change audiences; the browser UI is an
optional fallback and needs no flag (the browser user is the owner). Another user's agent can
never do any of this (404 for facts, 403 `not_allowed` from `require_contribute`), and every
write still runs after `authz.require_contribute`, with all other validation unchanged
(sensitive is always Only Me; Public needs eligibility and the opt-in).

For an agent principal, a change that **widens** access needs `owner_confirmed: true`:

- the FIRST policy approval (it turns on automatic acceptance within the policy), whatever its flags;
- `public_opt_in` false -> true;
- loosening any category's effective audience (compared with the current policy);
- `require_review_all` true -> false; removing a category's `require_review`;
- `enrichment_enabled` false -> true; `share_provisional` false -> true;
- approving a pending fact (including `sensitive` and `involves_others` facts);
- widening a fact's audience.

No flag is needed to **narrow**: tightening audiences, `share_provisional` off, requiring
review, withdrawing Public, `enrichment_enabled` off, rejecting a pending fact, and no-op
writes (no new version). A mixed policy write that contains any widening needs the flag; the
refusal lists only the widening parts. The check runs under the profile lock against the
current policy, after the version check (409 first) and validation (400 first) and before
any policy or claim row is written; a refusal writes no policy, claim or claim_event rows. (A
policy write ensures the profile first, in its own transaction, so a refused FIRST approval
may still leave the profile row and its owner tuple, as `add_facts` would.)

Corrections by an agent (`add_facts` with `supersedes_id`) never widen silently (#29 review
B1). The replacement is compared with the replaced fact's current audience (also across
categories): with no `audience`, it inherits the replaced fact's audience, capped at the
replacement category's effective default (`involves_others` still defaults to Only Me); an
explicit audience wider than the replaced fact's is `pending_review` (message says why; the old
fact stays live, and approving it needs `owner_confirmed`), even within the category default;
an equal or narrower one follows the usual rules (accepted and superseding atomically unless
another review rule applies). Owner corrections in the browser are unchanged.

Refusal (`reason_code` `owner_confirmation_required`):

- HTTP **403** `{"detail": "owner_confirmation_required", "changes": ["Opt in to Public: ...",
  "Widen the default audience of 'Sizes' from Only me to Trusted Circle"]}`;
- MCP `ToolError` whose text is `owner_confirmation_required: this widens access. Ask the owner,
  quoting exactly these changes, and call again with owner_confirmed=true only if they clearly
  agree:` followed by one `- <change>` line per change (same strings as `changes`).

Change strings name categories by label and audiences by label (never statement text); a
replacement's approval names the fact id it replaces. The tool descriptions tell assistants to
ask the owner, quote exactly what will change, and set `owner_confirmed` only after the owner
clearly agrees in this conversation; never infer consent.

Audit: fact approvals/rejections/audience changes by an agent record `actor_user_id` (owner) and
`actor_delegation_id` (the agent's delegation) in `claim_events`, with acceptance `explicit`
when `owner_confirmed` was given (always for approvals) and `automatic` for an unconfirmed
narrowing (rejection, narrowed audience, category tightening); the summary's `actor` shows the
agent's name. Automatic acceptance of contributions stays `auto_accepted`/`automatic`, so the
owner can tell confirmed agent actions apart. Policy versions record `approved_by_user_id` =
the owner (the table has no delegation column; an agent's policy write is visible through the
`audience_changed` events it causes, not on the version row).

The browser owner may widen an audience in place (explicit acceptance).
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
 "defaults": null | {"public_opt_in": false, "share_provisional": true,
   "require_review_all": false, "enrichment_enabled": true},
 "audiences": [{"id": "only_me", "label": "Only me"}, {"id": "trusted_circle", "label": "Trusted Circle"},
   {"id": "connections", "label": "Connections"}, {"id": "public", "label": "Public"}],
 "provisional_caveat": "Provisional facts (inferred or low confidence) are shared with a caveat ...",
 "categories": [{"id": "interest", "label": "Interests and hobbies", "default_audience": "connections",
   "public_eligible": true, "effective_audience": "connections",
   "allowed_audiences": ["only_me", "trusted_circle", "connections"], "require_review": false,
   "details_schema": null}]}
```
`defaults` is set only while `policy` is null (the flags that apply until the first approval),
else null. `audiences` lists every audience narrowest first with its display label.
`details_schema` is `null` (no details) or a JSON Schema subset: `{"type": "object",
"properties": {<name>: {"type": "integer", "minimum", "maximum"} | {"type": "string",
"maxLength": 100}}, "required": [...], "additionalProperties": false, "minProperties"?: 1}`
(only these keywords occur). The server also checks rules the subset cannot express (a valid
day of the month, no street address or year in text).

`allowed_audiences` bounds what a POLICY may set and what assistants get without review. A
fact itself may sit outside its category's `allowed_audiences` when the owner approved a wider
proposal or widened it in the UI (e.g. a `size` fact at `connections`); Public always needs
eligibility and the opt-in, and `sensitive` facts are always Only Me.

`PUT /api/ui/profile/policy` body: `{"expected_version": 2 | null, "public_opt_in": ...,
"share_provisional": ..., "require_review_all": ..., "enrichment_enabled": ...,
"category_settings": {...}}` → the `GET .../policy` shape. `PUT /api/v1/profile/policy` takes
the same body plus `"owner_confirmed": false` (default); the MCP `set_contribution_policy`
takes the same fields as arguments (`expected_version` required, nullable) and returns the
same shape. `POST /api/v1/profile/facts/{id}/approve` and `/reject` take an optional body
`{"owner_confirmed": false}` → the fact; MCP `review_pending_fact(fact_id, decision:
"approve"|"reject", owner_confirmed=false)` → the fact. 409 `{"detail": "policy_version_conflict", "policy": {...}, "categories": [...], "provisional_caveat": "..."}` (the current `GET .../policy` shape plus `detail`).

`POST .../facts` body `{"facts": [FactInput, ...]}` →
`{"results": [{"index": 0, "outcome": "accepted", "claim_id": "...", "status": "active",
"audience": "connections", "reason_code": null, "message": null}]}`.

`GET .../profile` →
```json
{"profile_id": "..." | null, "policy_version": 3 | null, "policy": null | {...policy...},
 "defaults": null | {...same as GET .../policy, set only while policy is null...},
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
`audience_not_allowed`). `PATCH /api/v1/profile/facts/{id}/audience` body `{"audience": ..., "owner_confirmed"?: false}`
(MCP `set_fact_audience(fact_id, audience, owner_confirmed=false)`).
PATCH field errors are 400 with field-level locations, e.g.
`{"detail": [{"loc": ["body", "statement"], "msg": "..."}]}` (also `["body", "details"]`,
`["body", "observed_at"]`, `["body", "audience"]`); the audience message names the actual rule
("Sensitive facts are always Only Me", "This category can never be Public", "Public needs the
owner's Public opt-in").

`DELETE .../facts/{id}` → **200** with the removed fact (`"status": "removed"`). Removing it
again (or a superseded fact) is **409** `{"detail": "fact_not_removable", "fact": {...}}`. The MCP
`remove_fact` tool is therefore annotated `idempotentHint: false` (a repeat is a tool error).

Locking: `add_facts` locks every `supersedes_id` target up front in id order (after the
profile's `FOR SHARE`), so batches naming the same facts in opposite orders queue instead of
deadlocking. Approving a replacement locks the replacement and its target together in the same
id order before touching either (#28), so it cannot deadlock with a batch that names both.

### Errors

422 structural validation (FastAPI: malformed JSON, wrong types or enum values, missing
required fields, unknown fields); 400 semantic validation (`{"detail": [{"loc", "msg"}]}`
field errors, e.g. a policy audience not allowed or an invalid correction); 401 not signed in /
bad token; 403 not allowed (`{"detail": "not_allowed"}`: an OpenFGA contribute check that
fails, e.g. another user's agent, a cross-origin UI write, an agent correction via `PATCH`) or
`{"detail": "owner_confirmation_required", "changes": [...]}` (an agent widening without
`owner_confirmed`, #29); 404 fact not found **or not the
caller's** (never reveals existence); 409 policy version conflict or a fact that changed state;
503 authorization/identity service unavailable (generic text, no SDK detail).
MCP tools return the same outcomes as tool results or `ToolError` with generic text, except
`owner_confirmation_required` (lists the changes) and policy field errors from
`set_contribution_policy` (`Invalid request: <field path>: <message>; ...`).

## Shared profile reads (#28, RG-56)

Reading another person's profile (Sam reading Alex). Every adapter: principal first, then F6's
`resolve_handle(session, principal, handle=...)`, then
`read_shared_profile(session, principal, *, profile_id=<resolved profile_id>)`. **No adapter
accepts a profile id**; `read_shared_profile` is internal and is only given the id that
`resolve_handle` returned for the same principal, so undiscoverable and blocking targets stay
hidden at the database layer. Facts come only from F4's `visible_claims` (tier from OpenFGA,
live statuses, audience <= tier, current `share_provisional` and `public_opt_in`); F5 filters
nothing itself. Nothing is written (F6's lookup quota aside); no F5 lock is held.

| Operation | MCP tool (agent) | HTTP bearer (agent API) | HTTP browser (F8, F6 #18) |
| --- | --- | --- | --- |
| Someone's shared profile | `get_profile(handle)` (readOnlyHint) | `GET /api/v1/profiles/{handle}` | `GET /api/ui/profiles/{handle}` |

The bearer route authenticates with the bearer token only (F2: cookies never authenticate
bearer routes); the browser route uses the session cookie (`CurrentUIPrincipal`; a GET, so no
same-origin check). Both return the same body and errors. Handles are exact (trimmed,
lowercased, one leading `@` dropped).

```json
{"handle": "alex_demo", "display_name": "Alex", "avatar_url": "https://..." | null,
 "facts": [{"id": "...", "category": "interest", "statement": "Collects stamps", "details": null,
   "audience": "public", "evidence_type": "explicit", "confidence": "unknown",
   "observed_at": null, "observed_precision": "unknown", "provisional": false}],
 "provisional_caveat": "Facts marked provisional were inferred ..." | null}
```

`facts` items are exactly F4's `SharedClaim` projection: never contributor, `contributed_via`,
status, policy version, `supersedes_id` or timestamps. `provisional_caveat` is set only when a
returned fact is provisional (shown only while the owner's `share_provisional` is on).
Connections viewers see Connections and Public facts, Trusted Circle also Trusted Circle facts,
an unconnected viewer Public facts only while the owner is opted in. Only Me, pending,
superseded and removed facts are never returned to anyone else. A user's agents see what the
user sees. The owner (or the owner's agent) reading their own handle gets the same shape with
all of their own live facts (including Only Me); `get_my_profile` remains the management read.

Errors (fixed text; never service or SDK detail):

- **404** `{"detail": "target_not_found"}` / MCP `ToolError("No Revel profile is available to
  you under that handle")` for every hidden case, identically: nonexistent or malformed handle,
  undiscoverable target without an approved connection, a target who blocked the caller, a
  target with no profile yet, and a resolvable target where the viewer's tier is `none`
  (unconnected without the Public opt-in, revoked, blocked, an agent whose tuples are gone).
- **429** `{"detail": "rate_limited"}` / `ToolError` (F6's lookup limit; misses cost, hits are
  free, checked before resolution).
- **403** `{"detail": "not_allowed"}` / `ToolError("Not allowed")` when F6 finds the acting
  delegation is no longer active (F2 normally rejects a disconnected agent before this: 401 /
  its own tool error). Independent of the handle.
- **503** `{"detail": "Authorization service unavailable"}` / `ToolError` for OpenFGA or any
  other failure.

Python: `read_shared_profile(session, principal, *, profile_id: UUID | None) ->
SharedProfileOut` (raises `SharedProfileNotFound` for `None` or tier `none`,
`AuthorizationUnavailable` on OpenFGA failure); `SharedProfileOut`, `SharedFactOut`.
