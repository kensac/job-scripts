"""Data-only controls for functionality shipped in the extension package."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

AdapterId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class ExtensionFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    autofill: bool = True
    ai_suggestions: bool = True
    resume_upload: bool = True
    auto_advance: bool = True


class ExtensionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    features: ExtensionFeatures = Field(default_factory=ExtensionFeatures)
    disabled_adapters: list[AdapterId] = Field(default_factory=list, max_length=256)
    # Five minutes is the initial operational choice for revocation delay,
    # not a browser guarantee. Keep it editable with the policy it expires.
    max_age_seconds: int = Field(default=300, ge=0, le=86400)


class FeatureAvailability(BaseModel):
    allowed: bool
    reason: Literal["FEATURE_DISABLED", "ADAPTER_DISABLED"] | None


class ExtensionAvailability(BaseModel):
    autofill: FeatureAvailability
    ai_suggestions: FeatureAvailability
    resume_upload: FeatureAvailability
    auto_advance: FeatureAvailability


class ExtensionConfig(BaseModel):
    schema_version: Literal[1] = 1
    revision: str
    adapter: str
    max_age_seconds: int
    features: ExtensionAvailability


def resolve(policy: ExtensionPolicy, adapter: str) -> ExtensionConfig:
    # Hash the effective response, so rollback to an earlier policy restores
    # its revision. There is no mutable version counter to race with a write.
    blocked = adapter in policy.disabled_adapters
    features = {}
    for name, enabled in policy.features.model_dump().items():
        reason = (
            "ADAPTER_DISABLED"
            if blocked
            else ("FEATURE_DISABLED" if not policy.features.autofill or not enabled else None)
        )
        features[name] = {"allowed": reason is None, "reason": reason}
    payload = {
        "schema_version": 1,
        "adapter": adapter,
        "max_age_seconds": policy.max_age_seconds,
        "features": features,
    }
    revision = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExtensionConfig.model_validate({**payload, "revision": revision})
