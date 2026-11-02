"""Question routing: purpose minimization, respondent selection and the spoiler guard. Owned by F7.

Revel (not the requesting assistant) prepares the purpose-minimized question a respondent sees
(Notion: Ongoing Enrichment & Agent Questions, "Surprise-safe questions"). Muse Spark proposes
``{minimized_question, respondent_ids}``; Revel validates the proposal before anything is
published and otherwise uses a deterministic fallback:

- respondent ids must be a non-empty subset of the candidate set Revel supplied (candidates are
  already constrained by OpenFGA and owner policy; the model cannot add anyone);
- the minimized question must be non-empty, within ``MINIMIZED_QUESTION_MAX_LENGTH`` and pass
  ``publishable_question_problems`` (``find_spoilers`` documents the rules): no occasion, gift,
  money or date wording; no occasion, date, budget or notes values; no candidate-idea term the
  requester did not write themselves in ``requested_information``; no identity claim, requester
  name/handle or assistant name. Requester and acting-assistant identity is always composed
  server-side from the authenticated principal, never from question text.

Fallback (timeout, unavailable, HTTP error, malformed or unsafe output): every eligible
candidate, and the normalized ``requested_information``. That text is checked up front
(``validate_requested_information``: spoilers and identity claims, before any model call) and
the service runs the same publish gate on whatever text is about to be published, so neither
path can publish unchecked text.

Nothing here logs or returns question text, purpose values or model output; reasons are short
category codes (``SpoilerCategory``) safe to store in ``question_events.details``.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from revel.services.muse_spark import MuseSparkClient, MuseSparkError

logger = logging.getLogger(__name__)

# Respondent-facing text limit (the column allows 1000; keep minimized questions short).
REQUESTED_INFORMATION_MAX_LENGTH = 500
MINIMIZED_QUESTION_MAX_LENGTH = 500
# Tokens shorter than this (that are not numbers) are too common to treat as purpose leaks.
MIN_SIGNIFICANT_TOKEN_LENGTH = 4
# Phrases shorter than this are ignored for phrase matching (numbers always count).
MIN_PHRASE_LENGTH = 3
MUSE_MAX_TOKENS = 1000  # reasoning model: needs >= 800 to leave room for the JSON answer

RoutingPath = Literal["model", "fallback"]


class SpoilerCategory(StrEnum):
    """Why text was judged unsafe. Codes only: never the matched text."""

    OCCASION = "occasion"
    GIFT = "gift"
    MONEY = "money"
    DATE = "date"
    PRIVATE_PURPOSE = "private_purpose"
    IDENTITY = "identity"


class FallbackReason(StrEnum):
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    MALFORMED = "malformed"
    INVALID_RESPONDENTS = "invalid_respondents"
    INVALID_QUESTION = "invalid_question"
    UNSAFE_QUESTION = "unsafe_question"
    NO_CANDIDATES = "no_candidates"
    ERROR = "error"


# --- Structured private purpose ----------------------------------------------------------------


class PrivatePurpose(BaseModel):
    """The requester's restricted context. Stored in ``questions.private_purpose`` (deferred,
    raiseload) and given only to Revel's router; never to respondents, events or logs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    occasion: str | None = Field(default=None, max_length=100)
    occasion_date: str | None = Field(default=None, max_length=50)
    budget: str | float | None = Field(default=None)
    candidate_ideas: list[str] = Field(default_factory=list, max_length=10)
    notes: str | None = Field(default=None, max_length=1000)

    def model_post_init(self, _context: Any) -> None:
        if isinstance(self.budget, str) and len(self.budget) > 50:
            raise ValueError("budget is too long")
        if any(len(idea) > 200 for idea in self.candidate_ideas):
            raise ValueError("candidate idea is too long")

    def string_values(self) -> list[str]:
        """Every value as text (budget numbers rendered both as written and as an integer)."""
        values: list[str] = []
        for value in (self.occasion, self.occasion_date, self.notes):
            if value:
                values.append(value)
        if isinstance(self.budget, str) and self.budget:
            values.append(self.budget)
        elif isinstance(self.budget, int | float) and not isinstance(self.budget, bool):
            values.append(str(self.budget))
            if float(self.budget).is_integer():
                values.append(str(int(self.budget)))
        values.extend(idea for idea in self.candidate_ideas if idea)
        return values

    def fixed_values(self) -> list[str]:
        """Occasion, occasion date and budget: never allowed in respondent-facing text."""
        values = [v for v in (self.occasion, self.occasion_date) if v]
        if isinstance(self.budget, str) and self.budget:
            values.append(self.budget)
        elif isinstance(self.budget, int | float) and not isinstance(self.budget, bool):
            values.append(str(self.budget))
            if float(self.budget).is_integer():
                values.append(str(int(self.budget)))
        return values


# --- Text normalization and matching -----------------------------------------------------------

_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NON_WORD = re.compile(r"[^a-z0-9$€£¥]+")

STOPWORDS = frozenset(
    {
        "about",
        "above",
        "after",
        "again",
        "against",
        "also",
        "among",
        "another",
        "anyone",
        "anything",
        "around",
        "because",
        "been",
        "before",
        "being",
        "below",
        "best",
        "between",
        "both",
        "could",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "either",
        "else",
        "enjoy",
        "enjoys",
        "even",
        "ever",
        "every",
        "everyone",
        "favorite",
        "favorites",
        "favourite",
        "from",
        "further",
        "good",
        "great",
        "have",
        "having",
        "here",
        "hers",
        "herself",
        "himself",
        "into",
        "itself",
        "just",
        "kind",
        "kinds",
        "know",
        "like",
        "liked",
        "likes",
        "love",
        "loved",
        "loves",
        "made",
        "make",
        "many",
        "maybe",
        "more",
        "most",
        "much",
        "must",
        "myself",
        "near",
        "need",
        "never",
        "nice",
        "only",
        "other",
        "ours",
        "over",
        "own",
        "please",
        "prefer",
        "preferred",
        "prefers",
        "really",
        "same",
        "should",
        "since",
        "some",
        "someone",
        "something",
        "sort",
        "style",
        "styles",
        "such",
        "than",
        "that",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "things",
        "think",
        "thinks",
        "this",
        "those",
        "though",
        "through",
        "type",
        "types",
        "under",
        "until",
        "upon",
        "very",
        "want",
        "wants",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "whom",
        "whose",
        "will",
        "with",
        "within",
        "without",
        "would",
        "your",
        "yours",
        "yourself",
    }
)

_OCCASION = re.compile(
    r"\b(?:birthdays?|bdays?|b-days?|anniversar(?:y|ies)|holidays?|christmas|xmas|x-mas|"
    r"hanukk?ah|chanukk?ah|diwali|eid|ramadan|kwanzaa|thanksgiving|easter|valentines?|"
    r"valentine's|mother'?s day|father'?s day|mothers day|fathers day|new year'?s?|"
    r"graduations?|weddings?|engagements?|retirements?|housewarmings?|showers?|"
    r"celebrat\w*|part(?:y|ies)|occasions?|special day|big day|milestones?|"
    r"secret santa|stocking)\b"
)
_GIFT = re.compile(
    r"\b(?:gift\w*|presents?|surpris\w*|secrets?|secretly|wish ?lists?|buy|buying|bought|"
    r"purchas\w*|shopping|shop for|treat (?:her|him|them)|spoil\w*)\b"
)
_MONEY = re.compile(
    r"(?:[$€£¥]|\b(?:budgets?|pric\w*|costs?|costing|spend\w*|spent|afford\w*|cheap\w*|"
    r"expensive|money|dollars?|bucks|usd|eur|euros?|gbp|pounds?|quid)\b|\b\d+\s*k\b)"
)
_DATE = re.compile(
    r"\b(?:jan|january|feb|february|march|april|june|july|aug|august|sept?|september|oct|"
    r"october|nov|november|dec|december|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|"
    r"saturdays?|sundays?|weekend|tomorrow|tonight|next (?:week|month|year)|this (?:week|month)|"
    r"\d{1,2}(?:st|nd|rd|th)|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?)\b"
    r"|\bmay\s+\d|\b\d{1,2}\s+may\b"
)
# Wording that claims or describes who is asking. Identity is composed server-side from the
# authenticated principal, so neither the requester nor the model may write it. A bare
# "assistant" is not a claim ("Does Alex use a voice assistant?").
_IDENTITY_CLAIM = re.compile(
    r"\b(?:asks|asking|on behalf of|requester|requesting|this is|i am|i'm|my name|anonymous|"
    r"wants to know|would like to know|wondering|wonders|"
    r"(?:through|via|from|by) (?:his|her|their|my|our) (?:assistant|agent|ai))\b"
    r"|'s (?:assistant|agent|ai)\b"
)
# In model output only: the model must not mention assistants unless the requester did.
_ASSISTANT_WORDS = re.compile(r"\b(?:assistants?|agents?)\b")

_LEXICON: tuple[tuple[re.Pattern[str], SpoilerCategory], ...] = (
    (_OCCASION, SpoilerCategory.OCCASION),
    (_GIFT, SpoilerCategory.GIFT),
    (_MONEY, SpoilerCategory.MONEY),
    (_DATE, SpoilerCategory.DATE),
)


def normalize_question_text(text: str) -> str:
    """NFKC, control characters removed, whitespace collapsed, trimmed."""
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _fold(text: str) -> str:
    """Lowercase, curly quotes straightened, for matching only."""
    text = unicodedata.normalize("NFKC", text).lower()
    return text.replace("\u2019", "'").replace("\u2018", "'")


def _stem(token: str) -> str:
    token = token.removesuffix("'s")
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokens(text: str) -> set[str]:
    """Stemmed lowercase word tokens."""
    return {_stem(t) for t in _TOKEN.findall(_fold(text))}


def significant_tokens(text: str) -> set[str]:
    """Tokens distinctive enough to count as a purpose leak (numbers, or long non-stopwords)."""
    out: set[str] = set()
    for raw in _TOKEN.findall(_fold(text)):
        token = _stem(raw)
        if token.isdigit() or (
            len(token) >= MIN_SIGNIFICANT_TOKEN_LENGTH
            and raw not in STOPWORDS
            and token not in STOPWORDS
        ):
            out.add(token)
    return out


def _phrase_key(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", _fold(text)).split())


def _contains_phrase(haystack_key: str, phrase: str) -> bool:
    key = _phrase_key(phrase)
    if not key or (len(key) < MIN_PHRASE_LENGTH and not key.isdigit()):
        return False
    return f" {key} " in f" {haystack_key} "


def _word_pairs(value: str) -> list[str]:
    """Consecutive word pairs of a purpose value with at least one significant word, so a
    fragment of a longer value ("the lake house") also counts as a phrase match."""
    words = _phrase_key(value).split()
    return [
        f"{a} {b}"
        for a, b in itertools.pairwise(words)
        if significant_tokens(a) or significant_tokens(b)
    ]


def find_spoilers(
    text: str,
    purpose: PrivatePurpose,
    *,
    requested_information: str,
    model_output: bool,
    identity_terms: Iterable[str] = (),
) -> set[SpoilerCategory]:
    """Spoiler and identity categories found in ``text`` (empty when safe). Never returns text.

    ``requested_information`` is the requester's own (normalized) question; for the up-front
    check of that question pass it as both ``text`` and ``requested_information`` with
    ``model_output=False``. Rules:

    - Always forbidden: the occasion/gift/money/date lexicon; the occasion, occasion date and
      budget values (as phrases, word pairs and significant tokens); notes phrases and word
      pairs; identity claims (``_IDENTITY_CLAIM``) and ``identity_terms`` (the requester's
      name and handle, the acting assistant's name).
    - Candidate ideas: a phrase (the idea or a word pair of it) or significant token is
      allowed only when the requester wrote it: the phrase occurs in ``requested_information``,
      or every significant token of it does. Other parts of the same idea stay forbidden
      ("binoculars" asked about: "compact" and "field guide" are not). So the requester may ask
      whether the owner has something, and the model can never add another idea term.
    - ``model_output`` additionally forbids significant notes tokens the requester did not use
      and a bare "assistant"/"agent" the requester did not use.
    """
    folded = _fold(text)
    key = _phrase_key(text)
    requested_key = _phrase_key(requested_information)
    allowed = tokens(requested_information)
    text_tokens = tokens(text)
    found: set[SpoilerCategory] = set()

    for pattern, category in _LEXICON:
        if pattern.search(folded):
            found.add(category)

    # Occasion, date, budget and notes: never allowed, as phrases or word pairs.
    fixed = purpose.fixed_values()
    notes = [purpose.notes] if purpose.notes else []
    for value in [*fixed, *notes]:
        if any(_contains_phrase(key, p) for p in [value, *_word_pairs(value)]):
            found.add(SpoilerCategory.PRIVATE_PURPOSE)
    fixed_tokens: set[str] = set()
    for value in fixed:
        fixed_tokens |= significant_tokens(value)
    if text_tokens & fixed_tokens:
        found.add(SpoilerCategory.PRIVATE_PURPOSE)

    # Candidate ideas: only the parts the requester explicitly asked about.
    def requester_wrote(phrase: str) -> bool:
        significant = significant_tokens(phrase)
        return _contains_phrase(requested_key, phrase) or bool(
            significant and significant <= allowed
        )

    for idea in purpose.candidate_ideas:
        for phrase in [idea, *_word_pairs(idea)]:
            if _contains_phrase(key, phrase) and not requester_wrote(phrase):
                found.add(SpoilerCategory.PRIVATE_PURPOSE)
        if text_tokens & (significant_tokens(idea) - allowed):
            found.add(SpoilerCategory.PRIVATE_PURPOSE)

    if model_output and notes and text_tokens & (significant_tokens(notes[0]) - allowed):
        found.add(SpoilerCategory.PRIVATE_PURPOSE)

    if _IDENTITY_CLAIM.search(folded):
        found.add(SpoilerCategory.IDENTITY)
    if model_output and _ASSISTANT_WORDS.search(folded) and not ({"assistant", "agent"} & allowed):
        found.add(SpoilerCategory.IDENTITY)
    if any(_contains_phrase(key, term) for term in identity_terms if term):
        found.add(SpoilerCategory.IDENTITY)
    return found


_PURPOSE_MESSAGE = (
    "requested_information looks like it reveals private purpose details (such as an "
    "occasion, gift or surprise, budget, date or other planning context). Ask only for the "
    "information you need and keep purpose details in private_purpose."
)
_IDENTITY_MESSAGE = (
    "requested_information must not say who is asking or on whose behalf (no names, handles "
    "or 'asks' wording). Revel shows the person's assistants your verified identity and your "
    "assistant itself."
)


class UnsafeRequestedInformation(ValueError):
    """``requested_information`` carries purpose details or an identity claim. The message
    never echoes them."""

    def __init__(self, categories: set[SpoilerCategory]) -> None:
        self.categories = frozenset(categories)
        identity_only = self.categories == {SpoilerCategory.IDENTITY}
        super().__init__(_IDENTITY_MESSAGE if identity_only else _PURPOSE_MESSAGE)


def validate_requested_information(
    text: str, purpose: PrivatePurpose, identity_terms: Iterable[str] = ()
) -> str:
    """Normalize and check the requester's question; returns the normalized text.

    Raises ``ValueError`` for empty/too-long text and ``UnsafeRequestedInformation`` when it
    carries an occasion, gift, money or date term, an occasion/date/budget value, a notes
    phrase, an identity claim or the requester's/assistant's name (``find_spoilers``).
    Candidate-idea terms the requester writes here are allowed (and become the only idea terms
    a minimized question may contain).
    """
    normalized = normalize_question_text(text)
    if not normalized:
        raise ValueError("requested_information must not be empty")
    if len(normalized) > REQUESTED_INFORMATION_MAX_LENGTH:
        raise ValueError(
            f"requested_information must be at most {REQUESTED_INFORMATION_MAX_LENGTH} characters"
        )
    categories = find_spoilers(
        normalized,
        purpose,
        requested_information=normalized,
        model_output=False,
        identity_terms=identity_terms,
    )
    if categories:
        raise UnsafeRequestedInformation(categories)
    return normalized


def publishable_question_problems(
    question: str,
    *,
    requested_information: str,
    purpose: PrivatePurpose,
    identity_terms: Iterable[str],
) -> set[SpoilerCategory]:
    """The final gate for any text a respondent will see (model output OR fallback)."""
    return find_spoilers(
        question,
        purpose,
        requested_information=requested_information,
        model_output=True,
        identity_terms=identity_terms,
    )


# --- Routing -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """An eligible respondent: an opaque id for the model, the delegation and a safe label."""

    id: str
    delegation_id: UUID
    agent_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    minimized_question: str
    respondents: tuple[Candidate, ...]
    path: RoutingPath
    fallback_reason: FallbackReason | None = None

    def event_details(self) -> dict[str, Any]:
        """Structured metadata for ``question_events.details`` (no text)."""
        details: dict[str, Any] = {"routing_path": self.path}
        if self.fallback_reason is not None:
            details["fallback_reason"] = self.fallback_reason.value
        return details


SYSTEM_PROMPT = (
    "You are the routing step of Revel, a private gifting-profile service. A person's "
    "assistant wants to learn something about the profile owner. Rewrite the requested "
    "information as ONE short, neutral question about the owner for the owner's own "
    "assistants, and choose which candidate assistants should receive it.\n"
    "Rules for minimized_question:\n"
    "- Ask only for the requested information; keep it short and natural.\n"
    "- NEVER mention or hint at any occasion, event, gift, present, surprise, shopping, budget, "
    "price, money, date or time, or any candidate idea, or anything else from private_purpose.\n"
    "- NEVER say who is asking or mention any assistant; Revel adds verified identity itself.\n"
    "Rules for respondent_ids: choose one or more ids, only from the candidates list.\n"
    'Reply with JSON only, no prose: {"minimized_question": "...", "respondent_ids": ["..."]}'
)


def _build_user_prompt(
    requested_information: str, purpose: PrivatePurpose, candidates: Sequence[Candidate]
) -> str:
    return json.dumps(
        {
            "requested_information": requested_information,
            "private_purpose": purpose.model_dump(exclude_none=True),
            "candidates": [{"id": c.id, "label": c.label} for c in candidates],
        },
        ensure_ascii=False,
    )


def _fallback(
    requested_information: str, candidates: Sequence[Candidate], reason: FallbackReason
) -> RoutingDecision:
    return RoutingDecision(
        minimized_question=requested_information,
        respondents=tuple(candidates),
        path="fallback",
        fallback_reason=reason,
    )


def validate_proposal(
    proposal: dict[str, Any],
    *,
    requested_information: str,
    purpose: PrivatePurpose,
    candidates: Sequence[Candidate],
    identity_terms: Iterable[str],
) -> tuple[str, tuple[Candidate, ...]] | FallbackReason:
    """Check a model proposal. Returns ``(question, respondents)`` or why it was rejected."""
    by_id = {c.id: c for c in candidates}
    ids = proposal.get("respondent_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or not all(isinstance(i, str) for i in ids)
        or not set(ids) <= by_id.keys()
    ):
        return FallbackReason.INVALID_RESPONDENTS
    question = proposal.get("minimized_question")
    if not isinstance(question, str):
        return FallbackReason.INVALID_QUESTION
    question = normalize_question_text(question)
    if not question or len(question) > MINIMIZED_QUESTION_MAX_LENGTH:
        return FallbackReason.INVALID_QUESTION
    if publishable_question_problems(
        question,
        requested_information=requested_information,
        purpose=purpose,
        identity_terms=identity_terms,
    ):
        return FallbackReason.UNSAFE_QUESTION
    chosen = tuple(by_id[i] for i in dict.fromkeys(ids))
    return question, chosen


async def route_question(
    *,
    requested_information: str,
    purpose: PrivatePurpose,
    candidates: Sequence[Candidate],
    client: MuseSparkClient | None,
    identity_terms: Iterable[str] = (),
) -> RoutingDecision:
    """Minimize the question and pick respondents; never raises for model problems.

    ``requested_information`` must already have passed ``validate_requested_information``.
    """
    identity_terms = tuple(identity_terms)
    if not candidates:
        return _fallback(requested_information, candidates, FallbackReason.NO_CANDIDATES)
    if client is None or not client.available:
        return _fallback(requested_information, candidates, FallbackReason.UNAVAILABLE)
    try:
        proposal = await client.complete_json(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(requested_information, purpose, candidates),
            max_tokens=MUSE_MAX_TOKENS,
        )
    except MuseSparkError as exc:
        return _fallback(requested_information, candidates, FallbackReason(exc.reason))
    except Exception as exc:  # never let the model path fail a submission
        logger.warning("question routing: model call failed (%s)", type(exc).__name__)
        return _fallback(requested_information, candidates, FallbackReason.ERROR)

    result = validate_proposal(
        proposal,
        requested_information=requested_information,
        purpose=purpose,
        candidates=candidates,
        identity_terms=identity_terms,
    )
    if isinstance(result, FallbackReason):
        logger.info("question routing: model proposal rejected (%s)", result.value)
        return _fallback(requested_information, candidates, result)
    question, chosen = result
    return RoutingDecision(minimized_question=question, respondents=chosen, path="model")
