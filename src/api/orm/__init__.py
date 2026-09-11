"""Every table, grouped by what it is for.

This was one 1,268 line module. It is imported for two different
reasons and both still work: alembic/env.py takes `Base` to compare the
models against the migrations, and that comparison needs EVERY table
registered on one metadata, which is why this file imports all of them
rather than leaving it to whoever imports it first. Everything else
takes a model by name, and those names are re-exported here, so
`from api.orm import Job` reads the same as it always did.
"""

from api.orm.ai import (
    AiBatch,
    AiBatchError,
    AiExperiment,
    AiExperimentResult,
    AiPrompt,
    AiPromptSample,
    AiQuery,
    ApiUsage,
    BatchRequest,
    BatchResultReceipt,
)
from api.orm.apply import (
    ApplicationAnswer,
    ApplicationAnswerBank,
    ApplicationFill,
    ApplicationForm,
    ApplicationReport,
    ExtensionRecipe,
    SuggestionResponse,
    UserResume,
)
from api.orm.base import Base
from api.orm.board import BoardVisible, FilterPreset, SavedView, UserFilter, UserJob, UserJobHistory
from api.orm.catalog import (
    Job,
    JobEmbedding,
    JobListingEvent,
    JobRequirements,
    JobSkill,
    Listing,
    Location,
    Source,
    SourceGroup,
    SourceRequest,
    UserSource,
)
from api.orm.mail import (
    ActionItem,
    Application,
    ApplicationMatch,
    EmailEvent,
    EmailMessage,
    UserOAuthToken,
)
from api.orm.platform import (
    AppConfig,
    GroupBudget,
    HealthAlert,
    HostBudget,
    Report,
    Task,
    TaskModelOverride,
    User,
    UserSettings,
    WorkerStatus,
)

__all__ = [
    "ActionItem",
    "AiBatch",
    "AiBatchError",
    "AiExperiment",
    "AiExperimentResult",
    "AiPrompt",
    "AiPromptSample",
    "AiQuery",
    "ApiUsage",
    "AppConfig",
    "Application",
    "ApplicationAnswer",
    "ApplicationAnswerBank",
    "ApplicationFill",
    "ApplicationForm",
    "ApplicationMatch",
    "ApplicationReport",
    "Base",
    "BatchRequest",
    "BatchResultReceipt",
    "BoardVisible",
    "EmailEvent",
    "EmailMessage",
    "ExtensionRecipe",
    "FilterPreset",
    "GroupBudget",
    "HealthAlert",
    "HostBudget",
    "Job",
    "JobEmbedding",
    "JobListingEvent",
    "JobRequirements",
    "JobSkill",
    "Listing",
    "Location",
    "Report",
    "SavedView",
    "Source",
    "SourceGroup",
    "SourceRequest",
    "SuggestionResponse",
    "Task",
    "TaskModelOverride",
    "User",
    "UserFilter",
    "UserJob",
    "UserJobHistory",
    "UserOAuthToken",
    "UserResume",
    "UserSettings",
    "UserSource",
    "WorkerStatus",
]
