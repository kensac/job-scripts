"""Location classification: every distinct location string, placed once."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel

from api import db, user_settings
from api.locations import LocationExtract, Place, store
from core import catalog
from core.batch import BatchResult, BatchSpec, structured_response_spec
from core.shapes import LOCATIONS_TASK
from tasks.derive import Derivation, Row


class LocationAnswer(BaseModel):
    """What the model returns: every place the string names, and whether it
    says remote. A string naming three cities is three entries."""

    places: list[Place] = []
    remote: bool = False


_INSTRUCTIONS = (
    "Classify ONE job-location string into the places it names.\n"
    "places: one entry per distinct place the string names, in the order "
    "written. 'London, Montreal, Singapore' is three entries; 'United States "
    "and Canada' is two (countries with no city); 'San Jose, CA, United "
    "States' is one. A bare town name IS a place: give its most likely "
    "country and region (Golden is US CO, Normal is US IL, Novi is US MI, "
    "Alexandria is US VA, Montevideo is UY). Empty only when the string "
    "names no place at all ('In-Office', 'N/A', '13 Locations', 'Multiple "
    "Locations') or only a continent or region ('Europe', 'EMEA', 'North "
    "America', 'Middle East', 'Asia').\n"
    "country: the ISO 3166-1 alpha-2 code, uppercase. 'United States', 'USA', "
    "'US', a US city, a US state name or two-letter code are US; 'UK', "
    "'London', 'England' are GB; 'Bengaluru', 'Bangalore', 'Hyderabad', 'Pune' "
    "are IN.\n"
    "region: for the US the two-letter state code (NYC and New York are NY, SF "
    "and San Francisco are CA); for Canada the two-letter province code; for "
    "other countries empty. Empty when no state or province is stated or "
    "implied by the city.\n"
    "city: the city in English, title case, without state or country ('San "
    "Francisco', 'New York', 'Bengaluru', 'London'). A neighbourhood, campus or "
    "office name maps to its city. Empty for a country or state alone.\n"
    "remote: true when the string says remote, work from home, distributed, "
    "telecommute or anywhere; 'Remote in USA' is remote true with one place, "
    "country US. Hybrid is not remote."
)

# Strings from every active posting plus every user's exclusion criteria (a
# criterion is a location string too, and it is matched as a place the same
# way), minus the ones already classified.
_CANDIDATES = f"""
    WITH raw AS (
        SELECT DISTINCT btrim(loc) AS text
        FROM jobs j, unnest(j.locations) AS loc
        WHERE {catalog.IS_AVAILABLE.format(job="j")} AND btrim(loc) <> ''
        UNION
{user_settings.CRITERIA_LOCATIONS_SQL}    )
    SELECT r.text FROM raw r
    LEFT JOIN locations l ON l.text = r.text
    WHERE l.text IS NULL
    ORDER BY r.text
    LIMIT %(cap)s
"""


def _custom_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _select(cap: int, payload: dict[str, Any]) -> list[Row]:
    if payload.get("reclassify"):
        return db.query(
            "SELECT text FROM locations WHERE model <> 'admin' ORDER BY text LIMIT %(cap)s",
            {"cap": cap},
        )
    return db.query(_CANDIDATES, {"cap": cap})


def _requests(rows: list[Row]) -> list[BatchSpec]:
    return [
        structured_response_spec(
            _custom_id(r["text"]),
            _INSTRUCTIONS,
            r["text"],
            LocationAnswer,
            context={"text": r["text"]},
        )
        for r in rows
    ]


def _store(result: BatchResult, context: dict[str, Any], answer: LocationAnswer) -> str:
    written = store(
        context["text"],
        LocationExtract(places=answer.places, remote=answer.remote),
        result.model,
        preserve_manual=True,
    )
    return "written" if written else "superseded"


# The input is a location string, not page text: a string is classified once
# and never goes stale, so the staleness rule is "not classified yet" (or every
# non-admin row, for a reclassify run).
LOCATIONS = Derivation(
    kind="classify_locations",
    purpose=LOCATIONS_TASK.purpose,
    noun="location",
    table="locations",
    per_cycle_key="classify_locations_per_cycle",
    select=_select,
    requests=_requests,
    store=_store,
    input_chars=None,
    recipe=None,
    shape=LOCATIONS_TASK,
    answer=LocationAnswer,
    context_keys=("text",),
)
