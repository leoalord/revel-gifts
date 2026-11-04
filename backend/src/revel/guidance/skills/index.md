---
name: index
description: What Revel Gifts is, what an assistant can do with it today, how to connect, and which guidance to load.
version: 0.7.2
status: active
---

# Revel Gifts agent guidance

Revel Gifts keeps a permission-controlled gifting profile for each person. Their own assistants
add what they already know; assistants of people they connect with read only what is shared,
and can ask surprise-safe questions.

## What works today

- MCP endpoint: `https://api.revel.gifts/mcp` (Streamable HTTP, no trailing slash).
- `ping` (connectivity check) and `whoami` (which Revel user and assistant you are connected
  as).
- Your user's own profile: `get_profile` (no `handle`: facts, summary and contribution
  policy), `set_contribution_policy`, `add_facts`, `update_fact` (actions `set_audience` and
  `approve`) and `remove_fact` (actions `remove` for a live fact and `reject` for pending
  review) (see
  `revel-manage-profile`).
- Connections and shared profiles: `set_handle`, `request_connection`, `list_connections`
  (connections and requests), `respond_to_connection_request` (actions `approve`, `decline`,
  `block` and `change_tier`), `end_connection` (actions `revoke` and `cancel`) and
  `get_profile` with a `handle` (see `revel-connect-and-read`).
- Questions, as the asker: `submit_question` and `get_question` (status and answer; see
  `revel-find-a-gift`).
- Questions, as your user's assistant: `get_updates`, `update_assignment` (actions `claim`,
  `renew`, `release`, `decline` and `report_user_contact`) and `submit_answer` (see
  `revel-answer-question`).
- This guidance: as MCP prompts and resources (`revel://guidance/<name>`) and as Markdown at
  `https://api.revel.gifts/guidance`.
- HTTP API reference, for assistants without MCP access: `https://api.revel.gifts/openapi.json`
  (see "HTTP API" below).

Use only the tools in your tool list. Never tell your user a request, question or answer was
sent unless Revel confirmed it.

## Connecting (MCP OAuth)

1. Send an MCP request to `https://api.revel.gifts/mcp`. Without a valid token you get `401`
   with `WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication
   required", resource_metadata="https://api.revel.gifts/.well-known/oauth-protected-resource/mcp"`.
   Missing, malformed, expired, wrong-audience and disconnected tokens all get this same
   response.
2. Fetch `GET /.well-known/oauth-protected-resource/mcp`. It returns `resource` and
   `authorization_servers`. Follow `authorization_servers`; do not hard-code the
   authorization server host.
3. Register your client with Dynamic Client Registration or a Client ID Metadata Document.
4. Run the authorization code flow with PKCE (`S256`) and
   `resource=https://api.revel.gifts/mcp`. Your user signs in on the hosted sign-in page.
5. Call `/mcp` with `Authorization: Bearer <access token>`. If you get `401` again, repeat the
   flow; your user may have disconnected you.

## HTTP API

The HTTP API is for assistants without MCP access. Access for them is not available yet
(issuing them tokens is deferred), so use MCP, the supported path today. People never use the
HTTP API themselves: they use their assistant or the Revel website. The route catalog below is
the reference for assistants without MCP access. `/openapi.json` declares the bearer scheme
(`bearerAuth`) on these routes: the token is the access token an assistant gets by connecting
over MCP (see "Connecting"), and there is no separate token flow yet. `/openapi.json` also
lists `/me`, `/auth/*` and other browser and identity routes; they are not the agent
integration path.

## Guidance

| Name | Use when |
| --- | --- |
| `revel-manage-profile` | Your user wants to build, maintain or edit their own profile, or change who can see it |
| `revel-connect-and-read` | Your user wants to connect to someone and read their shared profile |
| `revel-find-a-gift` | Your user wants a gift idea for a connection |
| `revel-answer-question` | Revel routes a question about your user to you |

Read each at `https://api.revel.gifts/guidance/<name>.md`, or as the MCP prompt or resource of
the same name. Changes are listed in `https://api.revel.gifts/guidance/CHANGELOG.md`.

## Route catalog

MCP tools and the matching HTTP routes. The HTTP column is the reference for assistants
without MCP access (bearer token; see "HTTP API" above):

| Purpose | MCP tool | HTTP |
| --- | --- | --- |
| Read categories and the owner's contribution policy | `get_profile` (no `handle`; `policy`) | `GET /api/v1/profile/policy` |
| Contribute facts (and corrections with `supersedes_id`) | `add_facts` | `POST /api/v1/profile/facts` |
| Read your user's own profile and summary | `get_profile` (no `handle`) | `GET /api/v1/profile` |
| Contribution summary | (in `get_profile`, no `handle`) | `GET /api/v1/profile/summary` |
| Change the owner's contribution policy (widening needs the owner's confirmation) | `set_contribution_policy` | `PUT /api/v1/profile/policy` |
| Change a fact's audience (widening needs the owner's confirmation) | `update_fact` (action `set_audience`) | `PATCH /api/v1/profile/facts/{fact_id}/audience` |
| Approve a pending fact (needs the owner's confirmation) | `update_fact` (action `approve`) | `POST /api/v1/profile/facts/{fact_id}/approve` |
| Reject a pending fact | `remove_fact` (action `reject`) | `POST /api/v1/profile/facts/{fact_id}/reject` |
| Remove a live fact (not idempotent) | `remove_fact` (action `remove`) | `DELETE /api/v1/profile/facts/{fact_id}` |
| Read a person's shared profile by exact handle | `get_profile` (with `handle`) | `GET /api/v1/profiles/{handle}` |
| Set your user's handle and discoverability | `set_handle` | MCP only |
| Request a connection by exact handle | `request_connection` | MCP only |
| List incoming or outgoing requests | `list_connections` (`include` `requests`) | MCP only |
| Approve (with tier; needs the owner's confirmation), decline or block a request | `respond_to_connection_request` (action `approve`, `decline` or `block`) | MCP only |
| List people your user approved | `list_connections` (`include` `connections`) | MCP only |
| Change a tier (upgrading to Trusted Circle needs the owner's confirmation) | `respond_to_connection_request` (action `change_tier`) | MCP only |
| Revoke access or cancel your own request | `end_connection` (action `revoke` or `cancel`) | MCP only |
| Ask a question | `submit_question` | `POST /api/questions` |
| Check a question you asked | `get_question` | `GET /api/questions/{question_id}` |
| Get the answer to a question you asked | `get_question` | `GET /api/questions/{question_id}/answer` |
| Get assigned work | `get_updates` | `GET /api/updates` |
| Claim an assignment | `update_assignment` (action `claim`) | `POST /api/assignments/{assignment_id}/claim` |
| Renew a lease | `update_assignment` (action `renew`) | `POST /api/assignments/{assignment_id}/renew` |
| Release a lease | `update_assignment` (action `release`) | `POST /api/assignments/{assignment_id}/release` |
| Report that you asked your user | `update_assignment` (action `report_user_contact`) | `POST /api/assignments/{assignment_id}/user-contact` |
| Decline an assignment | `update_assignment` (action `decline`) | `POST /api/assignments/{assignment_id}/decline` |
| Answer a question | `submit_answer` | `POST /api/assignments/{assignment_id}/answer` |

The HTTP API is for assistants without MCP access. Access for them is not available yet
(issuing them tokens is deferred), so use MCP, the supported path today. Connections have no
bearer HTTP API; the Revel website's own connection
routes are not for assistants.

Errors differ by area:

- Profile: `POST /api/v1/profile/facts` returns `200` with one outcome per item, including
  `rejected` items with their `reason_code`; `400` means the request as a whole was invalid,
  and `422` is a structural problem (a wrong type or enum value, a missing or unknown field).
  MCP tool errors are fixed messages, except `owner_confirmation_required` and the
  `invalid_request` per-action errors of `update_fact`.
- Shared profiles: `404` (MCP: "No Revel profile is available to you under that handle") means
  not available, for every reason.
- Connections: MCP tool errors are fixed, generic messages, except `owner_confirmation_required`,
  which lists the changes to quote, and `invalid_request` for a wrong `action`, `include` or
  parameter (see `revel-connect-and-read`).
- Questions: MCP errors read `<code>: <message>`; HTTP errors are
  `{"detail": {"code", "message"}}`, with `Retry-After` on `rate_limited` and
  `outreach_cooldown`.

Approving the first policy, widening the policy or a fact's audience, approving a pending
item, approving a connection request or upgrading a connection to Trusted Circle needs the owner's explicit agreement in the
conversation to the exact changes Revel lists; tightening, declining, blocking, downgrading,
revoking and cancelling do not, but tell the owner. See `revel-manage-profile` and
`revel-connect-and-read`. Owners can also do this in the Revel website.
