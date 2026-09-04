"""A detector's detail is dict(row), so it carries whatever SQL returned.

data_health failed 11 times between 2026-09-03 and 2026-09-04 with "Object of
type Decimal is not JSON serializable", while succeeding in between: a finding
carries a Decimal rate, an empty result carries nothing. The suite worked
whenever it had nothing to say.
"""

import datetime
import decimal
import json

from api import db


def _dumped(value):
    """What the driver will write: jsonb() dumps with the same default."""
    return json.loads(json.dumps(value, default=db._json_default))


def test_decimal_survives_as_a_number():
    assert _dumped({"rate": decimal.Decimal("0.27")}) == {"rate": 0.27}


def test_datetime_survives_as_a_string():
    ts = datetime.datetime(2026, 9, 4, 7, 9, tzinfo=datetime.UTC)
    assert _dumped({"seen": ts}) == {"seen": "2026-09-04T07:09:00+00:00"}


def test_a_detector_row_serializes():
    row = {"source": "internships", "rate": decimal.Decimal("0.31"), "n": 50}
    assert _dumped({"detail": row})["detail"]["rate"] == 0.31


def test_jsonb_still_wraps_plain_values():
    assert db.jsonb({"a": 1}).obj == {"a": 1}
