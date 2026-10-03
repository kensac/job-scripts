"""Stable job-profile taxonomy and request recipe."""

from __future__ import annotations

import dataclasses
import functools
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from core.batch import BatchSpec, structured_response_spec

CLASSIFIER_VERSION = "job-profile-v1"
JOB_PROFILE_MODEL = "gpt-6-luna"
JOB_PROFILE_INPUT_CHARS = 12_000

RoleFamily = Literal[
    "engineering",
    "product",
    "design",
    "data",
    "security",
    "sales",
    "marketing",
    "operations",
    "finance",
    "legal",
    "people",
    "customer_success",
    "other",
    "unknown",
]
RoleTrack = Literal[
    "frontend",
    "backend",
    "full_stack",
    "mobile",
    "platform",
    "infrastructure",
    "devops",
    "sre",
    "embedded",
    "systems",
    "qa_test",
    "machine_learning",
    "data_engineering",
    "data_science",
    "analytics",
    "product_management",
    "program_management",
    "security",
    "developer_relations",
    "other",
    "unknown",
]
CareerStage = Literal["internship", "entry", "mid", "senior", "staff", "executive", "unknown"]
EmploymentType = Literal[
    "full_time", "part_time", "contract", "temporary", "internship", "apprenticeship", "unknown"
]
OrganizationSector = Literal[
    "software",
    "fintech",
    "healthcare",
    "government",
    "education",
    "commerce",
    "media",
    "industrial",
    "professional_services",
    "nonprofit",
    "other",
    "unknown",
]
Selectivity = Literal["standard", "selective", "highly_selective", "unknown"]


class JobProfileAnswer(BaseModel):
    primary_role_family: RoleFamily
    role_tracks: list[RoleTrack] = Field(max_length=2)
    career_stage: CareerStage
    employment_type: EmploymentType
    organization_sector: OrganizationSector
    people_manager: bool | None
    company_selectivity: Selectivity
    role_selectivity: Selectivity

    @field_validator("role_tracks")
    @classmethod
    def unique_tracks(cls, value: list[RoleTrack]) -> list[RoleTrack]:
        if len(value) != len(set(value)):
            raise ValueError("role tracks must be unique")
        return value


JOB_PROFILE_INSTRUCTIONS = """Classify one job posting into the supplied taxonomy.
Use unknown whenever the posting does not support a field. role_tracks has zero to two values.
people_manager is true only when the role manages people, false only when it clearly does not,
and null when unclear. Selectivity describes demonstrated selectivity, never prestige guessed
from silence. Return only the schema fields. Do not add prose, explanations, or confidence.
Do not extract location, compensation, posting age, or company name."""


def build_job_profile_input(title: str, content: str) -> str:
    return f"Title: {title}\n\nPosting:\n{content[:JOB_PROFILE_INPUT_CHARS]}"


def job_profile_spec(
    url: str, content_row_id: int, title: str, content: str, content_hash: str
) -> BatchSpec:
    """The one request a profile is classified from. Review gate admission
    rebuilds it to prove a stored request by digest, so both build it here."""
    return dataclasses.replace(
        _job_profile_template(),
        custom_id=str(content_row_id),
        input=build_job_profile_input(title, content),
        context={
            "url": url,
            "content_row_id": content_row_id,
            "content_hash": content_hash,
            "classifier_version": CLASSIFIER_VERSION,
        },
    )


@functools.cache
def _job_profile_template() -> BatchSpec:
    # Generating the schema was 65% of an admission proving 1,000 candidates
    # (0.84 of 1.29 s, measured 2026-10-03); it depends only on the model.
    # Every spec shares this schema dict, which nothing mutates.
    return structured_response_spec("", JOB_PROFILE_INSTRUCTIONS, "", JobProfileAnswer)
