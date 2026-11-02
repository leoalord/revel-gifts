---
name: revel-answer-question
description: Answer a question Revel routes to you about your user with minimal evidence, and coordinate any contact with your user through the exclusive lease.
version: 0.2.1
status: draft
---

# Revel: answer a question about your user

Use this when Revel routes a question about your user to you.

**Availability.** Only the MCP tools `ping` and `whoami` are live today. Every other
operation named below is planned and marked PROVISIONAL; names may change. Do not call an
operation until it appears in your tool list.

## 1. Check for work

- Call `get_updates` when your user interacts with you. Each item has a `type`; today only <!-- PROVISIONAL: validate against merged F7 contract -->
  question assignments are expected.
- Each question shows the verified person asking and the assistant acting for them. You do not
  see their private purpose; do not ask for it.

## 2. Answer from what you already know

- First answer from authorized knowledge you already have, including your user's Revel profile.
  Do not contact your user if you can answer without them.
- Call `submit_answer` with the answer and an `evidence` list. Each evidence item is either: <!-- PROVISIONAL: validate against merged F7 contract -->
  - a reference to an existing profile fact id, or
  - inline evidence with only `category`, proposed `audience`, `evidence_type` and an
    approximate observed date (a month is enough).
- Never include quotes, raw conversation, source details or extra context. Never reveal more
  than the answer requires.
- The answer can be no more visible than its evidence: Revel uses the most restrictive audience.
  Never propose an audience wider than the owner's policy.
- If you learned a new durable fact, contribute it separately with `add_facts` under the <!-- PROVISIONAL: validate against merged F5 contract -->
  owner's policy (see `revel-profile-discovery`).
- If the question is inappropriate or unsafe to answer, or the context you were given is not <!-- PROVISIONAL: validate against merged F7 contract -->
  enough to answer it appropriately, call `decline_assignment`. Do not explain your user's <!-- PROVISIONAL: validate against merged F7 contract -->
  reasons.

## 3. Asking your user: the lease

Only one assistant may contact your user about a question.

- Before you ask your user, check prior outreach and cooldowns shown on the assignment. If
  another assistant holds the lease or a cooldown applies, do not contact your user.
- Call `claim_assignment` with the version you read. If the claim fails, re-read with <!-- PROVISIONAL: validate against merged F7 contract -->
  `get_updates`; another assistant may have it. <!-- PROVISIONAL: validate against merged F7 contract -->
- After you ask your user, call `report_user_contact` (the assignment becomes <!-- PROVISIONAL: validate against merged F7 contract -->
  `waiting_for_user`). Say whether delivery was confirmed.
- Renew with `renew_assignment` before the lease expires while you still expect an answer. <!-- PROVISIONAL: validate against merged F7 contract -->
- If you cannot continue, call `release_assignment` so another assistant can help. <!-- PROVISIONAL: validate against merged F7 contract -->
- Completions with a stale or expired lease, and answers after the question deadline, are
  rejected. Re-read the current state; do not retry blindly.

Example (synthetic): Revel routes "What size jacket does Alex wear?" from Sam, via Sam's
assistant. Alex's profile has no size. No other assistant holds the lease and no cooldown
applies, so you claim it, ask Alex, report the contact, and answer "Medium" with inline
evidence: category `size`, audience `trusted_circle`, `explicit`, observed this month.
