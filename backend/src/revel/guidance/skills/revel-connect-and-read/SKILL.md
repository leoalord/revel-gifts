---
name: revel-connect-and-read
description: Connect your user to another Revel member by exact handle, handle requests addressed to your user, and read only the profile facts that person has chosen to share.
version: 0.3.0
status: draft
---

# Revel: connect and read a profile

Use this when your user wants gift context about someone else on Revel, or when someone has
asked to connect with your user.

**Availability.** Only the MCP tools `ping` and `whoami` are live today. Every other
operation named below is planned and marked PROVISIONAL; names may change. Do not call an
operation until it appears in your tool list.

## How connections work

- Approval is directional: when Sam approves Alex, Alex can read what Sam shares. Sam sees <!-- PROVISIONAL: validate against merged F6 contract -->
  Alex's profile only if Sam asks separately and Alex approves. <!-- PROVISIONAL: validate against merged F6 contract -->
- The person approving picks the tier: Connections or Trusted Circle (which sees more). <!-- PROVISIONAL: validate against merged F6 contract -->
- Errors are fixed, generic messages. "Temporarily unavailable" means nothing changed; retry <!-- PROVISIONAL: validate against merged F6 contract -->
  later. <!-- PROVISIONAL: validate against merged F6 contract -->

## Asking to connect (your user wants to read someone's profile)

- Your user needs their own handle first; set it with `set_handle` only after asking them. <!-- PROVISIONAL: validate against merged F6 contract -->
- Ask your user for the other person's exact Revel handle. There is no browsing, search or <!-- PROVISIONAL: validate against merged F6 contract -->
  partial matching. <!-- PROVISIONAL: validate against merged F6 contract -->
- Call `request_connection` with `handle`. It returns the person's minimal identity (display <!-- PROVISIONAL: validate against merged F6 contract -->
  name, handle, avatar) and the pending request, or the request already open. <!-- PROVISIONAL: validate against merged F6 contract -->
- A handle that does not exist, is not discoverable, or whose owner blocked your user returns <!-- PROVISIONAL: validate against merged F6 contract -->
  the same not-found error. Infer nothing from it and do not try variations; tell your user <!-- PROVISIONAL: validate against merged F6 contract -->
  only that no one is available under that handle. <!-- PROVISIONAL: validate against merged F6 contract -->
- Check progress on a later turn with `list_connection_requests` and `direction` `outgoing`. It <!-- PROVISIONAL: validate against merged F6 contract -->
  lists only pending and approved requests; a declined or blocked request simply disappears. <!-- PROVISIONAL: validate against merged F6 contract -->
  Do not tell your user they were blocked, and do not guess the tier they were given. <!-- PROVISIONAL: validate against merged F6 contract -->
- To withdraw a pending request, call `cancel_connection_request` with its `request_id`. <!-- PROVISIONAL: validate against merged F6 contract -->

## Answering requests (someone wants to read your user's profile)

- Call `list_connection_requests` (`direction` `incoming`) to see who asked and through which <!-- PROVISIONAL: validate against merged F6 contract -->
  assistant. <!-- PROVISIONAL: validate against merged F6 contract -->
- Ask your user before you respond. Then call `respond_to_connection_request` with the <!-- PROVISIONAL: validate against merged F6 contract -->
  `request_id` and `action` `approve` (with `tier` `connections` or `trusted_circle`), `decline` <!-- PROVISIONAL: validate against merged F6 contract -->
  or `block`. Your user can also do this in Revel. <!-- PROVISIONAL: validate against merged F6 contract -->
- `list_connections` shows the people your user approved, with their tier and `request_id`. <!-- PROVISIONAL: validate against merged F6 contract -->
  With your user's confirmation, change a tier with `change_connection_tier` or remove access <!-- PROVISIONAL: validate against merged F6 contract -->
  with `revoke_connection`. <!-- PROVISIONAL: validate against merged F6 contract -->

## Reading a shared profile

- Call `get_profile` with the handle to read what that person shares with your user. <!-- PROVISIONAL: validate against merged F5/F6 contract -->
- You see only facts permitted for your user's tier. Missing facts may be private; never try to
  work around that.
- Facts marked provisional are uncertain; repeat the caveat when you use them ("possibly",
  "Revel marks this as unconfirmed").
- Access is checked on every request. After a revoke, tier change or narrowed fact, your next
  request returns less or nothing. Accept it; do not retry with other wording.
- You cannot recall information you already received, but use it only for your user's current
  gifting purpose. Do not store it as general memory, share it, or reuse it for anything else.

Example (synthetic): Alex asks for gift ideas for Sam. You call `request_connection` with <!-- PROVISIONAL: validate against merged F6 contract -->
handle "sam". Two days later the outgoing request shows as approved. You read Sam's profile
and see "likes trail running" and a provisional "may be learning pottery". You tell Alex both,
with the caveat on the second.
