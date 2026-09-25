"""Error categories and the single exception type used across the backend.

Every failure surfaced to a caller must carry a stable category string so the
main CLI can branch on it without parsing prose.  ``ok=false`` plus a category
is the only accepted way to report a failure: the backend never returns
``ok=true`` for a state it could not actually verify.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# --- Stable error categories -------------------------------------------------
DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
CONFIG_INVALID = "CONFIG_INVALID"
ENDPOINT_NOT_LOOPBACK = "ENDPOINT_NOT_LOOPBACK"
CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
AUTH_FAILED = "AUTH_FAILED"
CONNECT_FAILED = "CONNECT_FAILED"
RPC_UNSUPPORTED = "RPC_UNSUPPORTED"
BAD_RESPONSE = "BAD_RESPONSE"
OBS_BUSY = "OBS_BUSY"
PROFILE_MISMATCH = "PROFILE_MISMATCH"
SCENE_MISMATCH = "SCENE_MISMATCH"
#: The program scene contains enabled sources besides the one managed capture,
#: so it is not the dedicated scene this backend requires.
SCENE_NOT_DEDICATED = "SCENE_NOT_DEDICATED"
SCENE_COLLECTION_MISMATCH = "SCENE_COLLECTION_MISMATCH"
TARGET_EMPTY = "TARGET_EMPTY"
TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
TARGET_UNSAFE_PRIORITY = "TARGET_UNSAFE_PRIORITY"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
#: The target could not be confirmed either way (OBS did not expose its window
#: list).  Distinct from NOT_FOUND: "unknown" must never be read as "found".
TARGET_UNCONFIRMED = "TARGET_UNCONFIRMED"
SOURCE_DRIFT = "SOURCE_DRIFT"
GLOBAL_AUDIO_MIXED = "GLOBAL_AUDIO_MIXED"
MIC_MIXED = "MIC_MIXED"
AUDIO_NOT_CAPTURED = "AUDIO_NOT_CAPTURED"
NO_PROGRESS = "NO_PROGRESS"
NOT_OWNER = "NOT_OWNER"
STALE_STATE = "STALE_STATE"
OUTPUT_PATH_INVALID = "OUTPUT_PATH_INVALID"
OUTPUT_NOT_FOUND = "OUTPUT_NOT_FOUND"
OUTPUT_EMPTY = "OUTPUT_EMPTY"
INTERNAL = "INTERNAL"

ALL_CATEGORIES = (
    DEPENDENCY_MISSING,
    CONFIG_INVALID,
    ENDPOINT_NOT_LOOPBACK,
    CREDENTIAL_MISSING,
    AUTH_FAILED,
    CONNECT_FAILED,
    RPC_UNSUPPORTED,
    BAD_RESPONSE,
    OBS_BUSY,
    PROFILE_MISMATCH,
    SCENE_MISMATCH,
    SCENE_NOT_DEDICATED,
    SCENE_COLLECTION_MISMATCH,
    TARGET_EMPTY,
    TARGET_AMBIGUOUS,
    TARGET_UNSAFE_PRIORITY,
    TARGET_NOT_FOUND,
    TARGET_UNCONFIRMED,
    SOURCE_DRIFT,
    GLOBAL_AUDIO_MIXED,
    MIC_MIXED,
    AUDIO_NOT_CAPTURED,
    NO_PROGRESS,
    NOT_OWNER,
    STALE_STATE,
    OUTPUT_PATH_INVALID,
    OUTPUT_NOT_FOUND,
    OUTPUT_EMPTY,
    INTERNAL,
)


class CaptureError(Exception):
    """A failure with a stable category and machine-readable detail."""

    def __init__(
        self,
        category: str,
        message: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        if category not in ALL_CATEGORIES:
            raise AssertionError("unknown error category: %r" % (category,))
        super().__init__(message)
        self.category = category
        self.message = message
        self.detail: Dict[str, Any] = dict(detail or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "message": self.message,
            "detail": self.detail,
        }


def error_dict(
    category: str,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "category": category,
        "message": message,
        "detail": dict(detail or {}),
    }
