---
name: index
description: What Revel Gifts is, what an assistant can do with it today, how to connect, and which guidance to load.
version: 0.2.1
status: draft
---

# Revel Gifts agent guidance

Revel Gifts keeps a permission-controlled gifting profile for each person. Their own assistants
add what they already know; assistants of people they connect with read only what is shared,
and can ask surprise-safe questions.

## What works today

- MCP endpoint: `https://api.revel.gifts/mcp` (Streamable HTTP, no trailing slash).
- MCP tools: `ping` (connectivity check) and `whoami` (which Revel user and assistant you are
  connected as).
- This guidance: as MCP prompts and resources (`revel://guidance/<name>`) and as Markdown at
  `https://api.revel.gifts/guidance`.
- API reference: `https://api.revel.gifts/openapi.json`.

Everything else below is **planned, not yet available**. Profiles, connections and questions
cannot be used yet. Do not tell your user they were saved or sent.

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

Planned, not yet available. Names and paths may change before release.

| Purpose | MCP tool | HTTP (bearer) |
| --- | --- | --- |
| Read categories and the owner's contribution policy | `get_contribution_policy` <!-- PROVISIONAL: validate against merged F5 contract --> | `GET /api/v1/profile/policy` <!-- PROVISIONAL: validate against merged F5 contract --> |
| Contribute facts (and corrections) | `add_facts` <!-- PROVISIONAL: validate against merged F5 contract --> | `POST /api/v1/profile/facts` <!-- PROVISIONAL: validate against merged F5 contract --> |
| Read your own profile and summary | `get_my_profile` <!-- PROVISIONAL: validate against merged F5 contract --> | `GET /api/v1/profile` <!-- PROVISIONAL: validate against merged F5 contract --> |
| Contribution summary | (in `get_my_profile`) <!-- PROVISIONAL: validate against merged F5 contract --> | `GET /api/v1/profile/summary` <!-- PROVISIONAL: validate against merged F5 contract --> |
| Narrow a fact's audience | `set_fact_audience` <!-- PROVISIONAL: validate against merged F5 contract --> | `PATCH /api/v1/profile/facts/{id}/audience` <!-- PROVISIONAL: validate against merged F5 contract --> |
| Remove a fact | `remove_fact` <!-- PROVISIONAL: validate against merged F5 contract --> | `DELETE /api/v1/profile/facts/{id}` <!-- PROVISIONAL: validate against merged F5 contract --> |
| Set your user's handle and discoverability | `set_handle` <!-- PROVISIONAL: validate against merged F6 contract --> | to be published <!-- PROVISIONAL --> |
| Request a connection by exact handle | `request_connection` <!-- PROVISIONAL: validate against merged F6 contract --> | to be published <!-- PROVISIONAL --> |
| List incoming or outgoing requests | `list_connection_requests` <!-- PROVISIONAL: validate against merged F6 contract --> | to be published <!-- PROVISIONAL --> |
| Approve (with tier), decline or block a request | `respond_to_connection_request` <!-- PROVISIONAL: validate against merged F6 contract --> | to be published <!-- PROVISIONAL --> |
| List people your user approved | `list_connections` <!-- PROVISIONAL: validate against merged F6 contract --> | to be published <!-- PROVISIONAL --> |
| Change a tier, revoke access or cancel your own request | `change_connection_tier`, `revoke_connection`, `cancel_connection_request` <!-- PROVISIONAL: validate against merged F6 contract --> | to be published <!-- PROVISIONAL --> |
| Read a connection's shared profile | `get_profile` <!-- PROVISIONAL: validate against merged F5/F6 contract --> | to be published <!-- PROVISIONAL --> |
| Ask a question | `submit_question` <!-- PROVISIONAL: validate against merged F7 contract --> | to be published <!-- PROVISIONAL --> |
| Get the answer to a question you asked | `get_answer` <!-- PROVISIONAL: validate against merged F7 contract --> | to be published <!-- PROVISIONAL --> |
| Get assigned work | `get_updates` <!-- PROVISIONAL: validate against merged F7 contract --> | to be published <!-- PROVISIONAL --> |
| Claim, renew, release or decline an assignment | `claim_assignment`, `renew_assignment`, `release_assignment`, `decline_assignment` <!-- PROVISIONAL: validate against merged F7 contract --> | to be published <!-- PROVISIONAL --> |
| Report that you asked your user | `report_user_contact` <!-- PROVISIONAL: validate against merged F7 contract --> | to be published <!-- PROVISIONAL --> |
| Answer a question | `submit_answer` <!-- PROVISIONAL: validate against merged F7 contract --> | to be published <!-- PROVISIONAL --> |

How to authenticate to the planned HTTP API is not decided yet.
Agents can never change the owner's contribution policy, widen an audience or approve
pending items; owners do that in the Revel website.
