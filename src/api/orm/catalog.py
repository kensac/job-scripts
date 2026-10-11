"""Compatibility exports for catalog ORM models grouped by responsibility."""

from api.orm.catalog_derivations import (
    JobComp,
    JobEmbedding,
    JobProfile,
    JobRequirements,
    JobSkill,
    Location,
)
from api.orm.catalog_postings import Job, PageFetch, PostingUpload
from api.orm.catalog_sources import (
    JobListingEvent,
    Listing,
    Source,
    SourceGroup,
    SourceObservation,
    SourceRequest,
    TitlePattern,
    UserSource,
    UserSourceGroup,
)

__all__ = [
    "Job",
    "JobComp",
    "JobEmbedding",
    "JobListingEvent",
    "JobProfile",
    "JobRequirements",
    "JobSkill",
    "Listing",
    "Location",
    "PageFetch",
    "PostingUpload",
    "Source",
    "SourceGroup",
    "SourceObservation",
    "SourceRequest",
    "TitlePattern",
    "UserSource",
    "UserSourceGroup",
]
