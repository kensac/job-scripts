from api import db
from api.routers.task_models import _view
from core.shapes import LOCATIONS_TASK


def test_location_budget_allows_multi_place_answers():
    # The old 120-token ceiling truncated every invalid receipt in the
    # seven-day production sample, including strings naming 8+ cities.
    assert LOCATIONS_TASK.max_output_tokens >= 1024


def test_location_budget_setting_reaches_task_pricing():
    db.execute(
        "INSERT INTO app_config(key,value) VALUES ('classify_locations_max_output_tokens','4096') "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
    )
    assert _view("locations").cost_basis.max_output_tokens == 4096
