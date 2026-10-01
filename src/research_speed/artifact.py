# ruff: noqa: E501
"""Artifact fingerprint: the six inputs that determine an expensive result -> ARTIFACT_ID.

    DATA_HASH          content fingerprint of the input data (e.g. ``backfill.frame_fingerprint``)
    FEATURE_CODE_HASH  content hash of the import closure of the feature / label / generator code
    CONTROL_CODE_HASH  content hash of the import closure of the control-selection code ("-" when not applicable)
    CONFIG_HASH        hash of every parameter and config file the result depends on (incl. upstream ARTIFACT_IDs)
    LABEL_VERSION      label convention / observer / schema version string
    PREREG_VERSION     preregistration version ("-" when the result is not preregistered)

Identical fingerprint -> identical ARTIFACT_ID -> CACHE HIT. Any single component differing -> a different ARTIFACT_ID
and the reason names the component(s). There is no "close enough" reuse.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

COMPONENTS = (
    "data_hash",
    "feature_code_hash",
    "control_code_hash",
    "config_hash",
    "label_version",
    "prereg_version",
)
NOT_APPLICABLE = "-"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(obj: Any) -> str:
    """sha256 of the canonical JSON of ``obj`` (dicts sorted; non-JSON values via ``str``)."""
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


@dataclass(frozen=True)
class ArtifactFingerprint:
    data_hash: str
    feature_code_hash: str
    control_code_hash: str
    config_hash: str
    label_version: str
    prereg_version: str

    def components(self) -> dict[str, str]:
        return asdict(self)

    @property
    def artifact_id(self) -> str:
        return hashlib.sha256(canonical_json(self.components()).encode()).hexdigest()

    def diff(self, other: dict[str, str] | ArtifactFingerprint) -> list[str]:
        """Names of the components that differ from ``other`` (empty list = identical)."""
        o = other.components() if isinstance(other, ArtifactFingerprint) else other
        mine = self.components()
        return [k for k in COMPONENTS if mine.get(k) != o.get(k)]
