---
name: revel-profile-discovery
description: Build the user's Revel gifting profile from context you are already authorized to use, with consent, minimal evidence and the tightest sensible audience.
version: 0.3.0
status: active
---

# Revel profile discovery

Use this when your user asks you to set up or add to their own Revel gifting profile.

The MCP tools used here are live: `whoami`, `get_contribution_policy`, `add_facts`,
`get_my_profile`, `set_fact_audience` and `remove_fact`. If one is missing from your tool list,
stop and tell the user; never say something was saved when you could not save it.

## 1. Confirm identity and consent

- Call `whoami` to confirm which Revel account you are connected to. If it fails, stop and ask
  the user to reconnect Revel.
- Ask for explicit consent before you look through any of the user's context. Say what you will
  look at and that you will only send short summaries, never conversations.
- Use only context you can legitimately access. Do not assume you can read saved memories or
  other apps.

## 2. Read the owner's policy first

- Call `get_contribution_policy`. It lists every category with its `effective_audience` (the
  widest audience Revel accepts without the owner's review), `allowed_audiences`,
  `require_review` and `details_schema`.
- If `policy` is null, the owner has not approved a contribution policy yet: everything you add
  waits for their review. Tell them to approve their policy in Revel.
- Call `get_my_profile` to see what Revel already holds, so you do not resubmit known facts.
- You only read the policy. Never try to change it; the owner changes it in Revel.

## 3. Identify useful, durable facts

Look for: interests and hobbies, style, favorite brands, wishes, dislikes, items they already
own, birthday month and day, city or region, sizes, anniversaries, life events, sentimental
context, and foods to avoid.

Never collect or submit:

- raw conversations, quotes, message or conversation IDs, or source details;
- a home or street address, or a birth year;
- numeric confidence scores.

Sensitive information (health, religion, politics, sexual orientation, finances):

- Never infer it and never ask for it.
- Submit it only if the owner volunteers it and asks you to save it: category `sensitive`,
  `evidence_type` `explicit` (anything else is rejected with `sensitive_not_explicit`).
- Leave `audience` unset. Sensitive facts are always Only Me; any other audience is rejected
  with `invalid_field`.
- Tell the owner it stays Only Me and waits for their approval in Revel.

For a food to avoid, use `diet_exclusion` and record only what to avoid ("no peanuts"), never
why.

## 4. Submit facts

Call `add_facts` with `facts`: a list of 1 to 20 items. Each item has:

- `category`: an id from the policy: `interest`, `style`, `favorite_brand`, `wish`, `dislike`,
  `owned_item`, `birthday`, `location`, `size`, `life_event`, `anniversary`, `sentimental`,
  `diet_exclusion` or `sensitive`;
- `statement`: one short sentence about the user (1 to 500 characters), not about the source;
- `details` only where the category's `details_schema` defines them (for `birthday`: `month`
  and `day`, never a year; for `location`: `city`, `region` or `country`, never a street; for
  `size`: `item` and `size`; for `diet_exclusion`: `exclude`);
- `evidence_type`: `explicit` (the user said it), `observed` (seen in their behavior) or
  `inferred` (your conclusion);
- `confidence`: `low`, `medium`, `high` or `unknown` (the default);
- `observed_at` (an ISO date) with `observed_precision` `day`, `month` or `year`; with no date,
  omit both (the precision is then `unknown`);
- `involves_others`: `true` for any fact that mentions or concerns another person;
- `audience` (`only_me`, `trusted_circle`, `connections` or `public`): omitting it applies the
  category's `effective_audience`, the widest audience accepted automatically. Omit it only for
  routine facts that clearly fit that default. When in doubt, always set the tighter audience.

Default audiences (the owner's policy may tighten them):

| Facts | Default |
| --- | --- |
| Interests, style, brands, wishes | Connections (Public only after the owner's policy-level opt-in) |
| Dislikes, owned items, birthday month/day, city/region | Connections |
| Sizes, anniversaries, life events, sentimental context, foods to avoid | Trusted Circle |
| Anything that mentions another person | Set `involves_others`; tightest plausible audience, or omit it to get Only Me |
| Sensitive (volunteered only) | Only Me, always |

Rules:

- Confidence is not permission. A certain fact can still be private.
- Never widen an audience. You may only narrow one, with `set_fact_audience` (`fact_id`,
  `audience`).
- Inferred or low-confidence facts are stored with status `provisional` and shared with a
  caveat.
- A fact with `involves_others` set always waits for the owner's approval. Tell the owner it is
  waiting for them in Revel.

Examples (synthetic). Each block is one `add_facts` item; the outcome assumes the owner has
approved a policy with the default settings.

A routine interest, default audience:

```json add_facts outcome=accepted status=active
{"category": "interest", "statement": "Enjoys trail running", "evidence_type": "explicit", "confidence": "high", "observed_at": "2026-10-01", "observed_precision": "month"}
```

A birthday, month and day only:

```json add_facts outcome=accepted status=active
{"category": "birthday", "statement": "Birthday is March 14", "details": {"month": 3, "day": 14}, "evidence_type": "explicit", "confidence": "high"}
```

A size, with the tighter audience set explicitly:

```json add_facts outcome=accepted status=active
{"category": "size", "statement": "Wears US 9 running shoes", "details": {"item": "running shoes", "size": "US 9"}, "audience": "only_me", "evidence_type": "explicit", "confidence": "high"}
```

Your own conclusion, stored as provisional:

```json add_facts outcome=accepted status=provisional
{"category": "owned_item", "statement": "Probably owns a GPS running watch", "evidence_type": "inferred", "confidence": "medium"}
```

A fact about another person, held for the owner's review (audience defaults to Only Me):

```json add_facts outcome=pending_review status=pending_review
{"category": "life_event", "statement": "Is planning a hiking trip with their sister Sam", "evidence_type": "explicit", "involves_others": true}
```

Sensitive information the owner volunteered and asked you to save:

```json add_facts outcome=pending_review status=pending_review
{"category": "sensitive", "statement": "Is recovering from knee surgery", "evidence_type": "explicit"}
```

Rejected: sensitive information you inferred instead of being told:

```json add_facts outcome=rejected reason_code=sensitive_not_explicit
{"category": "sensitive", "statement": "May be recovering from an injury", "evidence_type": "inferred", "confidence": "low"}
```

## 5. Read each outcome

Each item returns an `outcome` of `accepted`, `pending_review` or `rejected`, with `claim_id`,
`status`, `audience`, `reason_code` and `message`. Report to the owner only the outcome Revel
actually returned:

- `accepted`: stored under the owner's policy, with status `active` or `provisional`.
- `pending_review`: stored but not shared until the owner approves it in Revel (for example a
  sensitive fact, a fact with `involves_others`, an audience wider than the policy allows, no
  approved policy yet, or a category the owner wants to review). Do not resubmit it.
- `rejected` with a `reason_code`:
  - `invalid_field` or `invalid_details`: fix the item and resubmit it once;
  - `unknown_category`: use a category id from the policy (a sensitive topic such as health
    goes in `sensitive`, only if volunteered);
  - `forbidden_category` or `sensitive_not_explicit`: do not retry it in another form;
  - `supersedes_not_found`: the fact you meant to replace is no longer live; read the profile
    again;
  - `enrichment_disabled`: the owner has turned assistant contributions off. Stop and tell them.
- A request that is malformed as a whole (a wrong enum value, a missing `evidence_type`, an
  unknown field) fails entirely with a validation error. Fix it before you send it again.

## 6. Contradictions and corrections

- If a new fact contradicts an existing one, submit it as a new fact; do not delete the old one.
  Both stay visible to the owner, who can correct or remove either.
- To correct a fact, call `add_facts` with `supersedes_id` set to the old fact's id. The old fact
  is retired only when the replacement is accepted.
- If the user asks you to undo something you added, call `remove_fact` with its `fact_id`. It is
  not idempotent: removing an already removed or replaced fact is an error. Do not retry it;
  call `get_my_profile` and check the fact's current status.

## 7. Ask only what matters

After using what you already know, ask the user only questions whose answers would change gift
choices. Often that is zero; never more than five at first. Examples: "Anything you would rather
not receive?" or "What would you like more time or equipment for?"

## 8. Finish with a summary

Tell the owner, briefly, what you added and who can see it, what is waiting for their review,
and how to correct it in Revel. Invite them to connect their other assistants, which may know
different useful details.

Example (synthetic), after the trip fact above came back `pending_review`:

> I added 4 things to your Revel profile. Your connections can see: you enjoy trail running;
> your birthday is March 14; you probably own a GPS watch (marked as unconfirmed). Only you can
> see your shoe size. One item, about your hiking trip with your sister Sam, is waiting for
> your approval in Revel. You can change or remove any of these in Revel.

## HTTP equivalent

The same operations exist over HTTP under `/api/v1/profile` (see `/openapi.json`):
`GET /api/v1/profile/policy`, `POST /api/v1/profile/facts` (body `{"facts": [...]}`),
`GET /api/v1/profile`, `GET /api/v1/profile/summary`,
`PATCH /api/v1/profile/facts/{id}/audience` and `DELETE /api/v1/profile/facts/{id}`. How
assistants authenticate to the HTTP API is not decided yet; use MCP. Structural problems (a
wrong type or enum value, a missing or unknown field) return `422`; semantic field errors return
`400`.
