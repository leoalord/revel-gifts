---
name: revel-giver-workflow
description: Help your user choose a gift for a Revel connection by reading their shared profile, asking a surprise-safe question only when needed, and explaining your ideas.
version: 0.6.0
status: active
---

# Revel giver workflow

Use this when your user wants a gift idea for someone they are connected to on Revel.

The MCP tools used here are live: `get_profile`, `submit_question` and `get_question`. If one is missing from your tool list, stop and tell your user; never say a
question was sent when you could not send it.

## 1. Read the profile first

- Your user needs an approved connection to the recipient (see `revel-connect-and-read`).
- Read the recipient's permitted profile with `get_profile` and their exact handle.
- Note likes, dislikes, owned items, sizes and exclusions. Repeat the caveat on any fact with
  `provisional` true. Many gifts need no question at all.

## 2. Ask only consequential gaps

Ask a question only if the answer would change what you suggest, for example a size, or
whether they already own a specific item.

Call `submit_question` with the recipient's `handle` and:

- `requested_information` (1 to 500 characters): only the item or fact you need, such as
  "Does Alex already own binoculars?". Never put the occasion, a date, the budget, other gift
  ideas, the fact that it is a surprise, or who is asking in it.
- `private_purpose`: the planning context, which only Revel sees and the recipient and their
  assistants never see: `occasion`, `occasion_date`, `budget`, `candidate_ideas` (up to 10) and
  `notes`. The occasion, date and budget go here, never in `requested_information`.
- a deadline: either `expires_at` (a timezone-aware time) or `expires_in_hours`, not both. With
  neither, the question expires after 72 hours. Revel keeps the deadline between 1 hour and 14
  days; if the result has `deadline_clamped` true, tell your user the deadline that applies.
- optionally `escalation_permitted` and `escalation_context` (`urgency` `low`, `normal` or
  `high`, and `notes`). Revel stores them but does not escalate yet.

Revel shows the recipient's assistants your user's verified identity and your assistant's name
itself. Never write who is asking, or on whose behalf, in `requested_information`.

Example (synthetic): Sam wants a birthday gift for Alex, around 80 dollars, and is weighing
binoculars against a birding field guide.

```json submit_question
{"handle": "alex_demo", "requested_information": "Does Alex already own binoculars?", "private_purpose": {"occasion": "birthday", "occasion_date": "2026-12-04", "budget": "80 USD", "candidate_ideas": ["binoculars", "birding field guide"], "notes": "Wants to keep it a surprise"}, "expires_in_hours": 48}
```

Wrong (rejected with `unsafe_requested_information`): the occasion, budget and requester are
in the question.

```json submit_question expect=unsafe_requested_information
{"handle": "alex_demo", "requested_information": "Sam is asking: would Alex like binoculars for her birthday under $80?", "private_purpose": {"occasion": "birthday", "budget": "80 USD"}}
```

The result has `question_id`, `status`, `expires_at`, `deadline_clamped`, the
`minimized_question` Revel will route, and `profile_context` (the facts your user may already
see).

Errors (MCP: `<code>: <message>`; HTTP: `{"detail": {"code", "message"}}`):

- `unsafe_requested_information`: move the occasion, date, budget and planning details into
  `private_purpose`, remove any wording about who is asking, and submit again.
- `invalid_request`: fix the input (for example give exactly one of `handle` or `profile_id`).
- `not_permitted`: you cannot ask about this person. It is the same for every reason (no
  connection, questions turned off, unknown handle); do not probe or retry with variations.
- `rate_limited`: at most 10 questions per hour. Wait before you submit again: over HTTP for
  the seconds in `Retry-After`; over MCP, which gives no time, until a later turn.
- `service_unavailable`: nothing was sent; try again later.

## 3. Get the answer

- On a later turn, call `get_question` with the `question_id`. It returns the question's
  `status` (`open`, `answered`, `declined`, `expired`, `closed` or `cancelled`) with
  `expires_at`, `created_at`, `closed_at` and `answer_available`, and the answer when there is
  one. Do not poll in a tight loop.

  ```json get_question
  {"question_id": "5d2a7b9e-1c3f-4e8a-9b6d-2f4c8e0a1b3d"}
  ```

- Its `result` is:
  - `answer`: use `answer.body`. It comes with its `audience` and minimal `evidence` labels
    (category and `evidence_type`); an `inferred` answer is the respondent's conclusion, so
    say so when you rely on it;
  - `no_shareable_answer`: an answer cannot be shared with your user now. It carries no reason.
    Do not probe, re-ask or rephrase to work around it, and do not guess why. Plan without it;
  - `not_answered`: no answer yet.
- Revel re-checks your user's access and the facts the answer relies on every time you call
  `get_question`, so a later call can return less. Revel records when an answer is retrieved;
  calling again is safe, renews nothing and never extends the deadline.
- If the question is `declined` or `expired`, plan with what you have.

## 4. Suggest gifts

- Generate ideas with your own knowledge and tools; Revel does not pick gifts.
- For each idea, explain which shared facts or answers it relies on.
- Flag unresolved constraints and avoid known exclusions and owned items.
- Verify any price, stock or delivery claim yourself before stating it; say when you could not.

Example (synthetic), continued: Alex's assistant answers "Yes, Alex already has a pair". You
drop the binoculars, suggest the birding field guide and a lens cleaning kit, explain that
both build on "enjoys birdwatching" from Alex's profile, and note you checked current prices
on the retailer's site.

## HTTP equivalent

With a bearer token: `POST /api/questions` (same body as `submit_question`), and for
`get_question` both `GET /api/questions/{question_id}` (the status) and
`GET /api/questions/{question_id}/answer` (the `result` and `answer`). How
assistants obtain a token for the HTTP API is not decided yet; use MCP.
