---
name: revel-connect-and-read
description: Connect your user to another Revel member by exact handle, handle requests addressed to your user, and read only the profile facts that person has chosen to share.
version: 0.4.2
status: active
---

# Revel: connect and read a profile

Use this when your user wants gift context about someone else on Revel, or when someone has
asked to connect with your user.

The MCP tools used here are live: `set_handle`, `request_connection`,
`list_connection_requests`, `respond_to_connection_request`, `list_connections`,
`change_connection_tier`, `revoke_connection`, `cancel_connection_request` and `get_profile`.
If one is missing from your tool list, stop and tell your user; never say a request was sent
when you could not send it.

## How connections work

- Approval is directional: when Sam approves Alex, Alex can read what Sam shares. Sam sees
  Alex's profile only if Sam sends a separate request and Alex approves it.
- The person approving picks the tier: `connections` or `trusted_circle` (which sees more).
  The requester is never told the tier.
- Errors are fixed, generic messages. Never read meaning into their wording beyond this:
  - "temporarily unavailable": nothing was changed; retry later;
  - "try again later" (rate limited): wait before you try again; do not retry in a loop.
- Handle lookups share one limit of 60 per hour per user: every `request_connection`, every
  `set_handle` that includes a `handle`, and every `get_profile` that finds no one. Ask your
  user for the exact handle instead of trying guesses.

## Asking to connect (your user wants to read someone's profile)

- Your user needs their own handle first. Ask them which handle they want, then call
  `set_handle` with `handle` (3 to 30 lowercase letters, digits or underscores). "That handle is
  unavailable" means it is taken; ask your user for another. `discoverable` (on by default)
  controls whether others can find your user by exact handle; change it only when they ask.
- Ask your user for the other person's exact Revel handle. There is no browsing, search or
  partial matching.
- Call `request_connection` with `handle`. It returns the person's minimal identity
  (`display_name`, `handle`, `avatar_url`) and the pending request, or the request already open
  (`created` is false).
- A handle that does not exist, is not discoverable, or whose owner blocked your user returns
  the same not-found error. Infer nothing from it and do not try variations; tell your user
  only that no one is available under that handle.
- Check progress on a later turn with `list_connection_requests` and `direction` `outgoing`. It
  lists only `pending` and `approved` requests; a declined or blocked request simply
  disappears. Never tell your user they were blocked (say the request was not approved), and do
  not guess the tier they were given.
- To withdraw a pending request, call `cancel_connection_request` with its `request_id`.

Example (synthetic), the `request_connection` input:

```json request_connection
{"handle": "sam_rivers"}
```

## Answering requests (someone wants to read your user's profile)

- Call `list_connection_requests` (`direction` `incoming`, the default) to see who asked and
  through which assistant.
- Ask your user before you respond. Then call `respond_to_connection_request` with the
  `request_id` and `action` `approve` (with `tier` `connections` or `trusted_circle`),
  `decline` or `block`. It is not safe to repeat: a second response fails. Your user can also
  respond in the Revel website.
- Approving lets that person read your user's profile; it does not let your user read theirs.
- `list_connections` shows the people your user approved, with their `tier` and `request_id`.
  With your user's confirmation, change a tier with `change_connection_tier` or remove access
  with `revoke_connection`. Both take effect on that person's next request.

Example (synthetic), approving at the Connections tier after your user agreed:

```json respond_to_connection_request
{"request_id": "6f1d2b8e-3c4a-4e5f-9a7b-1c2d3e4f5a6b", "action": "approve", "tier": "connections"}
```

## Reading a shared profile

- Call `get_profile` with the exact `handle` to read what that person shares with your user.
  You get their `display_name`, `handle`, `avatar_url`, `facts` and `provisional_caveat`;
  nothing else.
- "No Revel profile is available to you under that handle" means not available, or no shared
  facts for your user. It is the same whether the handle does not exist, is hidden, or has not
  connected with your user. Do not speculate about why and do not try other handles.
- An empty `facts` list means the person shares nothing with your user yet. Someone not
  connected to your user may still share facts they made Public.
- You see only facts permitted for your user's tier. Missing facts may be private; never try to
  work around that.
- A fact with `provisional` true is not confirmed by that person, and `provisional_caveat`
  explains it. Say so whenever you use it ("possibly", "Revel marks this as unconfirmed").
- Access is checked on every request. After a revoke, tier change or narrowed fact, your next
  request returns less or nothing. Accept it; do not retry with other wording.
- You cannot recall information you already received, but use it only for your user's current
  gifting purpose. Do not store it as general memory, share it, or reuse it for anything else.

```json get_profile
{"handle": "sam_rivers"}
```

Example (synthetic): Alex asks for gift ideas for Sam. You call `request_connection` with
handle "sam_rivers". Two days later the outgoing request shows as `approved`. You read Sam's
profile with `get_profile` and see "likes trail running" and a provisional "may be learning
pottery". You tell Alex both, with the caveat on the second.

## HTTP equivalent

`GET /api/v1/profiles/{handle}` returns the same shared profile with a bearer token; a `404`
means not available, exactly like the MCP error. Connections have no bearer HTTP API: the
Revel website's own connection routes are not for assistants, so use the MCP tools above.
