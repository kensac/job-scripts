"""Shared sample profile for assisted application tests."""

from __future__ import annotations


def ada_profile() -> dict:
    return {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "+1 555 0100",
        "city": "London",
        "country": "United Kingdom",
        "linkedin": "https://linkedin.com/in/ada",
        "work_authorized": "yes",
        "needs_sponsorship": "no",
        "experience": [{"company": "Analytical Engines", "title": "Engineer", "current": True}],
        "education": [{"school": "Home", "degree": "Bachelor's", "field": "Mathematics"}],
    }
