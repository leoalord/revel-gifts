---
name: revel-connect-and-read
description: Connect your user to another Revel member by exact handle and read only the profile facts that person has chosen to share.
version: 0.1.0
status: draft
---

# Revel: connect and read a profile

Use this when your user wants gift context about someone else on Revel.

**Availability.** Only the MCP tools `ping` and `whoami` are live today. Every other
operation named below is planned and marked PROVISIONAL; names may change. Do not call an
operation until it appears in your tool list.

## 1. Find the person by exact handle

- Ask your user for the other person's exact Revel handle (for example `@sam`). There is no
  browsing, search or partial matching; do not try variations.
- Call `request_connection` with that handle. It returns minimal identity and creates (or <!-- PROVISIONAL: validate against merged F6 contract -->
  returns) a pending connection request from your user to that person.
- A handle that does not exist, is not discoverable or has blocked your user returns the same
  response as any other unanswered request. Do not guess or tell your user why; say only that
  the request is pending.

## 2. Wait for the other person

- Only the other person can approve, and only for their side: approving lets your user read
  their shared facts, not the reverse.
- They choose the tier: Connections or Trusted Circle. You cannot choose or upgrade it.
- Check status with `list_connections` on a later turn; do not loop or poll rapidly. <!-- PROVISIONAL: validate against merged F6 contract -->

## 3. Read permitted facts

- Call `get_profile` with the handle to read what that person shares with your user. <!-- PROVISIONAL: validate against merged F5/F6 contract -->
- You see only facts permitted for your user's tier. Missing facts may be private; never ask
  Revel or your user to work around that.
- Facts marked provisional are uncertain; repeat the caveat when you use them ("possibly",
  "Revel marks this as unconfirmed").

## 4. Permissions change

- Access is checked on every request. If the person disconnects, narrows a fact or revokes
  access, your next request returns less or nothing. Accept it; do not retry with other wording.
- You cannot recall information you already received, but use it only for your user's current
  gifting purpose. Do not store it as general memory, share it, or reuse it for anything else.

Example (synthetic): Alex asks for gift ideas for Sam. You request a connection to `@sam`.
Two days later `list_connections` shows Sam approved Alex as a connection. <!-- PROVISIONAL: validate against merged F6 contract -->
You read Sam's profile and see "likes trail running" and a provisional "may be learning
pottery". You tell Alex both, with the caveat on the second.
