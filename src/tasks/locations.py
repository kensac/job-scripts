"""Location classification: every distinct location string, placed once."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from pydantic import BaseModel

from api import db
from api.ai.batch_results import progress_counts
from api.locations import LocationExtract, Place, store
from core.shapes import LOCATIONS_TASK
from tasks.runtime import consume_result, has_batch_work, run_batched, set_progress

logger = logging.getLogger(__name__)


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


async def handle_classify_locations(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    specs = []
    if not has_batch_work(task_id):
        cap = int(db.get_config("classify_locations_per_cycle"))
        if payload.get("reclassify"):
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
            set_progress(task_id, 0, 0, "nothing to classify")
            return
        schema = to_strict_json_schema(LocationAnswer)
        specs = [
            BatchSpec(
                _custom_id(text),
                _INSTRUCTIONS,
                text,
                "LocationAnswer",
                schema,
                context={"text": text},
            )
            for text in texts
        ]
        set_progress(task_id, 0, len(specs), "locations batch submitted")
    results, _ = await run_batched(task_id, LOCATIONS_TASK, specs)
    for res in results:
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            context = res.request.context if res.request else None
            if not context or "text" not in context:
                receipt.outcome = "unknown_request"
                continue
            if not res.text or res.error:
                receipt.outcome = "failed"
                continue
            try:
                answer = LocationAnswer.model_validate_json(res.text)
            except ValueError:
                logger.warning("location parse failed for %r", context["text"])
                receipt.outcome = "invalid_output"
                continue
            written = store(
                context["text"],
                LocationExtract(places=answer.places, remote=answer.remote),
                res.model,
                preserve_manual=True,
            )
            receipt.outcome = "written" if written else "superseded"
    done, total = progress_counts(task_id)
    set_progress(task_id, done, total, f"{done} location(s) classified")
