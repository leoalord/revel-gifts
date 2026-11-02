# F5 profiles and contributions: operation contract v1 (#9, RG-18)

Owner: revel-f5-profiles. Status: **provisional** until #9's reviewed merge. Published before
implementation so F6, F7, F8 (#10) and F9 (#11) can draft against it; changes are announced.
Product policy lives in Notion (main page "Profile fact data model", Privacy page "Contribution
policy"); this file is the implementation contract only.

Code layout: shared behavior in `backend/src/revel/services/categories.py` (category list) and
`backend/src/revel/services/profiles.py` (policy, contributions, owner reads, corrections);
thin adapters in `revel/api/profiles.py` and `revel/mcp/profile_tools.py`. Every claim read goes
through F4's `visible_claims` / `owner_claims`; nothing filters claims itself.

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

Always rejected: unknown categories, home/street address, birth year, and sensitive categories
(health, religion, politics, orientation, finances). `details` rejects unknown keys.

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
read); a mismatch returns **409** with the current policy. The first approval creates version 1.

Opt-in changes call F4's `on_public_opt_in_changed`: withdrawal after the policy INSERT and
before commit (raises → rollback); enabling after commit. Serialized per profile.

## add_facts

Input per fact: `category`, `statement` (1-500 chars), `details?`, `audience?` (default =
effective default), `evidence_type` (`explicit|observed|inferred`), `confidence?`
(`low|medium|high|unknown`, default `unknown`), `observed_at?` (ISO date) with
`observed_precision` (`day|month|year|unknown`; `unknown` iff no date), `supersedes_id?`.
Batch of 1-20; each item gets its own outcome, one bad item does not fail the batch.

Outcome per item (`accepted | pending_review | rejected`):

- **rejected** with `reason_code`: `unknown_category`, `forbidden_category`, `invalid_details`,
  `invalid_field`, `enrichment_disabled`, `supersedes_not_found` (not a live fact of this profile).
- **pending_review** (stored, never shared): no approved policy yet; audience wider than the
  effective default; `require_review_all`; category `require_review`.
- **accepted**: status `provisional` when `evidence_type = inferred` or `confidence = low`
  (shared with a caveat while `share_provisional`), else `active`.
- `supersedes_id`: if the replacement is accepted, the old fact becomes `superseded` in the same
  transaction; if pending, the old fact stays live until the owner approves the replacement.

Contributors: the owner (UI, explicit acceptance) and the owner's own agents (MCP/API).
Third-party contributions are out of MVP scope and denied. `claim_events` record actor user and
delegation, policy version and `automatic`/`explicit` acceptance, never statement text.

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
A UI correction creates the replacement and supersedes the old fact atomically (explicit).
Approving a pending replacement supersedes its target in the same transaction.

### JSON shapes

`GET .../policy` →
```json
{"policy": null | {"version": 3, "approved_at": "...", "public_opt_in": false,
  "share_provisional": true, "require_review_all": false, "enrichment_enabled": true,
  "category_settings": {"size": {"audience": "only_me"}}},
 "categories": [{"id": "interest", "label": "Interests and hobbies", "default_audience": "connections",
   "public_eligible": true, "effective_audience": "connections",
   "allowed_audiences": ["only_me", "trusted_circle", "connections"], "require_review": false,
   "details_schema": null}]}
```

`PUT /api/ui/profile/policy` body: `{"expected_version": 2 | null, "public_opt_in": ...,
"share_provisional": ..., "require_review_all": ..., "enrichment_enabled": ...,
"category_settings": {...}}` → the `GET .../policy` shape. 409 `{"detail": "policy_version_conflict", "policy": {...}}`.

`POST .../facts` body `{"facts": [FactInput, ...]}` →
`{"results": [{"index": 0, "outcome": "accepted", "claim_id": "...", "status": "active",
"audience": "connections", "reason_code": null, "message": null}]}`.

`GET .../profile` →
```json
{"profile_id": "...", "policy_version": 3,
 "facts": [{"id": "...", "category": "owned_item", "statement": "...", "details": null,
   "audience": "connections", "status": "active", "provisional": false,
   "evidence_type": "explicit", "confidence": "high", "observed_at": "2026-10-20",
   "observed_precision": "day", "contributed_via": "mcp", "contributor": "Muse" | "you",
   "supersedes_id": null, "created_at": "...", "updated_at": "..."}],
 "summary": {...}}
```
Default `facts` = live and pending; `?status=` repeats to filter (any status).

Summary: `{"since": "...", "counts": {"by_status": {...}, "by_audience": {...}},
"pending_review": 2, "recent": [{"claim_id", "action", "category", "audience", "acceptance",
"actor": "Muse" | "you", "occurred_at"}]}` (events carry no statement text).

`PATCH /api/ui/profile/facts/{id}` body: any of `audience`, `statement`, `details`,
`observed_at`/`observed_precision` → the updated/replacement fact. Statement/details/date
changes create a replacement; audience-only changes update in place.

### Errors

400 validation (`{"detail": [...]}` field errors); 401 not signed in / bad token; 403 not allowed
(e.g. an agent widening or approving, policy write by an agent); 404 fact not found **or not the
caller's** (never reveals existence); 409 policy version conflict or a fact that changed state;
503 authorization/identity service unavailable (generic text, no SDK detail).
MCP tools return the same outcomes as tool results or `ToolError` with generic text.

## Not in #9

Reads of other people's profiles (Sam reading Alex) need F6's handle resolution. Planned F5
follow-up: service `read_shared_profile(session, principal, *, profile_id)` over `visible_claims`
and MCP tool `get_profile(handle)` / `GET /api/v1/profiles/{handle}`, wired once F6 exposes
handle → profile resolution.
