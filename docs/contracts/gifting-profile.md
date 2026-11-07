# Gifting Profile: phase contract v1 (GP-01, RG-70)

Status: design contract for review. Nothing here is implemented. Base: `2ea61d4`.

This file is the implementation contract for GP-02 to GP-08. Product policy lives in Notion: the
[phase plan](https://app.notion.com/p/4112bade7f3181148150f30a460c3e08),
[Privacy](https://app.notion.com/p/40b2bade7f31811d8e55d8818e949b31) ("Gifting Profile narrative
access"), [Questions](https://app.notion.com/p/40a2bade7f3181bf9975f7fa9a1f597d) ("New typed
update") and [Onboarding](https://app.notion.com/p/40a2bade7f3181eab40de24dfbe88adb) ("Next
phase"). If this file and Notion conflict, Notion wins and the conflict goes to the lead.
Existing fact behavior stays in `docs/contracts/f5-profiles.md`.

Non-goals: no Revel-side LLM, worker, event table, grade table, new OpenFGA relation or model
change, new read tool, or change to `submit_question` profile context or answer evidence.

## 1. Sections

| `section` (stable id, canonical order) | `label` |
| --- | --- |
| `about` | About |
| `gifting_style` | Gifting style |
| `interests_and_taste` | Interests and taste |
| `gift_directions` | Gift directions |
| `gift_cautions` | Gift cautions |

Text: plain text, 1-1200 characters after trimming, no control characters except newline, passes
F5's `validate_statement` street-address check. No Markdown or HTML semantics; clients render it
as text. Every optional section may stay empty.

## 2. Vocabulary

| Term | Values | Meaning |
| --- | --- | --- |
| `audience` | `public`, `connections`, `trusted_circle`, `only_me` | Existing `Audience`. Each section has its own. Default for a new section: `connections` (Privacy) |
| `text_origin` | `owner_approved`, `evidence_derived` | `owner_approved`: the owner wrote the text or confirmed this exact text. Everything else is `evidence_derived`. Shown to every viewer of the section |
| `basis` | `stated`, `inferred` | `stated` needs either at least one citation with all of them explicit and non-provisional, or `owner_approved` text with no citations. Otherwise Revel stores `inferred`, whatever the caller sent. Default `inferred` |
| citation | live fact id | A reference to a claim of the same profile |
| head | highest `revision` of a section | Any state |
| published revision | highest `revision` with `state = published` | What readers may get. `body` null means cleared |
| pending revision | head row with `state = pending` | At most one per section, by construction |
| superseded pending | `state = pending` row that is not the head | Derived. Can never be approved |

## 3. Schema (GP-02, migration `0010_gifting_profile`)

The minimum the plan expected: one append-only revision table, one citation table, one column.

### `gifting_profile_revisions`

Append-only like `contribution_policies`: no `updated_at`, and no service updates or deletes a
row. Only `demo/reset.py` hard-deletes.

| Column | Type | Rule |
| --- | --- | --- |
| `id` | uuid PK | `gen_random_uuid()` |
| `profile_id` | uuid, FK `profiles` RESTRICT | |
| `section` | text | CHECK in `ProfileSection` |
| `revision` | int | `>= 1`; `UNIQUE (profile_id, section, revision)`; next = head + 1 |
| `action` | text | CHECK in `set, clear, set_audience, approve, reject, restore` |
| `state` | text | CHECK in `published, pending, rejected` |
| `body` | text null | length 1-1200; null exactly when `action IN (clear, reject)` |
| `audience` | text | CHECK in `Audience`. `clear` and `reject` copy the audience they replace, so a cleared section keeps its audience baseline |
| `text_origin` | text null | CHECK; null exactly when `body` is null |
| `basis` | text null | CHECK; null exactly when `body` is null |
| `source_revision` | int null | `approve`/`reject`: the pending revision. `restore`: the restored revision. `set_audience`: the revision whose text was copied. Else null |
| `author_user_id` | uuid, FK `users` RESTRICT | Always the owner |
| `author_delegation_id` | uuid null | Composite FK `(author_delegation_id, author_user_id)` to `agent_delegations (id, user_id)` RESTRICT, as on `claims` |
| `via` | text | CHECK in `ContributedVia` |
| `acceptance` | text null | CHECK in `AcceptanceMode`: `explicit` for owner-confirmed or browser rows, `automatic` for other agent rows, null while pending |
| `policy_version` | int | Composite FK to `contribution_policies (profile_id, version)` RESTRICT. Not null: no write precedes the first policy |
| `occurred_at` | timestamptz | `now()` |

Also `UNIQUE (profile_id, id)` as the composite target. `approve`, `restore` and `set_audience`
copy text and citations into a new row, so a published row is self-contained and approval leaves
the pending row untouched. Head and published lookups use the unique index
(`DISTINCT ON (section)` over at most five sections). No head table, because it could drift.

Why a new table: revisions need owner-only text, per-section numbering and audit that no existing
table can hold. `contribution_policies` is the precedent.

### `gifting_profile_citations`

Follows `answer_evidence`. Append-only. Why not a JSONB array: the composite FK proves a citation
belongs to this profile, and `claim_id` can be indexed.

| Column | Type | Rule |
| --- | --- | --- |
| `revision_id` | uuid | PK part; composite FK `(profile_id, revision_id)` to revisions RESTRICT |
| `claim_id` | uuid, indexed | PK part; composite FK `(profile_id, claim_id)` to `claims (profile_id, id)` RESTRICT |
| `profile_id` | uuid | Denormalized for both FKs |

Maximum 20 citations per revision.

### `profiles.narrative_reviewed_events`

`BIGINT NOT NULL DEFAULT 0`. The per-profile maintenance watermark, as a count (section 11).

### Enums

Add to `db/enums.py`: `ProfileSection`, `RevisionAction`, `RevisionState`, `TextOrigin`,
`NarrativeBasis`. `ClaimEventAction` and the OpenFGA model do not change.

## 4. Permissions

OpenFGA already holds what is needed. No relation or tuple changes.

| Operation | Check |
| --- | --- |
| Any section write | `authz.require_contribute` (owner or owner's agent). Writing another person's profile is impossible: no input names a profile |
| Owner read, `view_as`, revisions, pending, history | `view_only_me` tier |
| Shared read | `resolve_handle` then `visible_claims`, exactly as facts |
| Section filter | PostgreSQL, by `audience`, like claims |

### F4 additions (`auth/visibility.py`)

`ViewerTier.only_me` and the private tier helpers stay forbidden outside `visibility.py`.

```python
class OwnerGrant: ...  # frozen; proves view_only_me for one profile_id, private constructor
async def require_owner_view(principal, profile_id, *, fga=None) -> OwnerGrant
def audience_claims(session, grant, audience: Audience) -> VisibleClaims
    """Exactly what `audience` (not `only_me`) sees through `_visible_claims_for_tier`:
    Connections tier for `connections`, and so on. Sync, no OpenFGA call, so it is safe under
    the profile lock."""
def owner_claims_locked(session, grant) -> list[Claim]   # live and pending; sync
```

Readiness, `view_as`, reach and the write checks all use `audience_claims`. None may re-implement
the audience, provisional or opt-in rules.

## 5. Evidence rules

| Rule | Enforced |
| --- | --- |
| **Required citations.** `evidence_derived` text with audience other than `only_me` needs at least one citation | `set`, `restore`: `citations_required` |
| **Live.** Each citation names a live (`active`, `provisional`) fact of this profile. Missing, foreign, pending, superseded and removed are identical | Write and publication: `unknown_citation` |
| **Evidence ceiling.** A section is never more visible than its narrowest citation: every citation must be in `audience_claims(section audience)` (for `only_me`, liveness only). This applies to `owner_approved` text with citations too, and also rejects a provisional citation while `share_provisional` is off | Write and publication: `evidence_ceiling`. Read: section 6 |
| **Sensitive.** A section with audience other than `only_me` cites no `sensitive` fact | Write and publication: `sensitive_citation` |
| **Close match.** Shared text may not closely match a fact the audience cannot read (below) | Write and publication: `narrower_fact_match` |
| **Add first.** New durable knowledge goes in as a fact before it is cited | Guidance only |

**Close-match test (deterministic, in one helper shared with readiness).** `tokens(s)`: NFKC,
casefold, split on non-alphanumerics, drop tokens under 3 characters and a fixed stoplist of
function words and sentiment verbs (`likes`, `loves`, `prefers`, `hates`, `dislikes`, `enjoys`,
`avoids`, `wants`, `owns`, ...). Facts compared: live and pending facts not in
`audience_claims(section audience)`. For each fact, for its statement and each string value in
`details` independently: `T = tokens(value)`; a match is `T` non-empty and
`|T ∩ tokens(text)| / |T| >= 0.8`. It runs for every shared write, including the owner's own, and
never for `only_me` text or narrowing. The refusal names the fact id, never its text. This is a
safety net: it catches copying, not implication, so guidance and owner review remain necessary.

Citations are a disclosure ceiling and a withdrawal trigger. They do not prove the text.

## 6. Whole-section withdrawal

One predicate decides every read. It lives in `services/gifting_read.py` and takes no other input:

```python
def section_shown(published, citations: set[UUID], tier, visible_ids: set[UUID]) -> bool:
    return (
        published is not None and published.body is not None
        and Audience(published.audience).rank <= TIER_RANK[tier]
        and citations <= visible_ids
    )
```

- `visible_ids` come from `visible_claims` or `audience_claims` for that viewer, so a `public`
  tier viewer also needs the current `public_opt_in`.
- It uses only the latest published revision. There is **no fallback** to an older revision: a
  withdrawn section stays withheld until it is revised.
- The read loads revisions **first**, then claims, so a fact withdrawn during a read withholds.
- Nothing is cached or stored. Withdrawal needs no polling, signal, job or assistant response.
- `owner_approved` text with citations withdraws like any other. Text with no citations can only
  fall to the audience check.

| Trigger | Viewer effect on next read |
| --- | --- |
| Cited fact removed | Withheld from everyone but the owner |
| Cited fact superseded (a correction) | Same. The replacement is not auto-cited; the section needs revising |
| Cited fact narrowed, directly or by a category tightening | Withheld from viewers no longer able to read it; narrower tiers that can still read it keep the section |
| Cited fact is provisional and `share_provisional` goes off | Withheld from every non-owner |
| `public_opt_in` withdrawn | Public tier sees nothing (also section 8.5) |
| Connection revoked or tier lowered, viewer blocked | Falls to the new tier; tier `none` is the existing identical 404 |
| Viewer's assistant disconnected | Existing 401 / tool error |
| Section narrowed or cleared | Applies by audience, or nothing is published |
| Other unavailability to that viewer | `citations <= visible_ids` fails |

What follows the withdrawal, for that viewer:

| Output | Behavior |
| --- | --- |
| Text | Omitted |
| Evidence metadata | Omitted with the section. A shown section's evidence is always fully visible to that viewer, so no count or id can reveal a hidden citation |
| Readiness | Computed from the Connections projection, so a section not shown to Connections earns nothing and is not mentioned |
| Existence | Not revealed. A withheld, cleared, pending or never-written section is the same absence |

Owner reads remain complete: the owner sees the section with its `reach` and `withheld_citations`
(section 10.2). External responses carry no revision numbers, timestamps, authors, pending
markers or counts, so two profiles with identical shared content and different private facts,
conflicts or drafts produce equivalent responses, differing only in opaque ids.

## 7. Publishing states

| State | Who sees the text | Leaves by |
| --- | --- | --- |
| `pending` | Owner and owner's agents | `approve`, `reject`, or being superseded by a newer revision |
| `published` | Audience, through `section_shown` | A newer published revision or `clear` |
| `rejected` | History row, no text | Terminal |

The revision text of every state is owner-only and never logged.

Evaluate in order for an **agent** `set` or `restore`, after all checks in section 8 pass:

| # | Condition | Result | `pending_reason` |
| --- | --- | --- | --- |
| 1 | Proposed `text_origin` is `owner_approved` | pending | `owner_text` |
| 2 | Policy `require_review_all` | pending | `require_review_all` |
| 3 | Audience is `only_me` | published | |
| 4 | Profile has no published shared revision yet | pending | `first_publication` |
| 5 | Audience wider than baseline | pending | `wider_audience` |
| 6 | Otherwise | published, `automatic`; reports what changed | |

**Baseline**: the `audience` of the section's latest published row, including a `clear` row. If
the section has never been published, `connections`. Keeping the baseline across `clear` means
clear-then-set cannot widen silently.

**First publication** is profile-wide: it ends at the first `published` row with text and an
audience other than `only_me`. Each section in the first batch is confirmed separately.

Other cases:

| Case | Result |
| --- | --- |
| Agent `set_audience`, narrower | Published immediately, no confirmation |
| Agent `set_audience`, wider | `pending` with `wider_audience` (or `first_publication`), text and citations copied. No citation is required to hold it |
| `clear`, `reject` | Immediate |
| Browser owner (`via = ui`) | Writes publish at once with `owner_approved`, `explicit`, including first publication and widening: the owner is acting. All checks in section 8 still apply |
| Pending exists when another write lands | The write needs `expected_revision` = the pending head. It becomes the head, the old one is superseded, and the result reports `superseded_pending_revision` |

While a revision is pending, the published revision stays readable only while `section_shown`
passes.

## 8. Approval, publication checks, undo

### 8.1 Agent-first approval

Confirmation is a separate step on an existing revision. `set` has no `owner_confirmed`, so an
assistant cannot pre-confirm text the owner has not seen. This follows the fact-confirmation
pattern (`update_fact` preview, then confirmed retry) inside the one new tool:

1. The agent writes. The result returns `outcome: "pending"` and `confirmation`, holding the
   exact `text`, `audience`, `revision` and a `quote`.
2. The agent quotes exactly that to the owner and asks.
3. Only on a clear yes in this conversation, it sends `approve` with that `revision`, that
   `audience` and `owner_confirmed: true`.
4. `approve` without `owner_confirmed` writes nothing and refuses with
   `owner_confirmation_required`, listing the same data, so it also serves as a preview.

Assistants may read pending text with `get_profile` later and repeat steps 2-3.

### 8.2 Binding

A confirmation names `(section, revision, audience)`. Revisions are immutable, so this pins the
exact text and citations. `approve` succeeds only if `revision` is still the head **and** pending
**and** `audience` equals its stored audience. Otherwise nothing is written:

| Situation | Code |
| --- | --- |
| A newer revision arrived, or the pending was superseded, approved or rejected | `stale_approval` |
| `audience` differs | `audience_mismatch` |

Nothing carries forward: a confirmation of revision A cannot publish revision B. Approving
appends a `published`, `explicit`, `owner_approved` row with `source_revision` = the pending
revision, copying text, audience and citations.

### 8.3 Publication checks

Run under the profile lock at every publication, `approve` and `restore` included:

1. **Delegation.** The acting delegation is re-read and must be `active`.
2. **Policy.** A policy exists; `public` needs the current `public_opt_in`; `enrichment_enabled`
   per section 8.6.
3. **Sources.** Every section 5 rule on *current* facts. An approval fails with
   `source_unavailable` (or `evidence_ceiling`, `sensitive_citation`, `narrower_fact_match`) when
   a source moved since the revision was written. The revision stays pending and the owner read
   shows `blocked_citations`.

### 8.4 Locking

Facts and policy already hold the `profiles` row (`FOR SHARE`, policy `FOR UPDATE`). Section
writes take `FOR UPDATE`, so a publication and a fact change serialize:

`require_contribute` and `require_owner_view` (OpenFGA, no lock), then in one transaction: profile
`FOR UPDATE`, delegation, policy, heads, `audience_claims` and `owner_claims_locked`, inserts,
watermark, commit. If a withdrawal commits after the publication, the read predicate still
applies it. A unique-index violation maps to `stale_revision`.

### 8.5 Public

Needs both `public_opt_in` and a confirmation for that section. `public` without the opt-in is
refused (`public_not_opted_in`), never held. Withdrawing the opt-in (`set_policy`, in its
transaction) appends a `set_audience` narrowing to `connections` for each section published at
`public`, mirroring how F5 narrows facts. Opting in again therefore never revives a Public
section. A pending `public` revision is left in place and fails section 8.3 until the owner has
opted in again.

### 8.6 `enrichment_enabled = false`

| Agent action | Result |
| --- | --- |
| `set`, widening `set_audience`, `restore` | `enrichment_disabled` |
| `clear`, narrowing `set_audience`, `reject`, confirmed `approve`, no-change review | Allowed. These are owner controls, as `update_fact` stays available |

The browser owner is unaffected. No write of any kind precedes the first policy
(`policy_required`), browser included.

### 8.7 Disconnected assistants

A pending revision stays pending when its author disconnects. It never publishes by itself. It
publishes only through `approve` from a currently active delegation with `owner_confirmed`, or
the browser owner. Owner reads show `author_connected: false`. A revision already published stays
published under the normal read checks. A disconnected assistant cannot confirm: its token fails
at `principal_from_mcp_context` and at check 1.

### 8.8 Undo (`restore`)

`restore` proposes the text, citations and origin of an earlier revision as a new revision.

- Target: a `published` row of the same section with non-null `body`. Pending and rejected text
  cannot be restored.
- Audience: the **narrower** of the target's audience and the current baseline, so undo cannot
  restore old access. Wider access needs a later `set_audience` and confirmation.
- All of section 5 and 8.3 run on current facts. A target citing a withdrawn fact is refused with
  `source_unavailable`; the assistant must write a new revision.
- Then the section 7 table applies. An agent restoring `owner_approved` text gets a pending
  revision (row 1), because a confirmation never carries to a new revision.

## 9. `update_gifting_profile`

The only new MCP tool. Annotations: `readOnlyHint: false`, `destructiveHint: true` (`clear`),
`idempotentHint: false`, `openWorldHint: false`. Service: `gifting_writes.update_gifting_profile`.

```json
{
  "changes": [
    {"section": "gifting_style", "action": "set", "expected_revision": 3,
     "text": "Prefers useful things with a small story behind them.",
     "audience": "connections", "citations": ["<fact uuid>"],
     "text_origin": "evidence_derived", "basis": "stated"},
    {"section": "about", "action": "clear", "expected_revision": 4},
    {"section": "gift_cautions", "action": "set_audience", "expected_revision": 2,
     "audience": "trusted_circle"},
    {"section": "gift_directions", "action": "approve", "revision": 5,
     "audience": "connections", "owner_confirmed": true},
    {"section": "interests_and_taste", "action": "reject", "revision": 6},
    {"section": "about", "action": "restore", "expected_revision": 9, "revision": 2}
  ],
  "reviewed_through": 42
}
```

`changes`: 0-5 items, at most one per section (flat object, unknown keys refused, one shape over
MCP and HTTP). `reviewed_through`: optional integer from section 11. An empty `changes` needs
`reviewed_through`.

| `action` | Required | Optional | Refused |
| --- | --- | --- | --- |
| `set` | `section`, `expected_revision`, `text` | `audience` (default baseline), `citations`, `text_origin` (default `evidence_derived`), `basis` (default `inferred`) | `revision`, `owner_confirmed` |
| `clear` | `section`, `expected_revision` | | all others |
| `set_audience` | `section`, `expected_revision`, `audience` | | all others |
| `approve` | `section`, `revision`, `audience`, `owner_confirmed: true` (browser: flag not needed) | | all others |
| `reject` | `section`, `revision` | | all others |
| `restore` | `section`, `expected_revision`, `revision` | | all others |

- `expected_revision`: the section's head, `0` if it has none. Per section, so assistants editing
  different sections never conflict.
- A call is **atomic**: any failure writes nothing, including the watermark. All per-change errors
  are returned together.
- `set` with identical text, audience, citations, origin and basis writes nothing:
  `outcome: "unchanged"`. So does `set_audience` to the current audience.
- There is no grade, readiness or `owner_confirmed` input on `set`. An official tier cannot be
  submitted.

**No-change review:** `{"changes": [], "reviewed_through": 42}` records "reviewed, nothing to
change" for the whole profile. It writes no revision and resolves the signal for all the owner's
assistants. There is no acknowledgement tool.

Result (same over MCP and HTTP 200):

```json
{
  "results": [
    {"index": 0, "section": "gifting_style", "outcome": "published", "revision": 4,
     "published_revision": 4, "audience": "connections",
     "changed": ["text", "citations"], "superseded_pending_revision": null,
     "confirmation": null},
    {"index": 1, "section": "gift_directions", "outcome": "pending", "revision": 6,
     "published_revision": 2, "audience": "public", "changed": ["audience"],
     "superseded_pending_revision": null,
     "confirmation": {"section": "gift_directions", "revision": 6, "audience": "public",
       "text": "...", "pending_reason": "wider_audience",
       "quote": "Share Gift directions with Public, reading exactly: \"...\""}}
  ],
  "reviewed": true,
  "gifting_profile": {"...": "owner view, section 10.2"},
  "readiness": {"...": "section 12"},
  "maintenance": {"...": "section 11"}
}
```

`outcome`: `published`, `pending`, `cleared`, `rejected`, `unchanged`. `changed` subset: `text`,
`citations`, `audience`, `text_origin`, `basis`. `reviewed`: the watermark advanced.

Errors. HTTP body `{"detail": "<code>", "errors": [{"index", "section", "code", ...}]}`; the MCP
`ToolError` starts with the code and lists each item. Refusals name fact ids and revision numbers.

| HTTP | Code | When |
| --- | --- | --- |
| 422 | | Structural (FastAPI). MCP validation error |
| 400 | `invalid_request` | Per-action field rules, duplicate section, empty `changes` without `reviewed_through` |
| 400 | `invalid_text` | Length, control characters, street address |
| 400 | `citations_required`, `unknown_citation`, `evidence_ceiling`, `sensitive_citation`, `narrower_fact_match`, `public_not_opted_in`, `audience_mismatch` | Sections 5 and 8 |
| 400 | `invalid_reviewed_through` | Above the current event count |
| 401 | | Not signed in, bad token, disconnected |
| 403 | `not_allowed` | Fails `require_contribute`; delegation no longer active |
| 403 | `enrichment_disabled` | Section 8.6 |
| 403 | `owner_confirmation_required` | Body or text adds `confirmations: [{section, revision, audience, text, quote}]` |
| 409 | `stale_revision` | `expected_revision` is not the head; item carries `current_revision` |
| 409 | `stale_approval` | Section 8.2, for `approve` and `reject` |
| 409 | `nothing_published` | `clear`, `set_audience` with no published text; `restore` target invalid |
| 409 | `policy_required` | No approved policy yet (also no profile) |
| 409 | `source_unavailable` | Section 8.3 |
| 503 | | Authorization service unavailable |

Never logged: `text`, `quote`, any revision body. Adapters log exception type names only, as F5
does. `mcp/audit.py` logs method and target only and needs no change.

## 10. `get_profile`

### 10.1 Inputs and compatibility

```python
get_profile(handle: str | None = None,
            view_as: Literal["public", "connections", "trusted_circle"] | None = None,
            include_facts: bool = True)
```

| Field or behavior | Rule |
| --- | --- |
| `get_profile()` and `get_profile(handle)` with defaults | Every existing key and value is unchanged. New keys are additive |
| `include_facts` | Default `true`, unchanged until measurement justifies otherwise. `false` is the compact opt-in: `facts` becomes `null` and owner `history` becomes `[]`; nothing else changes. `null` is used, not `[]`, so a caller cannot read compact output as "no facts" |
| `view_as` | Owner preview, no handle. With `handle`: `invalid_request` |
| No narrative | Owner sees five empty sections; viewers see `sections: []`. Reads never create a profile |
| Non-MCP | `GET /api/v1/profile`, `/api/ui/profile`, `/api/v1/profiles/{handle}`, `/api/ui/profiles/{handle}` take `include_facts`; the two owner routes take `view_as` |
| Own handle | Existing shared shape at owner tier: no readiness, pending or history |
| `provisional_caveat` | Also set when a returned evidence item is provisional |
| `outputSchema` | Still both top-level shapes, no wrapper. `facts` becomes nullable; new keys are optional |

Added keys: owner `gifting_profile`, `readiness`, `maintenance`; shared `gifting_profile`,
`view_as`, `audience_reachable`; `AddFactsResult` adds `readiness`, `maintenance`. `update_fact`
results are unchanged.

### 10.2 Owner view (`get_profile()` adds)

Always five entries in canonical order. `pending`, `published` or both may be null.

```json
{
  "gifting_profile": {
    "first_publication_done": true,
    "sections": [
      {"section": "gifting_style", "label": "Gifting style", "revision": 6,
       "published": {
         "revision": 4, "text": "Prefers useful things with a small story behind them.",
         "audience": "connections", "text_origin": "evidence_derived", "basis": "stated",
         "evidence": [{"fact_id": "...", "category": "style", "evidence_type": "explicit",
           "observed_at": "2026-10-01", "observed_precision": "month", "provisional": false,
           "status": "active", "audience": "connections"}],
         "reach": ["connections", "trusted_circle"],
         "withheld_citations": [],
         "updated_at": "2026-11-01T10:00:00Z", "updated_by": "Muse"},
       "pending": {
         "revision": 6, "text": "...", "audience": "public",
         "pending_reason": "wider_audience", "author": "Muse", "author_connected": true,
         "blocked_citations": [], "created_at": "2026-11-02T10:00:00Z"},
       "history": [{"revision": 3, "action": "set", "text": "...", "audience": "connections",
         "actor": "you", "occurred_at": "2026-10-20T10:00:00Z"}]},
      {"section": "about", "label": "About", "revision": 0, "published": null,
       "pending": null, "history": []}
    ]
  }
}
```

- `revision`: the head, the value for `expected_revision`.
- `reach`: which of `public`, `connections`, `trusted_circle` currently pass `section_shown`,
  computed through `audience_claims`, never separately. Empty means shared with no one.
- `withheld_citations`: fact ids blocking reach at the section's own audience.
- `history`: at most the three latest restorable published revisions, with text. Omitted
  (`[]`) when `include_facts=false`. No separate history tool.
- `updated_by`, `author`: as `FactOut.contributor`: `"you"`, the agent's name or
  `"Your assistant"`.

### 10.3 Shared view (`get_profile(handle)` and `view_as` add)

```json
{
  "gifting_profile": {"sections": [
    {"section": "gifting_style", "label": "Gifting style",
     "text": "Prefers useful things with a small story behind them.",
     "audience": "connections", "text_origin": "evidence_derived", "basis": "stated",
     "evidence": [{"fact_id": "...", "category": "style", "evidence_type": "explicit",
       "observed_at": "2026-10-01", "observed_precision": "month", "provisional": false}]}
  ]},
  "view_as": null,
  "audience_reachable": null
}
```

Only sections passing `section_shown` appear, in canonical order. `evidence` is a subset of the
`SharedClaim` projection, with no statement, so compact output adds no fact text. No revision,
timestamps, author, status, pending, readiness, maintenance or next actions.

`view_as` returns exactly this shape, produced by the same code at the requested tier via
`audience_claims`, with `view_as` set. It never includes readiness. Because a real Public viewer
of a not-opted-in owner gets the 404, `view_as: "public"` then returns empty lists with
`audience_reachable: false` (`true` otherwise).

## 11. Derived maintenance signal

No stored event, job, expiry or dedupe. A watermark and a count.

```text
C(p) = count of claim_events for p WHERE action IN
       (auto_accepted, approved, audience_changed, superseded, removed)
       OR (action = created AND acceptance IS NOT NULL)
needed = narrative exists AND enrichment_enabled AND C(p) > profiles.narrative_reviewed_events
reviewed_through = C(p) as read
```

"Narrative exists" means the profile has at least one revision. Before that, readiness carries
the prompt instead.

**Why a count, not a timestamp.** `claim_events.occurred_at` is `now()`, the transaction start.
A fact whose transaction began before the read but committed after it would have an older
timestamp than a `reviewed_through` taken from a newer event, and would be lost. The count only
contains committed rows, so a late commit raises `C(p)` above any value already returned. Claim
events are append-only, and only demo reset deletes them.

Advance, only inside a successful `update_gifting_profile` that carried `reviewed_through = R`:
reject `R > C(p)` (`invalid_reviewed_through`), then
`SET narrative_reviewed_events = R WHERE narrative_reviewed_events < R`. It is monotonic, so an
older `R` is a harmless no-op. A write without `reviewed_through` never advances it.

| Property | Result |
| --- | --- |
| Duplicate polls | Same item, same id; nothing consumed |
| Missed poll | Derived again later; several changes make one signal |
| Consumed by | A profile update or no-change review only, never a poll. Resolves for all the owner's assistants |
| Concurrent fact | `C(p) > R` after advance, so the signal stays |
| Published sections | Stay readable while maintenance is pending, unless withdrawn |
| `gift_ready` | Ends readiness prompts, not this signal |
| `enrichment_enabled` off | Not needed, no item; the watermark holds until it is on again |
| Conflict and age triggers | Future scope |

Shape in owner `get_profile`, `add_facts` and `update_gifting_profile` results; `null` for no
profile, and `needed: false` with `prompt: null` when nothing is outstanding:

```json
{"maintenance": {"needed": true, "reviewed_through": 42,
  "prompt": "Facts changed since this profile was last reviewed. Update a section only if the evidence changed what it should say. Otherwise record no change with update_gifting_profile(changes=[], reviewed_through=42)."}}
```

`get_updates` item, added to `UpdateItem` as a discriminated union, returned only to the owner's
designated assistant (`inbox_check_setups.delegation_id` = the caller's delegation). With no
designated assistant there is no item, and the other surfaces still carry it:

```json
{"type": "profile_maintenance",
 "item_id": "profile_maintenance:<profile_id>:42",
 "reviewed_through": 42, "prompt": "..."}
```

It is appended after question items, does not count toward `MAX_UPDATES`, writes no event and is
not routed through `update_assignment`. It must also appear on the `get_updates` early return for
an empty inbox. `item_id` is stable until `C(p)` changes. It counts in `last_check_item_count`.

## 12. Readiness

Backend-owned, deterministic, non-numeric. A pure function, `services/readiness.py`:

```python
RULES_VERSION = "gp-readiness-1"
def assess(projection: ConnectionsProjection, *, policy_approved: bool, has_profile: bool,
           enrichment_enabled: bool, has_pending: bool, narrower_content: bool,
           today: date) -> Readiness
```

**Input:** only what a Connection can read: `audience_claims(connections)` and sections passing
`section_shown` at the Connections tier. `narrower_content`, `enrichment_enabled` and
`has_pending` never influence `state`; they affect only the owner note and next actions.

**Surface.** Owner reads and owner write results only. Absent from shared reads and `view_as`. No
persistence, percentage, weights, fact count or leaderboard. Labeled "Readiness for Connections".
An operational indicator: it is not a measure of accuracy or gift quality. The assistant owns
that judgment and may never set, override or restate the tier as its own.

### 12.1 Signals

| Term | Definition |
| --- | --- |
| Countable fact | In the projection, `provisional = false`, `evidence_type != inferred`, category not in `sensitive`, `size`, `location` |
| Fresh | `observed_at` null, or end of its period (`day`, `month`, `year`) within 730 days of `today` |
| Counted section | Shown at Connections, and either `owner_approved`, or has at least one countable citation |

| Signal | Holds when |
| --- | --- |
| `taste` | Countable `interest`, `style` or `favorite_brand`; or counted `interests_and_taste` or `gifting_style` |
| `direction` | Countable `wish`; or counted `gift_directions` |
| `caution` | Countable `dislike`, `owned_item` or `diet_exclusion`; or counted `gift_cautions`. A strength only. **Absence is neutral** |
| `narrative` | Any counted section |
| `conflict` | A projection fact in `interest`, `style`, `favorite_brand` or `wish` and one in `dislike` have `tokens` where one set contains the other and is non-empty; or likewise `wish` and `owned_item`. Provisional facts included: uncertainty applies to what is readable |
| `stale` | `taste` or `direction` holds but no fresh fact or counted section with a fresh citation (or `owner_approved`, no citations) supports it |

Facts about birthdays, anniversaries, life events and sentimental context neither help nor hurt.
There is no conflict state in claims, so `conflict` is a structural hint, labeled as "possible".

### 12.2 States

| State | Rule |
| --- | --- |
| `not_assessed` | No profile, no approved policy, or the assessment failed. A failure never fails the read: log the exception type and return this |
| `getting_started` | Neither `taste` nor `direction` |
| `useful` | `taste` or `direction`, and not `gift_ready` |
| `gift_ready` | `taste` and `direction` and `narrative`, and neither `conflict` nor `stale` |

`conflict` or `stale` caps a profile at `useful`. A concise evidence-backed profile can be
`gift_ready` with very few facts. Same inputs and same date give the same result on every client.

### 12.3 Output

```json
{"readiness": {
  "label": "Readiness for Connections",
  "state": "useful",
  "rules_version": "gp-readiness-1",
  "reason_code": "missing_direction",
  "reason": "Connections can read some gift signals, but not what this person wants.",
  "strengths": ["Interests or taste", "Things to avoid or already owned"],
  "narrower_content": false,
  "next_actions": [
    {"code": "add_direction",
     "gap": "Connections cannot see what they want.",
     "action": "If you already know from information you may use, add it with add_facts. Otherwise stop. Do not ask, guess or change who can see anything."}
  ]
}}
```

`reason_code` (fixed text, one per code): `no_profile`, `no_policy`, `assessment_unavailable`,
`no_shareable_signal`, `ready`, and for `useful` the first that applies of `possible_conflict`,
`stale_signals`, `missing_direction`, `missing_taste`, `no_narrative`. `strengths`: fixed labels of holding signals, never fact text, section
text or category names of hidden content. `narrower_content`: true when live facts or published
sections exist outside the projection. Its only effect is the fixed note "Some of this person's
profile is readable only by narrower audiences; that is their choice." It never produces an
action.

`next_actions`: at most two, first applicable in this order.

| Order | `code` | When |
| --- | --- | --- |
| 1 | `review_possible_conflict` | `conflict` |
| 2 | `refresh_old_facts` | `stale` |
| 3 | `add_taste` | No `taste` |
| 4 | `add_direction` | No `direction` |
| 5 | `write_narrative` | No `narrative`, `taste` or `direction` holds, no pending revision |

Every action names what to do from existing knowledge, then says to stop otherwise. Empty when
`gift_ready`, `not_assessed` or `enrichment_enabled` is false. Revel never generates a question
and never suggests widening an audience. Asking zero questions is valid: `getting_started` is an
acceptable place to stop. Never collect, infer, invent or publish merely to raise a state.

### 12.4 Example profiles (one parameterized test; `today = 2026-11-07`)

| # | Profile (what Connections can read unless noted) | Expected state | `reason_code` | `next_actions` |
| --- | --- | --- | --- | --- |
| 1 Sparse | One `interest`, inferred and provisional. Variant (generic filler): five sections citing only a `location` fact | `getting_started` | `no_shareable_signal` | `add_taste`, `add_direction` |
| 2 Concise but useful | Explicit `interest`, explicit `dislike`, no narrative | `useful` | `missing_direction` | `add_direction`, `write_narrative` |
| 3 Rich | Explicit `style`, explicit `wish`, one `gifting_style` section citing the `style` fact, no known cautions. Three items only | `gift_ready` | `ready` | none |
| 4 Conflicted or stale | As 3 plus `interest` "Loves lilies" and `dislike` "Dislikes lilies". Variant: as 3 with every fact observed 2023-06 | `useful` | `possible_conflict` / `stale_signals` | `review_possible_conflict` / `refresh_old_facts` |
| 5 Private only | Nothing. The owner has rich `only_me` and `trusted_circle` facts and a `trusted_circle` section. Variant (withdrawal): profile 3 after its cited `style` fact is narrowed to `trusted_circle` | `getting_started` / `useful` | `no_shareable_signal` / `missing_taste` | `add_taste`, `add_direction` / `add_taste`, `write_narrative`. Both with `narrower_content: true` |

The withdrawal variant shows readiness following a source. The section is withheld from
Connections and the `style` fact leaves the projection, so only `wish` remains: `direction` holds
and `taste` and `narrative` do not. Profile 3 also has no known cautions, which costs nothing.

The rubric and cutoffs (730 days, 0.8) are unvalidated. A change bumps `rules_version`. Validation
against gift outcomes is later research, not this phase.

## 13. Migration and rollback

`0010_gifting_profile`, hand-written, `down_revision = "0009_invitation_join"`, one head, owned by
GP-02.

| Direction | Steps |
| --- | --- |
| Upgrade | `SET LOCAL lock_timeout = '5s'`; create both tables; `ADD COLUMN` with a constant default (no table rewrite). **No backfill**: zero sections for every profile, no synthesized text |
| Downgrade | Refuse with an exception if either table has rows (precedent: `0009`); else drop the column and tables. Never silently drops narrative |
| Demo reset | Add `gifting_profile_citations`, `gifting_profile_revisions` before `claim_events` in `CLEAR_TABLES` and to the verification counts; reset `narrative_reviewed_events = 0` for demo profiles, because deleting `claim_events` lowers `C(p)` |
| Cloud Run rollback | Roll the image back; **no schema downgrade**. The old image names its columns, ignores the new tables and column, and reads no narrative. Facts are untouched. Forward fix is the default |
| Gate rollback | Setting `GIFTING_PROFILE_RELEASE=false` hides the narrative and write surface without losing data. Deployment changes follow the release gate |

Permissions, claims and existing data are not modified. `alembic check` stays clean.

## 14. MCP tool count

Exactly **19** registered at `2ea61d4`, **20** after release. Verified two ways:

1. `create_mcp_server(Settings())` was instantiated at `2ea61d4` and `list_tools()` returned 19:
   `add_facts`, `cancel_question`, `configure_inbox_checks`, `end_connection`, `get_profile`,
   `get_question`, `get_setup`, `get_updates`, `list_connections`, `manage_invitations`,
   `request_connection`, `respond_to_connection_request`, `set_contribution_policy`, `set_handle`,
   `submit_answer`, `submit_question`, `update_assignment`, `update_fact`, `whoami`.
2. The set matches `guidance/operations.py::IMPLEMENTED_MCP_TOOLS`, and
   `tests/guidance/test_content_lint.py::test_index_lists_every_registered_tool_and_bearer_route`
   asserts equality permanently. The release PR adds the one name there, which is the
   count check. That test passed at `2ea61d4`.

No `get_gifting_profile`, `acknowledge_profile_update`, `list_profile_revisions`, `get_guidance`
or render tool. The MCP App adds a resource and `_meta` on `get_profile`, not a tool.

## 15. HTTP and parity

| Operation | MCP | Bearer | Browser |
| --- | --- | --- | --- |
| Owner read | `get_profile()` | `GET /api/v1/profile` | `GET /api/ui/profile` |
| Audience preview | `get_profile(view_as=)` | `GET /api/v1/profile?view_as=` | `GET /api/ui/profile?view_as=` |
| Shared read | `get_profile(handle)` | `GET /api/v1/profiles/{handle}` | `GET /api/ui/profiles/{handle}` |
| All section writes and no-change review | `update_gifting_profile` | `POST /api/v1/profile/gifting-profile` | `POST /api/ui/profile/gifting-profile` (same-origin) |
| Maintenance inbox | `get_updates` | `GET /api/updates` | none |

- One service function per row. Adapters stay thin. `POST` bodies equal the tool's arguments and
  responses equal its result. Both are in `/openapi.json` from the release on.
- `view_as` on `/api/v1/profile` returns the shared shape. Declare `response_model` as the union
  of both.
- Browser writes authenticate as the owner: `owner_confirmed` is ignored there and `via = ui`.
  Bearer routes accept it like the tool.
- Status codes are the section 9 table; MCP returns the same code at the start of `ToolError`.

## 16. Security assumptions

| # | Assumption | Consequence |
| --- | --- | --- |
| 1 | `owner_confirmed` is asserted by the owner's assistant. Revel cannot prove a human agreed (same as facts) | Tool text forbids inferring consent. Binding limits the damage to one exact revision. MCP Apps get no confirm path |
| 2 | The owner's authorized assistants are trusted within the policy | They read all pending and history text. Other users never do |
| 3 | Citations do not prove the text | A truthful citation can sit under a sentence that says more. The close-match test catches copying only. Guidance and owner review remain necessary |
| 4 | `owner_approved` text with no citations has no withdrawal trigger | The owner accepted that text. It obeys audience only |
| 5 | Readers may have kept what they already saw | Withdrawal stops future reads and cannot recall anything (Privacy) |
| 6 | Section and fact text are untrusted data | Embedded instructions carry no authority. Tool descriptions and giver guidance say so; UI renders plain text; no field becomes a tool argument by itself. GP-08 tests this |
| 7 | OpenFGA and PostgreSQL state are current at read | OpenFGA failure fails closed (503), as for facts. Never an allow |
| 8 | Delegation status is authoritative | A disconnected token fails `principal_from_mcp_context` |
| 9 | `C(p)` relies on `claim_events` being append-only | Demo reset is the only deleter and resets the watermark |
| 10 | External equivalence relies on the fixed projection | New external fields must be reviewed against section 6 |
| 11 | Readiness is computed for the owner only | It is structural, so generic filler can satisfy it where the owner confirmed it. The label and reason say what it is |
| 12 | Text, quotes and evidence are never logged | Adapters log exception types. Reviewers check new log lines |

## 17. Owner decisions recorded

Each is an approved Notion record. This contract implements them and changes none.

| Decision | Source |
| --- | --- |
| Five sections; short, no diagnosis; assistant owns synthesis; no Revel LLM | Plan: Product direction, Core model |
| Citations unless owner wrote or confirmed; stated versus inferred; citations are a ceiling, not proof | Plan: Evidence |
| Only the owner's delegated agent edits; owner corrects, approves, rejects, undoes; third parties never | Plan: Editorial authority |
| Section audiences; narrow immediately; widen with confirmation; default Connections; Public needs opt-in plus section confirmation; `enrichment_enabled=false`; nothing before first policy | Plan: Privacy; Privacy page: Gifting Profile narrative access |
| Every-read check, whole-section withdrawal, evidence metadata and readiness follow, no fallback, omit silently, no sensitive citations, close-match rejection | Plan: Privacy; GP-01 sign-off blockers |
| Per-section revisions; first publication held; auto same-or-narrower; wider or `require_review_all` pending; confirmation bound to revision and audience; recheck at publication; disconnected stays pending; prior revision conditional; undo cannot restore access; text owner-only | Plan: Versioning; GP-05 page |
| 19 tools, target 20, only `update_gifting_profile`; `get_profile` extended; `include_facts` defaults true; `view_as`; compact opt-in | Plan: Scope guardrails, GP2 |
| One signal from `claim_events` newer than a watermark; no event table; `reviewed_through`; no acknowledgement tool; designated assistant; poll never consumes | Plan: GP4; Questions: New typed update; GP-06 page |
| Readiness states, Revel owns the grade, audience-aware, owner-only, Connections projection | Plan: Decision on official grade; Independent review additions |
| Guardrails: non-provisional only, sensitive/sizes/location excluded, cautions neutral, no generated questions or widening, `gift_ready` ends prompts, enrichment off removes actions, `rules_version` | Plan: Independent review additions |
| Read-only MCP App, no `owner_confirmed`; does not gate release | Plan: GP5; GP-07 page |
| New tool and guidance last; additive migrations; Cloud Run rollback | Plan: GP6; lead instruction, this task |
| This phase may change F1, F3, F4, F5, F7, F8, F9 contracts on `gp/phase-integration` | GP-01 Review evidence (owner, 2026-11-07) |

### Record interpretations (for reviewers to challenge)

These are readings of accepted records, not new policy. Each follows the literal text. A changed
reading is a small, local edit.

| # | Reading | Basis | Alternative |
| --- | --- | --- | --- |
| I1 | A later same-audience edit of a Public section auto-publishes. The "explicit confirmation" applies per section, not per edit | Versioning bullet has no Public exception; Privacy says "for that section" | Hold every Public edit: add a row before row 6 in section 7 |
| I2 | After first publication, a never-published section at Connections or narrower auto-publishes | "Narratives default to Connections"; first publication is "of a profile" | Hold each section's first publication: change row 4 |
| I3 | Only Me text is not a "publication": it publishes without confirmation and does not end first publication | Audiences: "not a social audience" | |
| I4 | An approved fact that is `inferred` still earns nothing | "inferred facts earn nothing" | Count it once owner-approved |
| I5 | With enrichment off, agents keep narrowing, clearing, rejecting and confirmed approval (section 8.6) | Agent-first owner controls; `update_fact` precedent | Refuse all agent writes |
| I6 | Without a designated assistant there is no `get_updates` item | "where one exists"; other surfaces carry the signal | Give it to all the owner's assistants |
| I7 | Heuristic `conflict` and date-based `stale` feed readiness, though conflict and age are out of scope as **maintenance triggers** | The rubric asks for contradictions and freshness; the exclusion is about triggers | Drop them from `gp-readiness-1` |

## 18. Unresolved product decisions

None at this revision. I1 and I2 are the two a reviewer is most likely to want the owner to
confirm: neither changes the schema or the tool shape.

## 19. Release ordering

The new tool and the guidance that references it release last, so a partial state never appears
to work.

### 19.1 The gate

One field: `Settings.gifting_profile_release: bool = False` (GP-02, env
`GIFTING_PROFILE_RELEASE`, set by no Terraform or deployment config). Tests enable it with
`Settings(gifting_profile_release=True)`. No second flag exists anywhere.

| Surface | Gate off (default) | Gate on |
| --- | --- | --- |
| `update_gifting_profile` | Not registered: `mcp/server.py` skips `gifting_profile_tools.register`. Count 19 | Registered. Count 20 |
| HTTP write routes | Router not included by `create_app`: 404, absent from `/openapi.json` | Included |
| Web editor | Hidden: the owner read has `gifting_profile: null`, the only signal the frontend uses | Shown |
| `gifting_profile`, `readiness`, `maintenance` in `get_profile` and `add_facts` | `null` | Populated |
| `get_updates` maintenance item | Never returned | Returned |
| `get_profile.view_as`, `include_facts` | Work for facts (no narrative exists to show) | Include narrative |

The service layer does not read the gate. Adapters and `create_app` do, using the `settings` they
already receive. Nothing else in the codebase may branch on it.

### 19.2 Order on `gp/phase-integration`

| Step | PR | Gate |
| --- | --- | --- |
| 1 | GP-02 schema, adds the gate field | off |
| 2 | GP-03 reads and readiness | off |
| 3 | GP-04 tool module, HTTP module, authoring tool description | off |
| 4 | GP-05 approval, review, undo | off |
| 5 | GP-06 first PR: `get_updates` item | off |
| 6 | GP-07 web card and editor checkpoint | off |
| 7 | **GP-06 last PR, `gp/rg-75-release-tool-guidance`** | **flips on** |
| 8 | GP-07 MCP App (any time after step 7; does not gate release or GP-08) | on |
| 9 | GP-08 verification, on a held revision | on |

The release PR merges last among 1-7 and is small. It changes only:

1. `settings.py`: default `True`.
2. `guidance/operations.py`: add `update_gifting_profile` to `IMPLEMENTED_MCP_TOOLS` and the bearer
   `POST` route to `IMPLEMENTED_HTTP`, making the lint test's set 20 names.
3. `get_profile` and `add_facts` descriptions: the short authoring and readiness rules.
4. The guidance: `revel-get-started`, `revel-manage-profile`, `revel-find-a-gift`,
   `revel-connect-and-read`, `index.md`, `server-instructions.txt` if needed, version bump and
   `CHANGELOG.md`.
5. The guidance tests, removing `UNRELEASED_FIELDS` (19.4).

Nothing merged before step 7 may mention `update_gifting_profile`, except the tool's own module
and tests. GP-06's Done depends on step 6 merging, which Notion's Depends on does not yet state:
the lead tracks this.

### 19.3 Acceptance split

Verified **before release**, in tests with the gate on (`gate_on` fixture), and default-off
behavior is asserted separately:

| Task | Items |
| --- | --- |
| GP-02 | All (gate-independent) |
| GP-03 | All, including the read matrix, withdrawal, legacy regression, external equivalence, readiness. Gate-off: legacy keys and values unchanged, new keys null |
| GP-04 | Section writes, non-owner refusal, stale revisions, ceiling, policy, holds, narrowing, readiness and maintenance in write results, API/OpenAPI/MCP parity on the gate-on app, 20 tools on a gate-on server, authoring text in the tool description |
| GP-05 | All, including stale-approval races and undo |
| GP-06 | Signal, watermark, no-change, duplicate and missed polls, concurrent fact |
| GP-07 | Web card, editor, readiness, withheld and empty states, Style Guide check, against fixtures then a gate-on backend |

Verified **only after release** (step 7 or later):

| Task | Items |
| --- | --- |
| Lint | Default `create_mcp_server(Settings())` registers 20; `IMPLEMENTED_MCP_TOOLS` equality; served guidance mentions the tool and passes lint; default `/openapi.json` includes the route |
| GP-06 | Guidance across tested MCP, website and API paths; readiness guidance never tells agents to collect, infer or widen; scheduled-run behavior; tool-only smoke flow |
| GP-07 | Tool text remains complete with final descriptions; MCP App host results |
| GP-08 | Two-user scenario on a held revision, client compatibility, tool-only onboarding, embedded-instruction test, release outcomes, rollback doc. All user-run Muse checks |

### 19.4 Guidance lint before release

`tests/guidance/test_skills_f6_f7.py` (`SharedProfileOut`) and `test_owner_controls_f5.py`
(`OwnerProfileView`) require every output model field to appear in served guidance. GP-03 and
GP-04 add fields before guidance may name them. Both loops skip one named constant,
`UNRELEASED_FIELDS = {"gifting_profile", "readiness", "maintenance", "view_as",
"audience_reachable"}`, defined in `tests/guidance/test_owner_controls_f5.py`. The release PR
deletes it, which forces the guidance to document them.

## 20. File ownership (at most two builders, disjoint files)

Paths under `backend/src/revel/` unless noted. Task order is the `gp/` branch order. Only the
listed task touches a path in its wave. "After GP-x" means serialized.

| Task | Owns |
| --- | --- |
| GP-02 | `backend/migrations/versions/0010_gifting_profile.py`; `db/enums.py`; `db/models/profile.py` (column); `db/models/gifting_profile.py` (new); `db/models/__init__.py`; `services/gifting_store.py` (new: head, published, pending, history, append; no claim reads); `demo/reset.py`; `settings.py` (gate); `backend/tests/db/test_db_profile_migrations.py`; `backend/tests/demo/test_demo_reset_db.py` |
| GP-03 | `auth/visibility.py`; `services/gifting_read.py` (new); `services/gifting_tokens.py` (new); `services/readiness.py` (new); `services/profiles.py` (read models, `get_my_profile`, `read_shared_profile`); `mcp/profile_tools.py`; `api/profiles.py`; `backend/tests/authz/test_visibility_v2_db.py`; `backend/tests/authz/test_claim_read_guard.py`; `backend/tests/profiles/test_shared_profiles_db.py`; `backend/tests/profiles/test_readiness_rules.py` (new); guidance tests for `UNRELEASED_FIELDS` |
| GP-04 | `services/gifting_writes.py` (new: `set`, `clear`, `set_audience`, decision table, checks); `services/gifting_maintenance.py` (new: `C(p)`, shape, advance); `mcp/gifting_profile_tools.py` (new); `api/gifting_profile.py` (new); `mcp/server.py`; `app.py`; `services/profiles.py` (`AddFactsResult`), `mcp/profile_tools.py` and `api/profiles.py` (`maintenance` wiring) after GP-03; `backend/tests/profiles/test_owner_controls_http_mcp_db.py` |
| GP-05 | `services/gifting_writes.py` (`approve`, `reject`, `restore`) after GP-04; `services/profiles.py` (`set_policy` hook, section 8.5); `backend/tests/profiles/test_owner_controls_http_mcp_db.py` after GP-04 |
| GP-06 first PR | `services/questions.py` (`UpdateItem`, `get_updates`); `mcp/question_tools.py`, `api/questions.py` if needed; `services/gifting_maintenance.py` after GP-04; `backend/tests/questions/test_questions_service_db.py` |
| GP-06 release PR | Section 19.2 list, including `backend/src/revel/guidance/**`, `settings.py` default, `mcp/profile_tools.py` descriptions, `backend/tests/guidance/**` |
| GP-07 | `frontend/src/GiftingProfileCard.tsx`, `.test.tsx` (new); `frontend/src/giftingProfileFixtures.ts` (new); `frontend/src/profileApi.ts`; `frontend/src/App.tsx`; `frontend/src/index.css`; `frontend/README.md`. MCP App, after the release PR: `backend/src/revel/mcp/app_resource.py` (new) plus `_meta` on `get_profile` |
| GP-08 | Disposable scripts, Notion evidence, `docs/deployment.md` rollback note. No permanent suite |

Parallel pairs:

| Wave | Pair | Disjoint because |
| --- | --- | --- |
| B | GP-02 + GP-07 card (fixtures) | Backend versus `frontend/` |
| C | GP-03 + GP-07 card | Same |
| D | GP-04 then GP-05, one at a time, GP-07 may continue | Shared `gifting_writes.py` |
| E | GP-06 first PR + GP-07 editor | `questions.py` versus `frontend/` |

**GP-07 before GP-03 merges** may build, against JSON copied from sections 10.2, 10.3 and 12.3
into `giftingProfileFixtures.ts`: the card, all states (owner, connection, withheld means simply
absent, empty, long text, pending, `gifting_profile: null` showing nothing), Readiness for
Connections, accessibility, mobile and desktop, and the frontend component test. It merges no live
integration until GP-03 is stable. The editor runs against fixtures until GP-05 merges, and is
shown only when `gifting_profile` is non-null. No backend file is touched. UI work reads the Brand
& UI Style Guide first.

## 21. Test inventory

Small and permanent, in the files the plan names. One-off client, rendering and quality checks
stay as task evidence; disposable scripts are deleted afterwards.

### Reuse

| Existing | Used for |
| --- | --- |
| `tests/authz/conftest.py`, `authz_fakes.py`, OpenFGA fixtures | Tiers and revocation |
| `tests/profiles/conftest.py`, `tests/db/conftest.py`, `run_alembic`, `migrated_engine` | Profiles, migrations |
| `test_claim_read_guard.py::guard_violations` | Static read guard |
| `test_content_lint.py` equality check | Tool count |
| `tests/demo/test_demo_reset_db.py`, `tests/questions/questions_support.py` | Reset, inbox |

### Add

| File | Cases |
| --- | --- |
| `tests/authz/test_visibility_v2_db.py` | Parameterized matrix: owner, Public, Connections, Trusted Circle, revoked, unauthorized × section audience, with a private-synthesis negative. Source-withdrawal matrix: removed, narrowed, superseded, provisional off, opt-in withdrawn, connection revoked, each asserting text, evidence and readiness. `view_as` equals a real viewer's response. External equivalence for two profiles with identical shared content and different private facts, conflicts and drafts |
| `tests/authz/test_claim_read_guard.py` | Only `services/gifting_read.py` and `gifting_writes.py` import `gifting_store`, so `revel/api` and `revel/mcp` cannot read revisions directly; no new claim-write module |
| `tests/profiles/test_owner_controls_http_mcp_db.py` | Owner-only edit of one section, non-owner refused, stale revision, ceiling, sensitive, close match, first-publication hold, widening hold, narrowing immediate, clear-then-set cannot widen, approve binds revision and audience, stale approval racing a second edit, racing a source withdrawal, disconnected author stays pending and needs current confirmation, reject, restore narrower and refused on withdrawn source, Public opt-in withdrawal, enrichment off, atomic batch, no-change review, concurrent fact not lost, API/MCP parity smoke |
| `tests/profiles/test_shared_profiles_db.py` | Captured legacy no-argument and handle responses, compact opt-in, absent-narrative fallback, gate-off null keys |
| `tests/db/test_db_profile_migrations.py` | Schema, constraints, zero sections, facts preserved, downgrade refused with rows and clean without, one head |
| `tests/profiles/test_readiness_rules.py` (new, one parameterized test) | Section 12.4 table and variants, determinism, no grade input, `not_assessed` on failure |
| `tests/questions/test_questions_service_db.py` | One item per window, duplicate and missed polls, other assistants' polls consume nothing, empty-inbox path, not designated gets none |
| `tests/demo/test_demo_reset_db.py` | New tables cleared, watermark reset |
| `tests/guidance/test_content_lint.py` | Release PR: curated guidance lint for tool, no-self-grading and stop rule text |
| `frontend/src/GiftingProfileCard.test.tsx` (new, one) | Plain-text rendering, owner versus connection, withheld, readiness label, no percentage |

### Remove

None at this revision. No existing test contradicts the contract: additive keys leave the
`outputSchema` two-shape assertion valid. Builders delete or consolidate a test only when an
interface change makes it obsolete, and say so in the PR. `UNRELEASED_FIELDS` is deleted by the
release PR.

## Changelog

- GP-01 (RG-70): initial contract at base `2ea61d4`.
