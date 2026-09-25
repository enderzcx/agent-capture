"""Strict accessors for obs-websocket response fields.

A missing or wrongly typed field is a **broken response**, not a default.  The
earlier code used ``bool(data.get("outputActive"))``, which silently reads a
missing field (or a typo'd one, or an error payload) as ``False`` -- i.e. as
"OBS is idle".  That is exactly the "unknown counted as pass" failure mode, so
every field this backend depends on now goes through one of these helpers and
raises ``BAD_RESPONSE`` when it is absent or the wrong type.

Booleans are required to be real JSON booleans: ``1``/``0``/``"true"`` are
rejected, because accepting them would mean guessing what the server meant.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .errors import BAD_RESPONSE, CaptureError


def _fail(request_type: str, field: str, expected: str, value: Any, data: Any) -> CaptureError:
    present = isinstance(data, dict) and field in data
    return CaptureError(
        BAD_RESPONSE,
        "%s response %s the %r field (%s)"
        % (request_type, "mistyped" if present else "is missing", field, expected),
        {
            "request_type": request_type,
            "field": field,
            "expected": expected,
            "present": present,
            "got_type": type(value).__name__ if present else None,
            "response_keys": sorted(data.keys()) if isinstance(data, dict) else None,
        },
    )


def require_bool(data: Any, field: str, request_type: str) -> bool:
    if not isinstance(data, dict) or field not in data:
        raise _fail(request_type, field, "boolean", None, data)
    value = data[field]
    if not isinstance(value, bool):
        raise _fail(request_type, field, "boolean", value, data)
    return value


def require_number(data: Any, field: str, request_type: str) -> float:
    """A finite number; booleans do not count (``isinstance(True, int)``)."""
    if not isinstance(data, dict) or field not in data:
        raise _fail(request_type, field, "finite number", None, data)
    value = data[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(request_type, field, "finite number", value, data)
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise _fail(request_type, field, "finite number", value, data)
    return number


def require_int(data: Any, field: str, request_type: str) -> int:
    number = require_number(data, field, request_type)
    if number != int(number):
        raise _fail(request_type, field, "integer", data.get(field), data)
    return int(number)


def require_str(data: Any, field: str, request_type: str, *, allow_empty: bool = True) -> str:
    if not isinstance(data, dict) or field not in data:
        raise _fail(request_type, field, "string", None, data)
    value = data[field]
    if not isinstance(value, str):
        raise _fail(request_type, field, "string", value, data)
    if not allow_empty and not value.strip():
        raise _fail(request_type, field, "non-empty string", value, data)
    return value


def require_dict(data: Any, field: str, request_type: str) -> Dict[str, Any]:
    if not isinstance(data, dict) or field not in data:
        raise _fail(request_type, field, "object", None, data)
    value = data[field]
    if not isinstance(value, dict):
        raise _fail(request_type, field, "object", value, data)
    return value


def require_list(data: Any, field: str, request_type: str) -> List[Any]:
    if not isinstance(data, dict) or field not in data:
        raise _fail(request_type, field, "array", None, data)
    value = data[field]
    if not isinstance(value, list):
        raise _fail(request_type, field, "array", value, data)
    return value


def optional_bool(data: Any, field: str, request_type: str) -> Optional[bool]:
    """A boolean that may legitimately be absent, but must not be mistyped."""
    if not isinstance(data, dict) or field not in data or data[field] is None:
        return None
    return require_bool(data, field, request_type)


def optional_str(data: Any, field: str, request_type: str) -> Optional[str]:
    if not isinstance(data, dict) or field not in data or data[field] is None:
        return None
    return require_str(data, field, request_type)


def optional_number(data: Any, field: str, request_type: str) -> Optional[float]:
    if not isinstance(data, dict) or field not in data or data[field] is None:
        return None
    return require_number(data, field, request_type)
