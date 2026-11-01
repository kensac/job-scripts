"""Filling a source's company names only its nameless rows."""

from __future__ import annotations

from api import db
from core import catalog
from core.catalog import jobs as catalog_jobs
from tests.factories import make_job


def test_fills_only_the_sources_nameless_rows_in_chunks(monkeypatch):
    monkeypatch.setattr(catalog_jobs, "_BATCH", 2)
    board = "https://jobs.lever.co/sonarsource"
    for i in range(5):
        make_job(url=f"{board}/{i}", source="lever_sonarsource", company="")
    make_job(url=f"{board}/named", source="lever_sonarsource", company="Sonar")
    make_job(url="https://jobs.lever.co/other/1", source="lever_other", company="")

    assert catalog.fill_source_company("lever_sonarsource", " SonarSource ") == 5
    assert catalog.fill_source_company("lever_sonarsource", "SonarSource") == 0
    companies = {r["url"]: r["company"] for r in db.query("SELECT url, company FROM jobs")}
    assert companies == {
        **{f"{board}/{i}": "SonarSource" for i in range(5)},
        f"{board}/named": "Sonar",
        "https://jobs.lever.co/other/1": "",
    }
