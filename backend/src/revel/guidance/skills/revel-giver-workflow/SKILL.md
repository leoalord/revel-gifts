---
name: revel-giver-workflow
description: Help your user choose a gift for a Revel connection by reading their shared profile, asking a surprise-safe question only when needed, and explaining your ideas.
version: 0.2.0
status: draft
---

# Revel giver workflow

Use this when your user wants a gift idea for someone they are connected to on Revel.

**Availability.** Only the MCP tools `ping` and `whoami` are live today. Every other
operation named below is planned and marked PROVISIONAL; names may change. Do not call an
operation until it appears in your tool list.

## 1. Read the profile first

- Read the recipient's permitted profile with `get_profile` (see `revel-connect-and-read`). <!-- PROVISIONAL: validate against merged F5/F6 contract -->
- Note likes, dislikes, owned items, sizes and exclusions. Many gifts need no question at all.

## 2. Ask only consequential gaps

- Ask a question only if the answer would change what you suggest (for example, a size, or
  whether they already own a specific item).
- Call `submit_question` with: <!-- PROVISIONAL: validate against merged F7 contract -->
  - `question`: the plain information needed, such as "What size jacket does Sam wear?";
  - `private_purpose`: structured context only Revel sees: occasion, budget, candidate gifts;
  - `deadline`: when your user needs the answer;
  - `escalation`: your user's preference. It is stored but not used yet.
- Put the occasion, the gift, the budget or the fact that it is a surprise ONLY in
  `private_purpose`, never in `question`.
- Never reveal the surprise, occasion or gift in the question or to the recipient.

## 3. Get the answer

- On a later turn, check for the answer with `get_answer`. Do not poll in a tight loop. <!-- PROVISIONAL: validate against merged F7 contract -->
- An answer may carry a provisional caveat; keep it.
- "No shareable answer" carries no reason. Do not probe, rephrase to get around it, or guess
  why. Plan without that detail.
- After the deadline the question expires; plan with what you have.

## 4. Suggest gifts

- Generate ideas with your own knowledge and tools; Revel does not pick gifts.
- For each idea, explain which shared facts it relies on.
- Flag unresolved constraints and avoid known exclusions and owned items.
- Verify any price, stock or delivery claim yourself before stating it; say when you could not.

Example (synthetic): Alex wants a birthday gift for Sam under a modest budget. Sam's profile
says "likes trail running" and "owns a GPS watch". You submit the question "What shoe size
does Sam wear?" with the birthday and budget only in `private_purpose`. Sam's assistant
answers "US 9". You suggest trail running socks and a hydration vest, explain why, and note
you checked current prices on the retailer's site.
