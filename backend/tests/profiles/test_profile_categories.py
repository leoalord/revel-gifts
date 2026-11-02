"""F5 category list and validation (pure; no database)."""

import pytest

from revel.db.enums import Audience
from revel.services.categories import (
    CATEGORIES,
    CATEGORY_BY_ID,
    ForbiddenCategory,
    InvalidDetails,
    InvalidField,
    UnknownCategory,
    validate_category,
    validate_details,
    validate_statement,
)

CONTRACT_DEFAULTS = {
    "interest": ("connections", True),
    "style": ("connections", True),
    "favorite_brand": ("connections", True),
    "wish": ("connections", True),
    "dislike": ("connections", False),
    "owned_item": ("connections", False),
    "birthday": ("connections", False),
    "location": ("connections", False),
    "size": ("trusted_circle", False),
    "life_event": ("trusted_circle", False),
    "anniversary": ("trusted_circle", False),
    "sentimental": ("trusted_circle", False),
    "diet_exclusion": ("trusted_circle", False),
    "sensitive": ("only_me", False),
}


def test_category_table_matches_contract() -> None:
    assert {c.id: (c.default_audience.value, c.public_eligible) for c in CATEGORIES} == (
        CONTRACT_DEFAULTS
    )
    for spec in CATEGORIES:
        assert spec.label
    assert CATEGORY_BY_ID["interest"].details_schema is None
    assert CATEGORY_BY_ID["birthday"].details_schema["required"] == ["month", "day"]
    assert CATEGORY_BY_ID["birthday"].details_schema["additionalProperties"] is False


def test_allowed_audiences() -> None:
    interest, size = CATEGORY_BY_ID["interest"], CATEGORY_BY_ID["size"]
    assert interest.allowed_audiences(public_opt_in=False) == (
        Audience.ONLY_ME,
        Audience.TRUSTED_CIRCLE,
        Audience.CONNECTIONS,
    )
    assert interest.allowed_audiences(public_opt_in=True)[-1] is Audience.PUBLIC
    assert size.allowed_audiences(public_opt_in=True) == (Audience.ONLY_ME, Audience.TRUSTED_CIRCLE)
    sensitive = CATEGORY_BY_ID["sensitive"]
    assert sensitive.allowed_audiences(public_opt_in=True) == (Audience.ONLY_ME,)
    assert sensitive.details_schema is None and not sensitive.public_eligible


@pytest.mark.parametrize("category", ["address", "home_address", "birth_year", "date_of_birth"])
def test_address_and_birth_year_categories_forbidden(category: str) -> None:
    with pytest.raises(ForbiddenCategory) as info:
        validate_category(category)
    assert info.value.reason_code == "forbidden_category"


@pytest.mark.parametrize("category", ["health", "religion", "politics", "finances", "Orientation"])
def test_sensitive_topics_point_to_sensitive_category(category: str) -> None:
    with pytest.raises(UnknownCategory) as info:
        validate_category(category)
    assert "sensitive" in info.value.message


def test_unknown_category() -> None:
    with pytest.raises(UnknownCategory):
        validate_category("favourite_colour")


@pytest.mark.parametrize(
    "details",
    [
        {"month": 3, "day": 14, "year": 1990},
        {"month": 3, "day": 14, "birth_year": 1990},
        {"month": 13, "day": 1},
        {"month": 2, "day": 30},
        {"month": "3", "day": 14},
        {"month": True, "day": 14},
        {"day": 14},
    ],
)
def test_birthday_details_rejected(details: dict) -> None:
    with pytest.raises(InvalidDetails):
        validate_details(CATEGORY_BY_ID["birthday"], details)


def test_birthday_details_month_day_only() -> None:
    spec = CATEGORY_BY_ID["birthday"]
    assert validate_details(spec, {"month": 2, "day": 29}) == {"month": 2, "day": 29}
    assert validate_details(spec, None) is None


@pytest.mark.parametrize(
    "details",
    [
        {"street": "1 Main St"},
        {"city": "Lisbon", "postal_code": "1000"},
        {"city": "12 Baker Street"},
        {"address": "x"},
        {"neighbourhood": "Alfama"},
    ],
)
def test_location_rejects_street_address_and_unknown_keys(details: dict) -> None:
    with pytest.raises(InvalidDetails):
        validate_details(CATEGORY_BY_ID["location"], details)


def test_location_city_region_country() -> None:
    spec = CATEGORY_BY_ID["location"]
    assert validate_details(spec, {"city": " Lisbon ", "country": "PT"}) == {
        "city": "Lisbon",
        "country": "PT",
    }


def test_diet_exclusion_only_exclude() -> None:
    spec = CATEGORY_BY_ID["diet_exclusion"]
    assert validate_details(spec, {"exclude": "peanuts"}) == {"exclude": "peanuts"}
    with pytest.raises(InvalidDetails):
        validate_details(spec, {"exclude": "peanuts", "reason": "allergy"})


def test_categories_without_details_reject_details() -> None:
    with pytest.raises(InvalidDetails):
        validate_details(CATEGORY_BY_ID["interest"], {"x": 1})
    with pytest.raises(InvalidDetails):
        validate_details(CATEGORY_BY_ID["size"], {"item": "shoes"})


def test_statement_rules() -> None:
    with pytest.raises(InvalidField):
        validate_statement(CATEGORY_BY_ID["location"], "Lives at 221B Baker Street")
    with pytest.raises(InvalidField):
        validate_statement(CATEGORY_BY_ID["birthday"], "Born 14 March 1990")
    assert validate_statement(CATEGORY_BY_ID["birthday"], "Birthday is 14 March")
    assert validate_statement(CATEGORY_BY_ID["interest"], "Ran the 2024 Lisbon marathon")
