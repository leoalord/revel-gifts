# Recipient profiling and preference discovery

Research brief • 31 October 2026

## Recommendation

Build a recipient-controlled, evidence-backed profile that existing assistants can contribute to only within the recipient's authorization. Start each gifting occasion with the authorized, relevant knowledge already available. Ask only the highest-value unanswered questions, typically zero to three and never more than five in the initial discovery flow. This is a product hypothesis to test, not a number established by the literature. A profile is useful when it supports a defensible choice for this occasion, not when every field is complete.

The giver's budget, occasion and deadline belong to the current gift request. Recipient preferences belong to the recipient's profile; sharing them with a giver requires separate recipient-controlled permission. An assistant's access to private information is not itself permission to transfer it or reveal it to a giver. If no authorized contribution is available, work with information voluntarily supplied by the giver, label its source and uncertainty, and offer a way to ask the recipient without exposing a surprise.

## Critical reading of the gifting brief

[Gift-giving psychology](01-gifting-psychology.md) rightly emphasizes explicit requests, feasibility, uncertainty, and recipient outcomes rather than giver excitement. It also properly treats surprise, sentiment and experiences as context-dependent. Its broad list of possible facts should not become an onboarding checklist: sizing, accessibility, customs and possessions matter only when a candidate or occasion makes them relevant. Nor does the gift-request evidence prove that comprehensive profiling, persistent storage, assistant sharing, or a particular question count improves gifts. The cited experiments often use constrained choices or hypothetical scenarios; adapt them to the recipient's situation and test with real exchanges. A giver's interpretation of a recipient's interests is not equivalent to the recipient's own request.

## Evidence and its limits

- **Select questions for the decision.** Christakopoulou and colleagues' restaurant recommender evaluated synthetic data and preferences collected from 28 participants; its offline simulations reported improved recommendation performance after two questions. The restaurant domain and simulation do not establish two questions as enough for gifts, but support asking context-dependent questions that change the candidate set. [KDD 2016 paper](https://www.kdd.org/kdd2016/papers/files/rfp0063-christakopoulouA.pdf).
- **A short, diverse set can outperform a longer one in a modeled setting.** Sepliarskaia and colleagues evaluated a static relative-preference questionnaire on two datasets using simulated behavior and revealed real judgments, reporting up to a threefold reduction in questionnaire length against their baselines. This is an offline model result, not a live gifting completion or satisfaction result; adaptive questions are not automatically superior to a well-designed static set. [RecSys 2018 research record](https://research.google/pubs/preference-elicitation-as-an-optimization-problem/).
- **Ask about use when specifications are unfamiliar.** Kostric and colleagues generated usage-related questions from product reviews and evaluated sentence selection and question generation with annotated data. This establishes feasibility of generating questions, not that the questions improve gift outcomes. [RecSys 2021 research record](https://research.google/pubs/soliciting-user-preferences-in-conversational-recommender-systems-via-usage-related-questions/).
- **Natural language can be useful at cold start.** Sanner and colleagues collected language and item preferences and ratings, finding competitive near-cold-start recommendation performance for language-based LLM approaches in their evaluation. This does not establish factual reliability, consent, or performance for third-party gift recipients. [RecSys 2023 research record](https://research.google/pubs/large-language-models-are-competitive-near-cold-start-recommenders-for-language-and-item-based-preferences/).
- **A direct request is strong evidence, not a mandate.** Five gift-request studies found recipients more appreciative of requested gifts than unrequested alternatives in the tested settings. Current, recipient-authored wishes should therefore be prominent; the recipient can still prefer discovery or surprise. [Gino and Flynn, 2011](https://static.tumblr.com/5tnincw/jCap15er9/gifts-explicit-ginoflynn-2011.pdf).

## Profile categories

Priority describes decision value, not mandatory collection. Unknown is a valid value throughout.

| Priority | Category | Useful examples and boundaries |
|---|---|---|
| Essential when relevant | Current wants and explicit exclusions | Requested items or experiences, links, desired contribution, unwanted gift types; distinguish a wish from an item already purchased. |
| Essential when relevant | Candidate-specific constraints | A disclosed allergy for food, access needs for an experience, size for clothing, location and dates for tickets. Record the minimum actionable constraint, not a diagnosis. Unknown critical constraints rule out that candidate until verified. |
| High value | Interests and usage | Activities, specific tastes, current projects and how they enjoy using things; specificity and recency matter more than generic hobbies. |
| High value | Existing possessions and duplication | What they already have, relevant compatibility, whether duplicates or upgrades are welcome. |
| High value | Gift preferences and practical fit | Material versus experience or flexible choice, surprise versus request, time, space, maintenance and travel tolerance. Avoid assuming a permanent preference from a single occasion. |
| Optional | Style and specificity | Colors, brands, materials, sizes and aesthetic preferences only where useful and willingly shared. |
| Optional | Meaning and social context | Recipient-approved shared memories, customs and relationship context; avoid demographic or cultural assumptions and invented sentimental narratives. |
| Optional | Learning from outcomes | Recipient-approved feedback, returns and actual use, with an opportunity to correct or remove. Polite thanks is weak evidence of enjoyment. |

For each fact retain a concise claim, source and permission scope, timestamp or freshness, whether it is direct, observed or inferred, and relevant context. Record conflicting claims rather than silently choosing a winner. Prefer a recent explicit recipient statement over a giver's inference; ask or qualify when a conflict affects a choice. Confidence is an indicator of evidence quality, not a fabricated probability. Allow review, correction, revocation and deletion; propagate revocation to future contributions and shared views according to the product's actual capabilities.

## Occasion-relative Minimum Useful Profile

The minimum is a decision state, not a fixed number of fields. Combine: (1) the giver's occasion, budget and deadline or a stated workable default; (2) at least one authorized, relevant positive signal, such as a recent wish or concrete interest, or an explicitly low-confidence broad starting point; (3) known exclusions and any candidate-specific essential constraints, with critical unknowns verified or those candidates withheld; and (4) enough availability and delivery context to check feasibility before purchase. The recipient's sharing scope controls what can be surfaced. If these conditions cannot be met, offer a clearly labeled exploratory direction or ask for the missing fact rather than claiming a personalized match.

Example: for a local pottery class, an interest in ceramics is insufficient without confirming location, schedule, accessibility and willingness to attend; for a requested book, those fields may be irrelevant, while format, ownership and availability matter. Budget is giver context, not an inferred recipient trait. A minimum useful profile is a testable product criterion, not an empirically validated threshold.

## Adaptive discovery

First retrieve only recipient-authorized, occasion-relevant contributions from existing assistants and profile records. Summarize the permitted facts and their freshness internally; do not repeat questions already answered adequately. Ask one question at a time, allow skip or “I don't know,” and rerank after every answer. Rank by expected effect on the current shortlist, severity of a wrong choice, uncertainty, and burden or sensitivity. Stop when viable candidates meet the minimum above or another question is unlikely to change the decision. The ordering below is a default, not a script; an urgent safety or delivery constraint moves to the top.

1. **Current request or desire:** “Is there something they've mentioned wanting lately, or a wish list they chose to share?” Strong evidence can immediately narrow the choice.
2. **Exclusions and critical fit:** “Anything we should avoid, or any must-have constraint for these ideas?” Ask a specific follow-up only if the proposed category needs it; offer unknown/skip.
3. **Use and direction:** “Would they be more likely to enjoy something for [specific activity], a flexible choice, or an experience?” Include “none/unsure”; ask about how it would fit their life rather than forcing technical specifications.
4. **Resolve a consequential ambiguity:** “Do they already have a suitable [item], or would an upgrade be welcome?” Alternatively ask the relevant schedule, format, location or size question.
5. **Meaning or surprise, if it can change the choice:** “Is there a shared memory or level of surprise they'd enjoy us considering?” Use only what the contributor is authorized to share.

These are a maximum of five discovery questions after authorized knowledge is considered, not five compulsory prompts. If a direct current wish and necessary constraints are already available, ask none. If critical information remains unknown after the cap, narrow to safer, flexible options, present uncertainty, or request recipient input rather than inventing facts. Transactional checkout details may still be required separately.

## Validation plan

Test the profile and stopping rule with consenting giver–recipient pairs across different relationships, occasions and budgets. Compare authorized-assistant seeding plus adaptive questions with a short static questionnaire and a no-profile baseline. Measure recipient-rated fit and actual use or enjoyment after the occasion, unsuitable or duplicate gifts, question burden and abandonment, correction rates, privacy comfort, and the fraction of suggestions with traceable evidence. Record sample composition, missing feedback and uncertainty; do not equate clicks or giver satisfaction with recipient benefit. Examine whether gains hold when recipients share little and whether infrequent or changing interests are misrepresented. These are proposed evaluations, not demonstrated results.
