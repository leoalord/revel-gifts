"""F5's central list of profile fact categories and their validation (pure; no database).

Contract: ``docs/contracts/f5-profiles.md`` ("Categories"). Product policy (what may be
collected, default audiences, Public eligibility) lives in Notion ("Profile fact data model" on
the main page; Privacy "Contribution policy"); this module implements it.

Always rejected: unknown categories, home/street addresses and birth years. Sensitive
information the owner volunteers (health, religion, politics, orientation, finances) is not
rejected: it goes in the ``sensitive`` category, which is Only Me, explicit evidence only and
always held for the owner's review. ``details`` shapes are closed:
unknown keys are rejected.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from typing import Any

from revel.db.enums import Audience

DETAIL_TEXT_MAX_LENGTH = 100


class CategoryError(ValueError):
    """Base class for category and details validation errors. ``reason_code`` is stable."""

    reason_code = "invalid_details"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownCategory(CategoryError):
    reason_code = "unknown_category"


class ForbiddenCategory(CategoryError):
    reason_code = "forbidden_category"


class InvalidDetails(CategoryError):
    reason_code = "invalid_details"


class InvalidField(CategoryError):
    """A field other than ``details`` violates a collection rule (e.g. a street address)."""

    reason_code = "invalid_field"


@dataclass(frozen=True, slots=True)
class DetailField:
    name: str
    kind: str  # "month" | "day" | "text"
    required: bool


@dataclass(frozen=True, slots=True)
class CategorySpec:
    id: str
    label: str
    default_audience: Audience
    public_eligible: bool
    detail_fields: tuple[DetailField, ...] = ()
    # Shape rule beyond per-field checks: "date" (valid month/day) or "any_of" (>= 1 field).
    detail_rule: str | None = None
    # ``sensitive``: explicit evidence only, always held for the owner's review, and its facts
    # can never be given an audience other than Only Me.
    sensitive: bool = False

    @property
    def details_schema(self) -> dict[str, Any] | None:
        """JSON description of ``details`` for clients (``null`` when the category has none)."""
        if not self.detail_fields:
            return None
        properties: dict[str, Any] = {}
        for f in self.detail_fields:
            if f.kind == "month":
                properties[f.name] = {"type": "integer", "minimum": 1, "maximum": 12}
            elif f.kind == "day":
                properties[f.name] = {"type": "integer", "minimum": 1, "maximum": 31}
            else:
                properties[f.name] = {"type": "string", "maxLength": DETAIL_TEXT_MAX_LENGTH}
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": [f.name for f in self.detail_fields if f.required],
            "additionalProperties": False,
        }
        if self.detail_rule == "any_of":
            schema["minProperties"] = 1
        return schema

    def allowed_audiences(self, *, public_opt_in: bool) -> tuple[Audience, ...]:
        """Audiences a policy may give this category, narrowest first: Only Me up to the
        default, plus Public for public-eligible categories while opted in."""
        if self.sensitive:
            return (Audience.ONLY_ME,)
        allowed = [a for a in reversed(Audience) if a.rank >= self.default_audience.rank]
        if self.public_eligible and public_opt_in:
            allowed.append(Audience.PUBLIC)
        return tuple(allowed)


_DATE_FIELDS = (DetailField("month", "month", True), DetailField("day", "day", True))

CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec("interest", "Interests and hobbies", Audience.CONNECTIONS, True),
    CategorySpec("style", "Style and taste", Audience.CONNECTIONS, True),
    CategorySpec("favorite_brand", "Favorite brands", Audience.CONNECTIONS, True),
    CategorySpec("wish", "Wishes and wants", Audience.CONNECTIONS, True),
    CategorySpec("dislike", "Dislikes", Audience.CONNECTIONS, False),
    CategorySpec("owned_item", "Things already owned", Audience.CONNECTIONS, False),
    CategorySpec(
        "birthday", "Birthday (month and day)", Audience.CONNECTIONS, False, _DATE_FIELDS, "date"
    ),
    CategorySpec(
        "location",
        "General location",
        Audience.CONNECTIONS,
        False,
        (
            DetailField("city", "text", False),
            DetailField("region", "text", False),
            DetailField("country", "text", False),
        ),
        "any_of",
    ),
    CategorySpec(
        "size",
        "Sizes",
        Audience.TRUSTED_CIRCLE,
        False,
        (DetailField("item", "text", True), DetailField("size", "text", True)),
    ),
    CategorySpec("life_event", "Life events", Audience.TRUSTED_CIRCLE, False),
    CategorySpec(
        "anniversary", "Anniversaries", Audience.TRUSTED_CIRCLE, False, _DATE_FIELDS, "date"
    ),
    CategorySpec("sentimental", "Sentimental items and memories", Audience.TRUSTED_CIRCLE, False),
    CategorySpec(
        "diet_exclusion",
        "Foods to avoid",
        Audience.TRUSTED_CIRCLE,
        False,
        (DetailField("exclude", "text", True),),
    ),
    CategorySpec("sensitive", "Sensitive (volunteered)", Audience.ONLY_ME, False, sensitive=True),
)

CATEGORY_BY_ID: dict[str, CategorySpec] = {c.id: c for c in CATEGORIES}

# Topics that belong in the ``sensitive`` category (volunteered, Only Me, always reviewed).
SENSITIVE_TOPICS: frozenset[str] = frozenset(
    {
        "health",
        "medical",
        "medication",
        "disability",
        "religion",
        "faith",
        "politics",
        "political",
        "orientation",
        "sexual_orientation",
        "gender_identity",
        "finances",
        "financial",
        "income",
        "salary",
    }
)

# Never collected (Privacy: home address, birth year).
FORBIDDEN_CATEGORIES: frozenset[str] = frozenset(
    {
        "address",
        "home_address",
        "street_address",
        "birth_year",
        "birthdate",
        "date_of_birth",
        "age",
    }
)

_ADDRESS_KEYS = frozenset(
    {
        "address",
        "street",
        "street_address",
        "line1",
        "line2",
        "address_line",
        "house_number",
        "apartment",
        "unit",
        "postal_code",
        "postcode",
        "zip",
        "zip_code",
    }
)
_YEAR_KEYS = frozenset({"year", "birth_year", "yob", "date_of_birth", "dob", "age"})

# A street address in free text: a house number followed by words and a street type.
_STREET_ADDRESS = re.compile(
    r"\b\d{1,6}[a-z]?\s+(?:[a-z0-9.'-]+\s+){0,4}"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|way|"
    r"place|pl|terrace|crescent|close|highway|hwy)\b\.?",
    re.IGNORECASE,
)
# A four-digit year (1900-2099) in a birthday statement is a birth year.
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def validate_category(category: str) -> CategorySpec:
    """The spec for ``category``; raises ``ForbiddenCategory`` or ``UnknownCategory``."""
    key = category.strip().lower() if isinstance(category, str) else ""
    if key in FORBIDDEN_CATEGORIES:
        raise ForbiddenCategory("This kind of information is never collected")
    if key in SENSITIVE_TOPICS:
        raise UnknownCategory(
            "Unknown category; sensitive information the user volunteers goes in 'sensitive'"
        )
    spec = CATEGORY_BY_ID.get(category) if isinstance(category, str) else None
    if spec is None:
        raise UnknownCategory("Unknown category")
    return spec


def validate_statement(spec: CategorySpec, statement: str) -> str:
    """Trimmed statement; rejects street addresses anywhere and birth years in birthdays."""
    if _STREET_ADDRESS.search(statement):
        raise InvalidField("Street addresses are never collected")
    if spec.id == "birthday" and _YEAR.search(statement):
        raise InvalidField("Birth years are never collected; give the month and day only")
    return statement


def validate_details(spec: CategorySpec, details: Any) -> dict[str, Any] | None:
    """Normalized ``details`` for the category (``None`` when absent or empty)."""
    if details is None or details == {}:
        return None
    if not isinstance(details, dict):
        raise InvalidDetails("details must be an object")
    keys = {str(k).lower() for k in details}
    if keys & _ADDRESS_KEYS:
        raise InvalidDetails("Street addresses are never collected")
    if keys & _YEAR_KEYS:
        raise InvalidDetails("Years and birth dates are never collected; month and day only")
    if not spec.detail_fields:
        raise InvalidDetails("This category takes no details")
    fields = {f.name: f for f in spec.detail_fields}
    unknown = set(details) - set(fields)
    if unknown:
        raise InvalidDetails(f"Unknown details: {', '.join(sorted(map(str, unknown)))}")
    out: dict[str, Any] = {}
    for name, field in fields.items():
        if name not in details or details[name] is None:
            if field.required:
                raise InvalidDetails(f"details.{name} is required")
            continue
        value = details[name]
        if field.kind in ("month", "day"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidDetails(f"details.{name} must be an integer")
            high = 12 if field.kind == "month" else 31
            if not 1 <= value <= high:
                raise InvalidDetails(f"details.{name} must be between 1 and {high}")
        else:
            if not isinstance(value, str) or not value.strip():
                raise InvalidDetails(f"details.{name} must be non-empty text")
            value = value.strip()
            if len(value) > DETAIL_TEXT_MAX_LENGTH:
                raise InvalidDetails(
                    f"details.{name} must be at most {DETAIL_TEXT_MAX_LENGTH} characters"
                )
            if _STREET_ADDRESS.search(value):
                raise InvalidDetails("Street addresses are never collected")
            if _YEAR.search(value) and spec.detail_rule == "date":
                raise InvalidDetails("Years are never collected; month and day only")
        out[name] = value
    # Any year (leap years included): Feb 29 is a valid birthday.
    if spec.detail_rule == "date" and out["day"] > calendar.monthrange(2024, out["month"])[1]:
        raise InvalidDetails("details.day is not a valid day of that month")
    if spec.detail_rule == "any_of" and not out:
        return None
    return out
