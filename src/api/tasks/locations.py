"""Location classification: every distinct location string, placed once."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from pydantic import BaseModel

from api import db
from api.tasks.runtime import _set_progress, run_batched
from core.providers import StructuredOutput
from core.routing import TaskShape

logger = logging.getLogger("jobtracker_worker")

# Strings are short and the answer is a lookup the model already knows, so the
# whole backlog (8,735 distinct strings on 2026-09-04) fits one cycle; after
# that a cycle carries only the strings new boards wrote since the last one.
# Persisted config (classify_locations_per_cycle), so the first pass can be
# a small sample read off GET /admin/locations before the backlog is paid for.
CLASSIFY_LOCATIONS_PER_CYCLE = 10000

LOCATIONS_TASK = TaskShape(
    purpose="locations",
    label="Location classification",
    per_cycle=CLASSIFY_LOCATIONS_PER_CYCLE,
    notes=(
        "Naming the country, state and city a short string refers to is a "
        "lookup, not a judgment about silence: the shape gpt-5-nano handles. "
        "A string that names no single place is left empty, which excludes "
        "nothing, so the cost of a wrong answer is one visible posting."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=120,
    est_prompt_tokens=260,
    effort_preference=("minimal", "low"),
    candidates=("gpt-5-nano",),
)


class Place(BaseModel):
    country: str = ""
    region: str = ""
    city: str = ""


class LocationAnswer(BaseModel):
    """What the model returns: every place the string names, and whether it
    says remote. A string naming three cities is three entries."""

    places: list[Place] = []
    remote: bool = False


class LocationExtract(BaseModel):
    """What store() takes. One place through country/region/city, or several
    through places; the first is what the display columns carry."""

    country: str = ""
    region: str = ""
    city: str = ""
    remote: bool = False
    places: list[Place] = []

    def all_places(self) -> list[Place]:
        if self.places:
            return self.places
        if self.country:
            return [Place(country=self.country, region=self.region, city=self.city)]
        return []


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
_CANDIDATES = """
    WITH raw AS (
        SELECT DISTINCT btrim(loc) AS text
        FROM jobs j, unnest(j.locations) AS loc
        WHERE j.active AND btrim(loc) <> ''
        UNION
        SELECT DISTINCT btrim(e)
        FROM user_settings s,
             jsonb_array_elements_text(
                 COALESCE(s.criteria->'excluded_locations', '[]'::jsonb)
                 || COALESCE(s.criteria->'included_locations', '[]'::jsonb)) AS e
        WHERE btrim(e) <> ''
    )
    SELECT r.text FROM raw r
    LEFT JOIN locations l ON l.text = r.text
    WHERE l.text IS NULL
    ORDER BY r.text
    LIMIT %(cap)s
"""


def _custom_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


# A bare two-letter code is a US state or a Canadian province on these boards,
# and the model got a third of them wrong on the first pass over production:
# CA became Canada (1,604 postings say CA and mean California), IN India, DE
# Germany, AR Argentina, ME Britain, ON the US, and a dozen states grew an
# invented city. Sixty-three codes is a table, not a judgment, so it is one.
_US_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
]
_CA_PROVINCES = ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"]
_CODES = {code: "US" for code in _US_STATES} | {code: "CA" for code in _CA_PROVINCES}


def _clean(value: str, length: int) -> str | None:
    v = value.strip()
    return v.upper() if v and len(v) == length and v.isalpha() else None


def _normalised(place: Place) -> dict[str, str | None] | None:
    """A place with a valid country, or nothing: a region or city without a
    country is a half-answer that could match the wrong country's CA."""
    country = _clean(place.country, 2)
    if not country:
        return None
    return {
        "country": country,
        "region": _clean(place.region, 2),
        "city": place.city.strip() or None,
    }


def store(text: str, parsed: LocationExtract, model: str) -> None:
    code = text.strip().upper()
    if code in _CODES:
        parsed = LocationExtract(country=_CODES[code], region=code)
    places = [p for p in (_normalised(pl) for pl in parsed.all_places()) if p is not None]
    first = places[0] if places else {"country": None, "region": None, "city": None}
    db.execute(
        """
        INSERT INTO locations (text, country, region, city, remote, places, model)
        VALUES (%(text)s, %(country)s, %(region)s, %(city)s, %(remote)s, %(places)s, %(model)s)
        ON CONFLICT (text) DO UPDATE SET
            country = EXCLUDED.country, region = EXCLUDED.region, city = EXCLUDED.city,
            remote = EXCLUDED.remote, places = EXCLUDED.places, model = EXCLUDED.model,
            classified_at = now()
        """,
        {
            "text": text,
            "country": first["country"],
            "region": first["region"],
            "city": first["city"],
            "remote": bool(parsed.remote),
            "places": db.jsonb(places),
            "model": model,
        },
    )


async def handle_classify_locations(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    cap = int(db.get_config("classify_locations_per_cycle"))
    if payload.get("reclassify"):
        # Every model-made row again, in place: a row keeps its old answer
        # until the new one lands, so a filter never sees a gap. Hand
        # corrections are never re-asked.
        texts = [
            r["text"]
            for r in db.query(
                "SELECT text FROM locations WHERE model <> 'admin' ORDER BY text LIMIT %(cap)s",
                {"cap": cap},
            )
        ]
    else:
        texts = [r["text"] for r in db.query(_CANDIDATES, {"cap": cap})]
    if not texts:
        _set_progress(task_id, 0, 0, "nothing to classify")
        return
    schema = to_strict_json_schema(LocationAnswer)
    by_id = {_custom_id(t): t for t in texts}
    specs = [BatchSpec(cid, _INSTRUCTIONS, t, "LocationAnswer", schema) for cid, t in by_id.items()]
    _set_progress(task_id, 0, len(specs), "locations batch submitted (half price)")
    results, chosen = await run_batched(task_id, LOCATIONS_TASK, specs)
    done = 0
    for cid, res in results.items():
        text = by_id.get(cid)
        if text is None or not res.text or res.error:
            continue
        try:
            answer = LocationAnswer.model_validate_json(res.text)
            store(text, LocationExtract(places=answer.places, remote=answer.remote), chosen.model)
            done += 1
        except Exception:
            # No row, so the next cycle asks again: the same re-sweep contract
            # every batched pass has.
            logger.warning(f"location parse failed for {text!r}")
    _set_progress(task_id, done, len(specs), f"{done} location(s) classified")
