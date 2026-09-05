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


class LocationExtract(BaseModel):
    country: str = ""
    region: str = ""
    city: str = ""
    remote: bool = False


_INSTRUCTIONS = (
    "Classify ONE job-location string into a place.\n"
    "country: the ISO 3166-1 alpha-2 code, uppercase, of the single country the "
    "string refers to. 'United States', 'USA', 'US', a US city, a US state name "
    "or two-letter code are US; 'UK', 'London', 'England' are GB; 'Bengaluru', "
    "'Bangalore', 'Hyderabad', 'Pune' are IN. Empty when the string names no "
    "single country ('Multiple locations', 'US or Canada', 'EMEA', 'Global', "
    "'Anywhere') or is not a place.\n"
    "region: for the US the two-letter state code (NYC and New York are NY, SF "
    "and San Francisco are CA); for Canada the two-letter province code; for "
    "other countries empty. Empty when no state or province is stated or "
    "implied by the city.\n"
    "city: the city in English, title case, without state or country ('San "
    "Francisco', 'New York', 'Bengaluru', 'London'). A neighbourhood, campus or "
    "office name maps to its city. Empty when the string names no city.\n"
    "remote: true when the string says remote, work from home, distributed, "
    "telecommute or anywhere; 'Remote in USA' is remote true with country US. "
    "Hybrid is not remote."
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


def store(text: str, parsed: LocationExtract, model: str) -> None:
    code = text.strip().upper()
    if code in _CODES:
        parsed = LocationExtract(country=_CODES[code], region=code)
    country = _clean(parsed.country, 2)
    db.execute(
        """
        INSERT INTO locations (text, country, region, city, remote, model)
        VALUES (%(text)s, %(country)s, %(region)s, %(city)s, %(remote)s, %(model)s)
        ON CONFLICT (text) DO UPDATE SET
            country = EXCLUDED.country, region = EXCLUDED.region, city = EXCLUDED.city,
            remote = EXCLUDED.remote, model = EXCLUDED.model, classified_at = now()
        """,
        {
            "text": text,
            "country": country,
            # A region or city without a country is a half-answer that could
            # match the wrong country's "CA"; both hang off the country.
            "region": _clean(parsed.region, 2) if country else None,
            "city": (parsed.city.strip() or None) if country else None,
            "remote": bool(parsed.remote),
            "model": model,
        },
    )


async def handle_classify_locations(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    cap = int(db.get_config("classify_locations_per_cycle"))
    texts = [r["text"] for r in db.query(_CANDIDATES, {"cap": cap})]
    if not texts:
        _set_progress(task_id, 0, 0, "nothing to classify")
        return
    schema = to_strict_json_schema(LocationExtract)
    by_id = {_custom_id(t): t for t in texts}
    specs = [
        BatchSpec(cid, _INSTRUCTIONS, t, "LocationExtract", schema) for cid, t in by_id.items()
    ]
    _set_progress(task_id, 0, len(specs), "locations batch submitted (half price)")
    results, chosen = await run_batched(task_id, LOCATIONS_TASK, specs)
    done = 0
    for cid, res in results.items():
        text = by_id.get(cid)
        if text is None or not res.text or res.error:
            continue
        try:
            store(text, LocationExtract.model_validate_json(res.text), chosen.model)
            done += 1
        except Exception:
            # No row, so the next cycle asks again: the same re-sweep contract
            # every batched pass has.
            logger.warning(f"location parse failed for {text!r}")
    _set_progress(task_id, done, len(specs), f"{done} location(s) classified")
