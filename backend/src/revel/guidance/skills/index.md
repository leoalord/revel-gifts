---
name: index
description: What Revel Gifts is, what an assistant can do with it today, how to connect, and which guidance to load.
version: 0.4.2
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
- Your user's own profile: `get_contribution_policy`, `add_facts`, `get_my_profile`,
  `set_fact_audience` and `remove_fact` (see `revel-profile-discovery`).
- Connections and shared profiles: `set_handle`, `request_connection`,
  `list_connection_requests`, `respond_to_connection_request`, `list_connections`,
  `change_connection_tier`, `revoke_connection`, `cancel_connection_request` and `get_profile`
  (see `revel-connect-and-read`).
- Questions, as the asker: `submit_question`, `get_question_status` and `get_answer` (see
  `revel-giver-workflow`).
- Questions, as your user's assistant: `get_updates`, `claim_assignment`, `renew_assignment`,
  `release_assignment`, `report_user_contact`, `decline_assignment` and `submit_answer` (see
  `revel-answer-question`).
- This guidance: as MCP prompts and resources (`revel://guidance/<name>`) and as Markdown at
  `https://api.revel.gifts/guidance`.
- API reference: `https://api.revel.gifts/openapi.json`.

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

Getting a token without MCP OAuth (API-only clients) is not supported yet; a decision is
pending. `/openapi.json` also lists `/me`, `/auth/*` and other browser and identity routes;
they are not the agent integration path.

## Guidance

| Name | Use when |
| --- | --- |
| `revel-profile-discovery` | Your user wants to build or add to their own profile |
| `revel-connect-and-read` | Your user wants to connect to someone and read their shared profile |
| `revel-giver-workflow` | Your user wants a gift idea for a connection |
| `revel-answer-question` | Revel routes a question about your user to you |

Read each at `https://api.revel.gifts/guidance/<name>.md`, or as the MCP prompt or resource of
the same name. Changes are listed in `https://api.revel.gifts/guidance/CHANGELOG.md`.

## Route catalog

MCP tools and the matching HTTP routes (bearer token):

| Purpose | MCP tool | HTTP |
| --- | --- | --- |
| Read categories and the owner's contribution policy | `get_contribution_policy` | `GET /api/v1/profile/policy` |
| Contribute facts (and corrections with `supersedes_id`) | `add_facts` | `POST /api/v1/profile/facts` |
| Read your user's own profile and summary | `get_my_profile` | `GET /api/v1/profile` |
| Contribution summary | (in `get_my_profile`) | `GET /api/v1/profile/summary` |
| Narrow a fact's audience | `set_fact_audience` | `PATCH /api/v1/profile/facts/{fact_id}/audience` |
| Remove a fact (not idempotent) | `remove_fact` | `DELETE /api/v1/profile/facts/{fact_id}` |
| Read a person's shared profile by exact handle | `get_profile` | `GET /api/v1/profiles/{handle}` |
| Set your user's handle and discoverability | `set_handle` | MCP only |
| Request a connection by exact handle | `request_connection` | MCP only |
| List incoming or outgoing requests | `list_connection_requests` | MCP only |
| Approve (with tier), decline or block a request | `respond_to_connection_request` | MCP only |
| List people your user approved | `list_connections` | MCP only |
| Change a tier, revoke access or cancel your own request | `change_connection_tier`, `revoke_connection`, `cancel_connection_request` | MCP only |
| Ask a question | `submit_question` | `POST /api/questions` |
| Check a question you asked | `get_question_status` | `GET /api/questions/{question_id}` |
| Get the answer to a question you asked | `get_answer` | `GET /api/questions/{question_id}/answer` |
| Get assigned work | `get_updates` | `GET /api/updates` |
| Claim an assignment | `claim_assignment` | `POST /api/assignments/{assignment_id}/claim` |
| Renew a lease | `renew_assignment` | `POST /api/assignments/{assignment_id}/renew` |
| Release a lease | `release_assignment` | `POST /api/assignments/{assignment_id}/release` |
| Report that you asked your user | `report_user_contact` | `POST /api/assignments/{assignment_id}/user-contact` |
| Decline an assignment | `decline_assignment` | `POST /api/assignments/{assignment_id}/decline` |
| Answer a question | `submit_answer` | `POST /api/assignments/{assignment_id}/answer` |

The HTTP routes take a bearer token, but how assistants obtain one for the HTTP API is not
decided yet; use MCP. Connections have no bearer HTTP API; the Revel website's own connection
routes are not for assistants.

Errors differ by area:

- Profile: `POST /api/v1/profile/facts` returns `200` with one outcome per item, including
  `rejected` items with their `reason_code`; `400` means the request as a whole was invalid,
  and `422` is a structural problem (a wrong type or enum value, a missing or unknown field).
  MCP tool errors are fixed messages.
- Shared profiles: `404` (MCP: "No Revel profile is available to you under that handle") means
  not available, for every reason.
- Connections: MCP tool errors are fixed, generic messages (see `revel-connect-and-read`).
- Questions: MCP errors read `<code>: <message>`; HTTP errors are
  `{"detail": {"code", "message"}}`, with `Retry-After` on `rate_limited` and
  `outreach_cooldown`.

Agents can never change the owner's contribution policy, widen an audience or approve
pending items; owners do that in the Revel website.
