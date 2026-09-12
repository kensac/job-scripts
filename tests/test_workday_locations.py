"""A Workday posting's location, from whichever field actually says.

`locationsText` is the intended field. Measured against live tenants on
2026-09-12, three of them do not use it as intended: Accenture and Thomson
Reuters omit it entirely, BlackRock sends a count ("2 Locations").

Both failures defeat a location filter, in opposite directions, which is why
neither was noticed:

  - A count never matches the `locations` vocabulary, so with
    `included_locations` set the posting is silently EXCLUDED. 1,002 postings
    in the catalog carry one.
  - An empty list is deliberately KEPT by the same predicate ("a posting with
    no location at all stays, having nothing to judge"), so it bypasses the
    filter. 162 carry none, and that is how an Accenture posting in Jakarta
    reached a United States board.
"""

from __future__ import annotations

from core.fetching.boards import _workday_locations


def test_a_real_locations_text_is_used_as_is():
    assert _workday_locations(
        {"locationsText": "New York, NY", "externalPath": "/job/Elsewhere/Title_R1"}
    ) == ["New York, NY"]


def test_a_missing_locations_text_falls_back_to_the_path():
    """The Accenture case, and the one that put Jakarta on a US board."""
    assert _workday_locations(
        {"locationsText": None, "externalPath": "/job/Jakarta/Graduate-Analyst_R00336202"}
    ) == ["Jakarta"]


def test_a_count_is_not_a_place():
    """BlackRock sends "2 Locations". Storing that as a location is worse than
    storing nothing: it can never match, so the posting is dropped silently."""
    assert _workday_locations(
        {"locationsText": "2 Locations", "externalPath": "/job/Mumbai-India/Tech_R266332"}
    ) == ["Mumbai India"]
    assert _workday_locations({"locationsText": "  3 locations ", "externalPath": ""}) == []


def test_run_together_hyphens_are_the_tenant_s_own_separator():
    """Replacing "---" and then "-" eats the hyphen the first pass wrote, so
    this is done in one pass."""
    assert _workday_locations(
        {"locationsText": None, "externalPath": "/job/Pernambuco---Recife/Pessoa_R253057"}
    ) == ["Pernambuco - Recife"]
    assert _workday_locations(
        {
            "locationsText": None,
            "externalPath": "/job/United-States-of-America-Eagan-Minnesota/A_J1",
        }
    ) == ["United States of America Eagan Minnesota"]


def test_a_path_with_no_place_segment_invents_nothing():
    """Some postings carry only a title. An empty list is honest; a guess
    would put a posting on a board it does not belong to."""
    assert (
        _workday_locations(
            {"locationsText": None, "externalPath": "/job/XMLNAME-2027-Program-Analyst_R265178"}
        )
        == []
    )
    assert _workday_locations({"locationsText": None, "externalPath": ""}) == []
