---
name: revel-profile-discovery
description: Build the user's Revel gifting profile from context you are already authorized to use, with consent, minimal evidence and the tightest sensible audience.
version: 0.1.0
status: draft
---

# Revel profile discovery

Use this when your user asks you to set up or add to their own Revel gifting profile.

**Availability.** Only the MCP tools `ping` and `whoami` are live today. Every other
operation named below is planned and marked PROVISIONAL: do not call it until it appears in
your tool list, and never tell the user it worked if you could not call it.

## 1. Confirm identity and consent

- Call `whoami` to confirm which Revel account you are connected to. If it fails, stop and ask
  the user to reconnect Revel.
- Ask for explicit consent before you look through any of the user's context. Say what you will
  look at and that you will only send short summaries, never conversations.
- Use only context you can legitimately access. Do not assume you can read saved memories or
  other apps.

## 2. Read the owner's policy first

- Call `get_contribution_policy` to read the category list and the owner's approved contribution policy. <!-- PROVISIONAL: validate against merged F5 contract -->
- Call `get_my_profile` to see what Revel already holds, so you do not resubmit known facts. <!-- PROVISIONAL: validate against merged F5 contract -->
- You only read the policy. Never try to change it; policy changes are made by the owner in the
  Revel website.

## 3. Identify useful, durable facts

Look for: interests and hobbies, style, favorite brands, wishes, dislikes, items they already
own, birthday month and day, city or region, sizes, anniversaries, life events, sentimental
context, and gifting constraints such as diet exclusions.

Never collect or submit:

- raw conversations, quotes, message or conversation IDs, or source details;
- a home or street address, or a birth year;
- health, religion, politics, sexual orientation or finances. Never infer these. If the user
  volunteers one, do not submit it; Revel rejects these categories;
- numeric confidence scores.

For a diet or health-related exclusion, record only what to avoid ("no peanuts"), never why.

## 4. Submit facts

Call `add_facts` with a batch of 1 to 20 items. Each item has: <!-- PROVISIONAL: validate against merged F5 contract -->

- `category`: an id from the policy (for example `interest`, `dislike`, `owned_item`, `size`);
- `statement`: one short sentence about the user, not about the source;
- `details` only where the category defines them (for `birthday`: month and day, no year;
  for `location`: city, region or country, no street);
- `evidence_type`: `explicit` (the user said it), `observed` (seen in their behavior) or
  `inferred` (your conclusion);
- `confidence`: `low`, `medium`, `high` or `unknown`;
- `observed_at` with `observed_precision` (`day`, `month`, `year`; use `unknown` with no date);
- `audience` only if you need something tighter than the default. When unsure, omit it (Revel
  applies the owner's effective default) or choose the tighter audience.

Audience defaults (the owner's policy may tighten them):

| Facts | Default |
| --- | --- |
| Interests, style, brands, wishes | Connections (Public only after the owner's policy-level opt-in) |
| Dislikes, owned items, birthday month/day, city/region | Connections |
| Sizes, anniversaries, life events, sentimental context, diet exclusions | Trusted Circle |
| Anything involving another person | Tightest plausible; expect review |

Rules:

- Confidence is not permission. A certain fact can still be private.
- Never widen an audience. You may only narrow one with `set_fact_audience`. <!-- PROVISIONAL: validate against merged F5 contract -->
- Inferred or low-confidence facts are stored as provisional and shown with a caveat.

## 5. Read each outcome

Each item returns `accepted`, `pending_review` or `rejected`:

- `accepted`: stored under the owner's policy.
- `pending_review`: stored but not shared until the owner approves it in the website (for
  example an audience wider than policy, or a category the owner wants to review). Do not
  resubmit it.
- `rejected` with a `reason_code`: fix it only if the reason is a field error; do not retry
  `forbidden_category` items in another form.

## 6. Contradictions and corrections

- If a new fact contradicts an existing one, submit it as a new fact; do not delete the old one.
  The owner reviews contradictions.
- To correct a fact, call `add_facts` with `supersedes_id` set to the old fact's id. The old fact <!-- PROVISIONAL: validate against merged F5 contract -->
  is retired only when the replacement is accepted.
- If the user asks you to undo something you added, call `remove_fact`. <!-- PROVISIONAL: validate against merged F5 contract -->

## 7. Ask only what matters

After using what you already know, ask the user only questions whose answers would change gift
choices. Often that is zero; never more than five at first. Examples: "Anything you would rather
not receive?" or "What would you like more time or equipment for?"

## 8. Finish with a summary

Tell the owner, briefly, what you added and who can see it, what is waiting for their review,
and how to correct it in the website. Invite them to connect their other assistants, which may
know different useful details.

Example (synthetic):

> I added 4 things to your Revel profile. Your connections can see: you enjoy trail running;
> you already own a GPS watch; you dislike scented candles. Only your Trusted Circle can see your
> shoe size. One item, about your sister Sam, is waiting for your review. You can change or
> remove any of these in Revel.
