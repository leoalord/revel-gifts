# Agent-first gift recommendations

Research brief • 31 October 2026

## Recommendation

Use an existing assistant as the conversational decision-maker, supported by a small, permission-aware gift context and verifiable candidate information. Begin with what is authorized and relevant, identify the decisions the missing information could change, generate a few distinct options, check hard constraints, and explain the evidence and remaining unknowns. The giver stays in control of the choice. This is a proposed product design, not a demonstrated improvement in gifting outcomes.

The MVP does not require a multi-agent system, learned ranking model, elaborate profile, or numerical prediction of recipient happiness. An assistant can reason over a structured handoff and current sources; Revel's distinctive responsibility is to preserve recipient consent, provenance, constraints, and occasion context, and to make recommendations inspectable and correctable.

## Reading the first two briefs critically

[Gift-giving psychology](01-gifting-psychology.md) provides a sound starting point: prioritize a recipient's known wishes and the reality of use over impressiveness, and consider practicality, sentiment and experience in context. Its experiments do not prove a universal hierarchy of gift types, nor that a system using these principles improves real gifts. A giver's account is useful but is not the same as a recipient's current wish.

[Recipient profiling](02-recipient-profiling.md) appropriately treats readiness as occasion-specific, asks only decision-relevant questions, and distinguishes assistant access from permission to share. Its zero-to-three typical questions and five-question initial cap are testable design choices, not research-derived thresholds. An important tension is that an initial cap cannot turn an unknown critical constraint into a safe recommendation: withhold that candidate or seek recipient input even if this prevents a shortlist. A low-confidence exploratory direction is useful only when clearly labeled and should not be portrayed as a personalized, purchase-ready match.

## What the research supports

- **Conversation can improve cold-start decisions.** Christakopoulou and colleagues' restaurant work combines recommendation with questions and reports improvements in offline simulations after two questions, drawing on preferences from 28 participants. This motivates decision-relevant elicitation, not a gift-specific question count or a guarantee of live success. [Christakopoulou et al., 2016](https://www.kdd.org/kdd2016/papers/files/rfp0063-christakopoulouA.pdf).
- **Language can convey preferences when item histories are scarce.** Sanner and colleagues collected users' natural-language and item preferences and ratings; LLM approaches were competitive in their near-cold-start evaluation. This was not a third-party gifting test and does not show that generated products, prices or rationales are factual. [Sanner et al., 2023](https://arxiv.org/abs/2307.14225).
- **Explanations affect choices.** In a study with 129 participants, positively or negatively biased recommendation explanations changed selections across two textual formats. Persuasive phrasing is therefore not proof of fit; disclose relevant drawbacks and ground reasons in shareable evidence. [Balog, Radlinski and Petrov, 2023](https://research.google/pubs/measuring-the-impact-of-explanation-bias-a-study-of-natural-language-justifications-for-recommender-systems/). A separate study proposes distinct explanation goals, including transparency and persuasion; success on one should not be assumed to establish another. [Balog and Radlinski, 2020](https://research.google/pubs/measuring-recommendation-explanation-quality-the-conflicting-goals-of-explanations/).
- **Offline scores are limited proxies.** In six simulated environments, offline metrics correlated with online performance but gains diminished and model rankings depended on initial data. This is simulation evidence, not a real-world gifting result. [Krauth et al., 2020](https://arxiv.org/abs/2011.07931). Evaluate recipient outcomes directly as well as selection and completion.

The gifting studies cited in the first brief further motivate current requests and feasibility, but come from particular populations, occasions and choices. None validates the proposed Revel workflow or the assumptions below.

## Lightweight end-to-end workflow

1. **Frame the occasion.** Capture the giver's occasion, total budget, date and delivery/location context, including any known flexibility. Keep these separate from persistent recipient preferences. If missing, use an explicitly stated provisional assumption only for exploration; obtain necessary facts before purchase.
2. **Load authorized context.** An existing assistant or Revel retrieves only relevant recipient-approved facts, wish-list items, exclusions and sharing scope, plus giver-provided observations. Retain source, date, context and whether each claim is direct, observed or inferred. Private assistant access alone does not authorize onward disclosure. Conflicts remain visible internally; prioritize recent recipient statements and resolve consequential conflicts.
3. **Assess readiness and ask selectively.** Identify plausible gift directions and what could eliminate or change them. Ask one question at a time, allow skip and unknown, update after each answer, and stop when further answers are unlikely to change the decision. Usually zero to three questions, at most five in initial discovery, are a product hypothesis. A critical unknown still blocks the affected candidate.
4. **Generate and retrieve.** Have the assistant propose a small set of distinct candidate directions grounded in authorized signals, including a current wish when relevant, a practical alternative or a flexible choice where appropriate. If product search or a catalog is available, retrieve actual listings and capture source and check time. If not, describe ideas as concepts rather than inventing a merchant, price, availability or delivery guarantee. Affiliates or commercial ranking must be disclosed and must not override a stated constraint.
5. **Gate, compare and explain.** Reject contradictions to explicit exclusions, budget or deadline; hold candidates whose essential safety, fit, access or logistics are unknown. Compare the remaining options using the rubric below. Present a concise reason, source the giver may see, tradeoff, and what still needs checking. Do not reveal a private fact in either the explanation or a revealing inference. The assistant's explanation is a grounded rationale, not a claim to expose its internal reasoning.
6. **Refine and hand off.** Let the giver correct assumptions, reject options or request a different direction; regenerate and recheck after changes. Before purchase, verify current price including relevant fees, availability, shipping or booking terms, and candidate-specific constraints from an appropriate current source. Give the user the source and check time. Never claim the item is available or purchased without evidence. The giver decides and performs or explicitly authorizes any transaction.
7. **Learn only with permission.** After the occasion, optionally invite recipient feedback on fit and use; allow correction or deletion and apply sharing permissions to future recommendations. Record a decline or no response as missing data, not dissatisfaction or success.

A minimal handoff can contain: occasion/budget/deadline; authorized facts with source, date and disclosure scope; exclusions and unresolved conflicts; candidate concepts or sourced listings with check timestamps; and outstanding checks. Store only what is needed and follow actual retention, revocation and deletion capabilities; do not promise propagation that has not been implemented.

## Occasion-relative readiness

Readiness is a decision state, not a completed profile. A **purchase-ready candidate** needs a supported positive reason, no known contradiction, resolved candidate-specific critical constraints, and verified budget, timing, availability and delivery/booking feasibility. A **promising candidate** has a plausible fit but an identified noncritical or verifiable unknown; label the missing check and do not present it as ready. An **exploratory direction** is a broad, low-confidence idea based on sparse information, with no claim of personal fit. If a critical safety or compatibility fact is unknown, withhold that specific candidate until verified; suggest a lower-risk direction or ask the recipient instead. A request can be stale or already fulfilled, so confirm when relevant.

For a physical item, verify relevant format, size, ownership, compatibility, total cost and delivery as applicable. For an experience, verify willingness, location, dates, accessibility, cancellation and other relevant logistics. A flexible contribution may avoid some category-specific risks but still requires the recipient's willingness and applicable terms. A meaningful note can be offered separately when supported by authorized context.

## Candidate rubric

First apply hard gates; no amount of charm compensates for a known exclusion, critical unknown or missed deadline. Then compare candidates qualitatively, without fabricated probabilities or arbitrary weighted scores:

| Dimension | Evidence or question | What to show |
|---|---|---|
| Recipient fit | Recent explicit wish, current use or concrete interest; source and freshness | Why it fits, distinguishing a direct wish from the giver's inference |
| Practical use | Ownership, format, compatibility, effort, space, access and schedule where relevant | The benefit and any burden or unresolved check |
| Occasion and feasibility | Relationship context, total cost, delivery/booking timing and terms | Verified details and timestamp, or an honest unknown |
| Meaning and surprise | Authorized shared context and stated preference for surprise | A specific supported connection, never an invented story |
| Choice quality | Whether candidates offer meaningfully different paths | Shortlist diversity without forcing a weaker option over a clear wish |
| Confidence and privacy | Recency, conflicts, source quality and disclosure permission | A plain-language uncertainty and only shareable reasons |

Tie-break toward the best-supported, usable option, not the most expensive, novel, or eloquently explained. An explanation should identify a concrete shareable reason, a material tradeoff, and the next check; invite correction. If the only reason is private, suppress it and seek permission or show a non-revealing explanation, provided even the suggestion itself is authorized.

## Choosing the next question

Use qualitative value of information: ask when plausible answers would change eligibility, order or confidence enough to justify the user's effort and sensitivity. Consider severity of being wrong, likelihood the giver knows, and whether another route can resolve it. This is a practical heuristic, not a calibrated expected-value model. Avoid questions whose answers would not change the shortlist.

| Situation | Highest-value question and consequence |
|---|---|
| A particular book appears on a recent shared wish list | “Do they already own it, and do you know their preferred format?” If unknown, verify before buying; broad hobby questions add little. |
| Giver knows only “likes cooking” | “Have they mentioned something they want, or a particular kind of cooking they enjoy?” A concrete answer changes directions; unknown yields exploratory options, not a claim of personalization. |
| Food candidate with unknown restriction | “Is there anything they cannot or prefer not to eat?” Unknown holds the food candidate; a nonfood alternative may proceed. |
| Proposed class requires travel and scheduling | “Would they enjoy a class, and can they get to it on the available dates?” If unknown, withhold a booking and consider a flexible alternative. |
| Conflicting recent wish and older avoidance | “Their shared preferences seem to have changed. Which is current?” If the conflict cannot be safely disclosed, obtain recipient clarification through an authorized route. |

## Worked examples

**Sparse, exploratory.** A giver has $40 for a birthday in two weeks and only knows the recipient enjoys cooking; no shared profile exists. The assistant asks whether there is a recent request or specific cooking activity. The giver does not know. It offers broad directions such as a flexible contribution toward a cooking purchase, or asking the recipient for a wish, while stating that neither is a known preference. It does not assert a specific food, gadget, dietary restriction or merchant. Before any purchase, it checks the recipient's willingness and the actual terms, total cost and delivery as applicable. If the giver declines further discovery, stop with labeled ideas rather than inventing a close match.

**Rich, near-ready.** A recipient recently authorized sharing a request for a particular novel in paperback and said they prefer physical books; the giver has $35 and a birthday next week. The assistant finds a real listing from a current source, checks total delivered price and estimated arrival, and asks only if ownership or fulfillment is genuinely uncertain. It presents the requested paperback as the leading option, citing the permitted request and verified listing; it flags the delivery estimate and confirms ownership if unresolved. If it cannot verify delivery or the book is already owned, it does not claim purchase readiness and asks about an alternative or offers a suitable flexible contribution. A private related conversation is neither disclosed nor used to infer a revealing explanation.

Both examples are illustrative design hypotheses, not observed results. No particular item, merchant, price or availability is asserted here.

## Pragmatic MVP evaluation

Start with a small, consented pilot of real giver–recipient pairs, diverse in relationship, occasion and information available. Specify outcomes and analysis before comparison. Randomly assign at the occasion level, where practical, to (A) assistant plus authorized context and adaptive questioning, (B) assistant plus a short static questionnaire, and (C) ordinary assistant or giver-only approach with the same product access. Where randomization or sample size is impractical, treat comparisons as exploratory and report uncertainty rather than declaring a winner.

Primary outcome: recipient-rated fit after the gift, using a consistent short scale and collecting responses privately and voluntarily. Secondary outcomes: recipient-reported use/enjoyment after a suitable interval; duplicates and unsuitable gifts; giver decision confidence; time, question count and abandonment; privacy comfort and corrections; verified constraint and listing accuracy; and unsupported or unauthorized disclosures. Log denominators, refusals, no gift, returns and missing recipient feedback; separate giver enthusiasm, clicks and purchase conversion from recipient benefit. Review harms and reasons for abstention, including whether restrictions or exclusion rules fail for people who share little. Disaggregate cautiously by information richness and relevant occasion/relationship groups; small samples cannot support strong subgroup claims.

Before a pilot, run a small adversarial case set for stale requests, conflicts, revoked access, unknown critical constraints, invented products, price changes, deadline failures and sensitive explanations. Check both whether the assistant withholds unsupported candidates and whether it remains useful with sparse data. Human review of groundedness and privacy complements automated checks. A live, source-verified recommendation is needed to test retrieval; simulated cases alone cannot establish gift quality.

## Method and limits

Reviewed both preceding briefs and primary research on conversational elicitation, near-cold-start language recommendation, explanation bias and offline evaluation. Sources were selected for relevance, not through a systematic review; domain transfer from restaurants and other recommendation datasets to third-party gifts is uncertain. The workflow, rubric, readiness states and evaluation plan are product hypotheses, not results of a Revel experiment. Research on privacy controls and the operational feasibility of integrating existing assistants was not independently established by these recommendation studies. Current product data and delivery conditions must be checked at decision time.
