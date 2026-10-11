from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import db
from tasks import application as drafts


@pytest.fixture(autouse=True)
def _available_owner_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")


GREENHOUSE = json.dumps(
    {
        "questions": [
            {
                "label": "First Name",
                "required": True,
                "fields": [{"name": "first_name", "type": "input_text"}],
            },
            {
                "label": "Resume/CV",
                "required": True,
                "fields": [
                    {"name": "resume", "type": "input_file"},
                    {"name": "resume_text", "type": "textarea"},
                ],
            },
            {
                "label": "Cover Letter",
                "required": False,
                "fields": [
                    {"name": "cover_letter", "type": "input_file"},
                    {"name": "cover_letter_text", "type": "textarea"},
                ],
            },
            {
                "label": "Why Anthropic?",
                "required": True,
                "fields": [{"name": "question_1001", "type": "textarea"}],
            },
            {
                "label": "LinkedIn Profile",
                "required": False,
                "fields": [{"name": "question_1002", "type": "input_text"}],
            },
            {
                "label": "Are you authorized to work?",
                "required": True,
                "fields": [
                    {"name": "question_1003", "type": "multi_value_single_select", "values": []}
                ],
            },
        ]
    }
)
ASHBY = json.dumps(
    {
        "data": {
            "jobPosting": {
                "applicationForm": {
                    "sections": [
                        {
                            "title": "Contact",
                            "fieldEntries": [
                                {
                                    "field": {
                                        "path": "_systemfield_name",
                                        "title": "Name",
                                        "type": "String",
                                        "isNullable": False,
                                    }
                                },
                                {
                                    "field": {
                                        "path": "_systemfield_resume",
                                        "title": "Resume",
                                        "type": "File",
                                        "isNullable": False,
                                    }
                                },
                                {
                                    "field": {
                                        "path": "b1f2",
                                        "title": "Additional Information",
                                        "type": "LongText",
                                        "isNullable": False,
                                    }
                                },
                            ],
                        }
                    ]
                }
            }
        }
    }
)
LEVER = (
    '<ul><li class="application-question custom-question"><div>'
    '<div class="application-label full-width textarea"><div class="text">Export Control: '
    "list your citizenships<span>✱</span></div></div>"
    '<div class="application-field full-width"><textarea class="card-field-input" '
    'name="cards[abc][field0]"></textarea></div></div></li>'
    '<li class="application-question"><div class="application-label">Full name<span>✱</span></div>'
    '<div class="application-field"><input type="text" name="name"></div></li>'
    '<li class="application-question"><div><div class="application-label full-width textarea">'
    '<div class="text">Additional information</div></div><div class="application-field">'
    '<textarea name="comments"></textarea></div></div></li></ul>'
)
WORKABLE = json.dumps(
    [
        {
            "name": "Personal information",
            "fields": [
                {"id": "firstname", "type": "text", "label": "First name", "required": True}
            ],
        },
        {
            "name": "Profile",
            "fields": [
                {"id": "resume", "type": "file", "label": "Resume", "required": True},
                {
                    "id": "cover_letter",
                    "type": "paragraph",
                    "label": "Cover letter",
                    "required": False,
                },
            ],
        },
        {
            "name": "Questions",
            "fields": [
                {"id": "q9", "type": "paragraph", "label": "Why this role?", "required": True},
                {"id": "q10", "type": "text", "label": "Notice period", "required": False},
            ],
        },
    ]
)


def _user_id(sub: str = "test-user") -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (sub,))
    assert row is not None
    return row["id"]


def _job_for(f, user_id: int, url: str) -> int:
    job_id, _ = f.make_ready_job(url=url, company="Anthropic", title="Engineer")
    f.make_board_row(user_id, job_id)
    return job_id


def _owner_config(monkeypatch):
    from api import ai

    cfg = ai.AIConfig(provider="openai", api_key="k", key_source="owner", model="gpt-5.6-luna")
    monkeypatch.setattr(drafts, "load_config", lambda uid: (SimpleNamespace(), cfg))
