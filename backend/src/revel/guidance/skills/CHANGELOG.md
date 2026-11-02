---
name: CHANGELOG
description: Version history of the Revel agent guidance.
version: 0.3.0
status: draft
---

# Revel agent guidance changelog

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
- Questions: `get_answer` and `submit_answer` replace earlier draft names; decline when the <!-- PROVISIONAL: validate against merged F7 contract -->
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
