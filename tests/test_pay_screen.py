"""The pay screen answers only what the posting's own figures settle."""

from __future__ import annotations

from types import SimpleNamespace

from api import db
from core import pay_screen
from tasks.filtering import personal_runs

BAR = "stated_base_under_150k_v1"


def test_a_stated_range_under_the_bar_is_settled():
    text = "US Salary Range\n$86,000\n$114,000 USD\nThe salary range ... base salary only."
    assert pay_screen.screen(BAR, text) is None  # two figures, no range between them
    text = "Base pay: $86,000 - $114,000 per year, plus equity."
    assert pay_screen.screen(BAR, text) == (
        "Stated pay tops out at $114,000, under the $150,000 base bar."
    )
    assert pay_screen.screen(BAR, "Pay: $40.00 - $55.00 per hour") is not None


def test_what_the_figures_do_not_settle_is_left_to_the_model():
    for text in (
        "No pay stated. $5,000 signing bonus.",
        "Base: $120,000 - $160,000.",
        "Base: $120K - $140K; total compensation up to $210,000.",
        "Based in Colorado or Hawaii: $116,960.00 - $146,200.00",
        "Monthly stipend $8,000 - $10,000",
        "$75 - $90 per hour",
    ):
        assert pay_screen.screen(BAR, text) is None, text


def test_only_a_listed_prompt_is_settled_and_the_answer_is_stored(f, set_config):
    uid = f.make_user()
    flt = f.make_filter(uid)
    low, high = (
        db.query_one("SELECT url, company, title FROM jobs WHERE id = %s", (f.make_job(),))
        for _ in range(2)
    )
    contents = {
        low["url"]: SimpleNamespace(text="Salary: $60,000 - $80,000 annually."),
        high["url"]: SimpleNamespace(text="Salary: $160,000 - $190,000 annually."),
    }
    jobs = [low, high]
    assert personal_runs._settle_by_stated_pay(jobs, contents, flt, "m") == jobs

    set_config("pay_screens", {flt["prompt_hash"]: BAR})
    assert personal_runs._settle_by_stated_pay(jobs, contents, flt, "m") == [high]
    row = db.query_one(
        "SELECT status, config_name, model, prompt_hash FROM verdicts WHERE url = %s",
        (low["url"],),
    )
    assert row is not None
    assert dict(row) == {
        "status": "rejected",
        "config_name": "filter-pay-screen",
        "model": "m",
        "prompt_hash": flt["prompt_hash"],
    }
