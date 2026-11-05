---
name: CHANGELOG
description: Version history of the Revel agent guidance.
version: 0.8.2
status: active
---

# Revel agent guidance changelog

## 0.8.2 (2026-11-05)

- Invitations support a single signup path, nonexpiring links and idempotent verified joined
  attribution without a connection request or profile access.

## 0.8.1 (2026-11-04)

- `revel-manage-profile` section 3 centres facts on gift choice: record the lasting trait at
  the detail a giver can use, and only for interests a giver could act on. Routines, plans
  and task details (meal plans, workout logs, league settings) are not facts, and diets or
  fitness goals are never recorded as interests. Adds before-and-after examples. The `add_facts` tool and `statement` field descriptions say
  the same. No tool or API change.

## 0.8.0 (2026-11-04)

- Add canonical `revel-get-started` onboarding, owner-approved polling preferences and
  host task reports, with server-observed checks kept separate from background verification.
- Keep actual recurring task creation, updates and disablement in the host; no email fallback
  or native MCP Events support is claimed by polling setup.

## 0.7.3 (2026-11-04)

- Document email-bound invitations, exact destination consent, browser-only redemption,
  separate sharing approval and truthful asynchronous delivery status.

## 0.7.2 (2026-11-03)

- Confirm fact approvals and audience widening with the fact timestamp returned by the
  confirmation preview. Give the explicit `remove` or `reject` action when undoing a fact.

## 0.7.1 (2026-11-03)

- Profile guidance distinguishes drafts, pending facts and current provisional sharing,
  preserves evidence date precision, and asks for clarification only when a gap matters.
  The first-policy confirmation explains the proposed settings.
- Answer guidance keeps pending and restricted facts out of inline evidence.
- Gift guidance explains when a directly requested item may appear in the routed question.

## 0.7.0 (2026-11-03)

Two skills are renamed (RG-23, RG-43). MCP tool names are unchanged.

- `revel-manage-profile` (was `revel-profile-discovery`): it covers building, maintaining and
  editing your user's own profile, and its policy, approvals and audiences.
- `revel-find-a-gift` (was `revel-giver-workflow`).
- The MCP prompts and `revel://guidance/<name>` resources use the new names only; the old names
  are gone, with no aliases. The old website pages `/guidance/revel-profile-discovery.md` and
  `/guidance/revel-giver-workflow.md` permanently redirect (`308`) to the new pages.

Wording:

- HTTP API: it is for assistants without MCP access. Access for them is not available yet
  (issuing them tokens is deferred), so use MCP, the supported path today. People never use the
  HTTP API themselves; they use their assistant or the Revel website. The index has an "HTTP API"
  section, and the route catalog is labelled as the reference for assistants without MCP
  access. `/openapi.json` now declares the bearer scheme (`bearerAuth`) on those routes; the
  token is the access token an assistant gets by connecting over MCP.
- Owner confirmation (`revel-manage-profile`, `revel-connect-and-read`): include each returned
  `- <change>` line in the message to the owner, word for word, never a mention of the list
  without its lines. To approve a pending fact, also show the owner the fact's `statement`.
- Corrections (`revel-manage-profile`): a correction may keep the old fact's audience or set a
  narrower one; only a wider one is held for review.
- `revel-find-a-gift`: by default `private_purpose` carries the occasion, date, budget and the
  candidate ideas being weighed, because Revel checks the routed question against them (and
  `notes`) to keep it surprise-safe. If your client refuses to send them, send the occasion
  alone and keep the question itself neutral (no idea, brand or product names).
- `revel-answer-question`: lists the `invalid_evidence` reason "observed_month is in the
  future".
- `revel-connect-and-read`: a retried `end_connection` that is refused means the connection or
  request is already ended or cannot be ended that way; check `list_connections` rather than
  retrying. The examples now follow Sam connecting to Alex (handle "alex_demo"), as in the other
  skills.

## 0.6.0 (2026-11-03)

Fewer MCP tools (RG-59): related operations are now one tool with an `action` (or `include`),
and each action takes only its own parameters; anything else is refused with
`invalid_request`, whose exact messages the skills quote. Every example uses the new call
shape. HTTP routes are unchanged; the index route catalog maps each one to its tool and action.
Policy is unchanged.

- Questions (F7): `update_assignment` replaces the five separate lease tools. Pass
  `assignment_id` and an `action` (`claim`, `renew`, `release`, `decline` or
  `report_user_contact`). `revel-answer-question` lists each action's required and optional
  parameters. The lease and human-contact rules are unchanged: claim with `human_contact` true
  before contacting your user, quote the latest `lease_version`, respect the deadline, cooldown
  and `Retry-After`.
- Questions (F7): `get_question` replaces the separate status and answer tools: one call returns
  the status fields plus `result` (`answer`, `no_shareable_answer` or `not_answered`) and the
  `answer`. It records answer retrieval, so it is not read-only; calling it again is safe.
- Profiles (F5): `get_profile` without a `handle` replaces the separate own-profile and policy
  reads. It returns `facts`, `summary` and `policy`, the full policy response: read categories
  from `policy.categories` and pass `policy.policy.version` as `expected_version`. With a
  `handle` it is the shared read, unchanged.
- Profiles (F5): `update_fact` with `action` `set_audience` (requires `audience`) or `approve`
  (no `audience`) replaces the audience and review tools. `remove_fact` rejects a fact pending
  review (never needs confirmation; pass `owner_confirmed` true only when the owner explicitly
  asked to reject it) and removes a live one; a rejected correction leaves the fact it would
  have replaced live. Repeating a removal is a conflict: re-read with `get_profile`.
- Connections (F6): `list_connections` takes `include` (`connections`, `requests` or `all`, the
  default) and returns `connections`, `incoming` and `outgoing`. `respond_to_connection_request`
  gains `action` `change_tier`; `end_connection` with `action` `revoke` or `cancel` ends a
  connection or withdraws your own request. Do not pass `tier` or `owner_confirmed` with
  `decline` or `block`, and use `approve`, not `change_tier`, on a pending request. The
  owner-confirmation two-step flow is unchanged; the step about a changed change list is dropped
  from `revel-connect-and-read` because it cannot happen for connections.

## 0.5.4 (2026-11-03)

- Connections owner confirmation (F6, RG-27 follow-up): `revel-connect-and-read` gains a
  "Getting the owner's yes" section. Approving a request and upgrading a connection to
  `trusted_circle` need `owner_confirmed` true, using the same two-step flow as owner controls:
  call without the flag, quote Revel's `owner_confirmation_required` change list word for word,
  and repeat the identical call with the flag only after the owner's clear yes. Declining,
  blocking, downgrading, revoking and cancelling need no flag. Examples show both steps.
- Index: the widening sentence, the connections error note and the route catalog rows for
  `respond_to_connection_request` and `change_connection_tier` mention the confirmation.

## 0.5.3 (2026-11-03)

- Index: the own-profile tools in "What works today" now include `set_contribution_policy`
  and `review_pending_fact`, matching the route catalog (F5 review O2).

## 0.5.2 (2026-11-03)

- Discovery corrections: a correction with `supersedes_id` and no `audience` keeps the old
  fact's audience; an explicitly wider one is held for review (Revel's message is quoted) and
  approving it uses the two-step owner confirmation.
- Never remove a fact the owner narrowed or removed and re-add the same content as a new fact:
  it would land at the category default and undo the owner's choice.

## 0.5.1 (2026-11-03)

- Owner controls use one two-step flow for every widening change: call without
  `owner_confirmed`, quote Revel's `owner_confirmation_required` change list word for word,
  and repeat the identical call with `owner_confirmed` true only after the owner's clear yes;
  re-quote if the list changes or the call conflicts. Examples show both steps.
- First approval: show each category's default audience, Public, `share_provisional` and
  `require_review_all`, and say plainly that routine facts are then accepted and shared
  automatically without the owner's review (Revel's own wording is quoted).
- `category_settings` replaces the whole map: send the full current map with only the intended
  edit. Index: the owner-controls sentence matches the skill.

## 0.5.0 (2026-11-03)

- Agent-first owner controls (F5 #29): `revel-profile-discovery` gains an owner-controls
  section for `set_contribution_policy`, `review_pending_fact` and widening with
  `set_fact_audience`. Widening changes need `owner_confirmed` true, passed only after the
  owner clearly agrees in the conversation to the exact changes you quoted; tightening needs no
  flag but is reported. On `owner_confirmation_required`, show the listed changes and ask; on a
  policy version conflict, re-read and ask again if the change differs. The Revel website stays
  an option. Agents are no longer told they can only narrow.

## 0.4.2 (2026-11-02)

- F7's human-contact change is merged: nothing is PROVISIONAL any more.
- `revel-answer-question`: upgrade a plain claim you hold by calling `claim_assignment` with
  `human_contact` true and your current `lease_version` (new `lease_version`, same expiry);
  a plain claim again is still `already_claimed`. `report_user_contact` without a
  human-contact lease fails with `human_contact_required`. The upgrade example and these codes
  are checked against F7's code, with a database test of the upgrade path.

## 0.4.1 (2026-11-02)

- `revel-answer-question`: never contact your user unless a claim with `human_contact` true has
  succeeded and you hold that lease. Decide before claiming; a second claim on your own lease
  is `already_claimed`, which is never permission to contact your user. Upgrading a held claim
  in place and the `human_contact_required` error are marked PROVISIONAL until F7's pending
  change merges. Adds the `duplicate claim` evidence reason.
- `revel-connect-and-read`: the `get_profile` not-found error means not available or no shared
  facts; an empty `facts` list and Public facts for unconnected viewers are explained;
  `provisional_caveat` is listed among the returned fields.

## 0.4.0 (2026-11-02)

- Every operation the guidance names is live; nothing is PROVISIONAL any more. All skills and
  the index are `active`.
- `revel-connect-and-read` matches F6's merged connection tools and F5's shared profile read
  (`get_profile`): exact handles only, identical not-found for hidden, missing and blocking
  handles, the shared 60 per hour lookup limit, directional approval, and a block that looks
  like a decline.
- `revel-giver-workflow` matches F7's `submit_question`, `get_question_status` and `get_answer`:
  `requested_information` holds only the item or fact, `private_purpose` holds the occasion,
  date, budget and ideas, deadlines (72 hours by default, 1 hour to 14 days, `deadline_clamped`),
  stored but unused escalation, and every error code. `no_shareable_answer` carries no reason.
- `revel-answer-question` matches F7's leases and answers: untrusted `minimized_question`,
  identity only from `requester` and `acting_agent`, 15-minute leases with 8 renewals,
  `lease_version` on every call, the exclusive human-contact lease, the 24-hour response window
  and outreach cooldown, decline reasons, evidence (1 to 10 items) and `invalid_evidence`
  reasons.
- Index: "What works today" and the route catalog list every live tool and its HTTP route;
  error formats per area. Discovery and index use `{fact_id}` in fact paths and explain that
  `POST /api/v1/profile/facts` reports rejected items inside a `200`.
- JSON examples in all four skills are checked against the real tool input models.

## 0.3.0 (2026-11-02)

- F5 profile tools are live: `get_contribution_policy`, `add_facts`, `get_my_profile`,
  `set_fact_audience` and `remove_fact`, plus their `/api/v1/profile` routes.
  `revel-profile-discovery` is now `active` and matches F5's merged contract: category ids,
  enums, `involves_others`, the `sensitive` category, outcomes and reason codes. Its JSON
  examples are checked against F5's input model and, with a database, the real service.
- Discovery: `remove_fact` is not idempotent, so re-read the profile instead of retrying;
  HTTP returns `422` for structural errors and `400` for semantic ones.
- Index: "What works today" and the route catalog list the live profile operations.
  Connections, shared profiles and questions remain planned (PROVISIONAL), and the other three
  skills remain drafts.

## 0.2.1 (draft, 2026-11-02)

- Discovery: leave the audience unset for sensitive facts (always Only Me; any other audience is
  rejected).
- Connections: every line describing F6 behavior is marked PROVISIONAL.
- MCP server instructions now come from this package. They name no tools and tell clients to
  use only the tools in their tool list.

## 0.2.0 (draft, 2026-11-02)

- Discovery: sensitive information is never inferred or asked for; it is saved only when the
  owner volunteers it and asks, as `explicit`, Only Me and pending their approval.
- Discovery: omit the audience only for routine facts that fit the default; otherwise choose the
  tighter audience. Set `involves_others` for facts about other people.
- Discovery: report only the outcome Revel returns; handle no approved policy,
  `enrichment_disabled` and `sensitive_not_explicit`. Contradictions stay visible to the owner.
- Questions: `get_answer` and `submit_answer` replace earlier draft names; decline when the
  context is not enough.
- Connections: aligned with F6's tools (handle, requests in both directions, approve with tier,
  decline, block, tier change, revoke, cancel). Hidden handles return the same not-found error;
  a block looks like a decline and the requester never learns the tier.
- Index: HTTP API authentication is not decided; browser and identity routes are not the agent
  path.
- All profile, connection and question operations remain PROVISIONAL.

## 0.1.0 (draft, 2026-11-02)

- First drafts of `index`, `revel-profile-discovery`, `revel-connect-and-read`,
  `revel-giver-workflow` and `revel-answer-question`.
- Live today: MCP OAuth discovery and the `ping` and `whoami` tools. All profile, connection
  and question operations are planned and marked PROVISIONAL.
