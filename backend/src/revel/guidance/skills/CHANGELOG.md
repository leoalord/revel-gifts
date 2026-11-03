---
name: CHANGELOG
description: Version history of the Revel agent guidance.
version: 0.5.1
status: active
---

# Revel agent guidance changelog

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
