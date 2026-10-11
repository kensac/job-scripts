"""Register every table on one metadata and re-export the model names.

Alembic imports `Base` here to compare all models with migrations. Importing
each domain module registers its tables before that comparison. Other callers
can import individual models from `api.orm`.
"""

from api.orm.ai import (
    AiBatch,
    AiBatchError,
    AiInstructionText,
    AiQuery,
    BatchRequest,
    BatchResultReceipt,
    ModelCall,
)
from api.orm.apply import (
    ApplicationAnswer,
    ApplicationAnswerBank,
    ApplicationFill,
    ApplicationForm,
    ApplicationReport,
    ExtensionRecipe,
    UserResume,
)
from api.orm.base import Base
from api.orm.board import (
    BoardVisible,
    FilterPreset,
    SavedView,
    UserFilter,
    UserJob,
    UserJobHistory,
    UserJobWorkingSet,
)
from api.orm.catalog_derivations import (
    JobEmbedding,
    JobProfile,
    JobRequirements,
    JobSkill,
    Location,
)
from api.orm.catalog_postings import Job, PostingUpload
from api.orm.catalog_sources import (
    JobListingEvent,
    Listing,
    Source,
    SourceGroup,
    SourceRequest,
    UserSource,
)
from api.orm.mail import (
    Application,
    ApplicationMatch,
    EmailEvent,
    EmailMessage,
    EventAnswer,
    GmailScanCursor,
    UserOAuthToken,
)
from api.orm.managed_boards import ManagedBoard, ManagedBoardJob, ManagedBoardSource
from api.orm.platform import (
    AppConfig,
    AppConfigChange,
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
    "AiBatch",
    "AiBatchError",
    "AiInstructionText",
    "AiQuery",
    "AppConfig",
    "AppConfigChange",
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
    "EventAnswer",
    "GmailScanCursor",
    "ExtensionRecipe",
    "FilterPreset",
    "GroupBudget",
    "HealthAlert",
    "HostBudget",
    "Job",
    "JobEmbedding",
    "JobListingEvent",
    "JobProfile",
    "JobRequirements",
    "JobSkill",
    "Listing",
    "Location",
    "ManagedBoard",
    "ManagedBoardJob",
    "ManagedBoardSource",
    "ModelCall",
    "PostingUpload",
    "Report",
    "SavedView",
    "Source",
    "SourceGroup",
    "SourceRequest",
    "Task",
    "TaskModelOverride",
    "User",
    "UserFilter",
    "UserJob",
    "UserJobHistory",
    "UserJobWorkingSet",
    "UserOAuthToken",
    "UserResume",
    "UserSettings",
    "UserSource",
    "WorkerStatus",
]
