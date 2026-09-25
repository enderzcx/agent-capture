"""Window target semantics for OBS ``window_capture`` on Windows.

Everything here mirrors the official OBS implementation, not third-party
documentation:

* ``libobs/util/windows/window-helpers.h`` defines the priority enum::

      enum window_priority { WINDOW_PRIORITY_CLASS, WINDOW_PRIORITY_TITLE, WINDOW_PRIORITY_EXE };

  i.e. CLASS = 0, TITLE = 1, EXE = 2.  Note that this is *not* the order the
  property list is displayed in, so the integer must never be guessed.

* ``ms_build_window_strings`` splits the ``window`` setting on ``":"`` into
  ``title:class:exe`` and decodes ``#3A`` -> ``:`` and ``#22`` -> ``#``.

* ``ms_find_window`` enumerates top-level windows, rates each one, keeps the
  lowest rating and stops early on a rating of 0.  OBS therefore selects a
  *window*, never a stable handle: there is no HWND and no PID input anywhere
  in the ``window_capture`` settings, and OBS re-runs the search whenever the
  capture needs to re-hook.

Consequences that the backend must state honestly instead of papering over:

* HWND pinning is **not** available.
* A same-title / same-class / same-exe window can take over the match; this
  cannot be fully prevented by any setting OBS exposes.
* ``priority="class"`` is silently degraded to title matching by OBS when the
  class is "generic" (``Chrome``, ``SDL_app``), which is not what the caller
  asked for -- so we refuse it up front.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .errors import TARGET_AMBIGUOUS, TARGET_EMPTY, TARGET_UNSAFE_PRIORITY, CaptureError

# --- Official enum values (libobs/util/windows/window-helpers.h) -------------
PRIORITY_CLASS = 0
PRIORITY_TITLE = 1
PRIORITY_EXE = 2

PRIORITY_BY_NAME = {"class": PRIORITY_CLASS, "title": PRIORITY_TITLE, "exe": PRIORITY_EXE}
PRIORITY_NAMES = {v: k for k, v in PRIORITY_BY_NAME.items()}

#: Substrings that make a window class "generic" in OBS
#: (``generic_class_substrings`` in libobs/util/windows/window-helpers.c).
GENERIC_CLASS_SUBSTRINGS = ("chrome", "sdl_app")

#: Match dimensions OBS actually exposes for ``window_capture``.
SUPPORTED_MATCH_DIMENSIONS = ("title", "class", "exe")

#: Things callers often assume exist but that the OBS setting surface has no
#: field for at all.
UNSUPPORTED_MATCH_DIMENSIONS = ("hwnd", "pid", "process_creation_time", "z_order")


def encode_window_field(value: str) -> str:
    """Encode one field for the ``window`` setting (``encode_dstr``)."""
    return value.replace("#", "#22").replace(":", "#3A")


def decode_window_field(value: str) -> str:
    """Decode one field of the ``window`` setting (``decode_str``)."""
    return value.replace("#3A", ":").replace("#22", "#")


def build_window_string(title: str, window_class: str, exe: str) -> str:
    """Build the exact ``window`` setting value OBS stores for a window."""
    return ":".join(
        encode_window_field(part) for part in (title or "", window_class or "", exe or "")
    )


def parse_window_string(value: str) -> Tuple[str, str, str]:
    """Split a ``window`` setting value into ``(title, class, exe)``.

    Returns empty strings for fields that are missing, mirroring the tolerant
    behaviour of ``ms_build_window_strings`` (which leaves the outputs NULL).
    """
    if value is None:
        return ("", "", "")
    parts = value.split(":")
    if len(parts) < 3:
        return ("", "", "")
    return (
        decode_window_field(parts[0]),
        decode_window_field(parts[1]),
        decode_window_field(parts[2]),
    )


def is_generic_class(window_class: str) -> bool:
    lowered = (window_class or "").lower()
    return any(token in lowered for token in GENERIC_CLASS_SUBSTRINGS)


@dataclass(frozen=True)
class WindowTarget:
    """A requested capture target, expressed only in terms OBS can honour."""

    title: str = ""
    window_class: str = ""
    exe: str = ""
    priority: int = PRIORITY_EXE

    @classmethod
    def from_config(cls, raw: Optional[Dict[str, Any]]) -> "WindowTarget":
        if raw is None:
            raise CaptureError(TARGET_EMPTY, "no target given; refusing to guess a window")
        if not isinstance(raw, dict):
            raise CaptureError(TARGET_AMBIGUOUS, "target must be an object", {"got": type(raw).__name__})

        priority_raw = raw.get("priority", "exe")
        if isinstance(priority_raw, int) and not isinstance(priority_raw, bool):
            if priority_raw not in PRIORITY_NAMES:
                raise CaptureError(
                    TARGET_UNSAFE_PRIORITY,
                    "priority integer %r is not a valid OBS window_priority" % (priority_raw,),
                    {"valid": PRIORITY_NAMES},
                )
            priority = priority_raw
        elif isinstance(priority_raw, str):
            key = priority_raw.strip().lower()
            if key not in PRIORITY_BY_NAME:
                raise CaptureError(
                    TARGET_UNSAFE_PRIORITY,
                    "priority %r is not one of title|class|exe" % (priority_raw,),
                    {"valid": sorted(PRIORITY_BY_NAME)},
                )
            priority = PRIORITY_BY_NAME[key]
        else:
            raise CaptureError(TARGET_UNSAFE_PRIORITY, "priority must be a string or integer")

        return cls(
            title=str(raw.get("title") or ""),
            window_class=str(raw.get("class") or ""),
            exe=str(raw.get("exe") or ""),
            priority=priority,
        )

    @property
    def priority_name(self) -> str:
        return PRIORITY_NAMES[self.priority]

    def to_obs_window_string(self) -> str:
        return build_window_string(self.title, self.window_class, self.exe)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "class": self.window_class,
            "exe": self.exe,
            "priority": self.priority_name,
            "priority_value": self.priority,
            "window_string": self.to_obs_window_string(),
        }

    # -- safety -------------------------------------------------------------
    def match_risk(self) -> str:
        """How exposed this target is to being taken over by another window."""
        if self.priority == PRIORITY_TITLE:
            return "title_equality_only__same_title_window_can_take_over"
        if self.priority == PRIORITY_CLASS:
            return "class_then_title__same_class_window_can_take_over"
        # EXE priority still matches by exe equality first, then closest title.
        return "exe_equality_then_closest_title__another_window_of_same_exe_can_take_over"

    def validate(self, *, allow_partial_match: bool = False) -> List[str]:
        """Return refusal reasons; empty list means the target may be used."""
        problems: List[str] = []

        if not (self.title or self.window_class or self.exe):
            raise CaptureError(
                TARGET_EMPTY,
                "target has no title, class or exe; refusing to match an arbitrary window",
            )

        if self.priority == PRIORITY_TITLE and not self.title:
            problems.append("priority=title requires a non-empty title")
        if self.priority == PRIORITY_CLASS and not self.window_class:
            problems.append("priority=class requires a non-empty class")
        if self.priority == PRIORITY_EXE and not self.exe:
            problems.append("priority=exe requires a non-empty exe")

        if self.priority == PRIORITY_CLASS and is_generic_class(self.window_class):
            problems.append(
                "priority=class with generic class %r is silently degraded to title "
                "matching by OBS; pick an explicit title or exe priority instead"
                % (self.window_class,)
            )

        if not allow_partial_match:
            missing = [
                name
                for name, value in (
                    ("title", self.title),
                    ("class", self.window_class),
                    ("exe", self.exe),
                )
                if not value
            ]
            if missing:
                problems.append(
                    "target is missing %s; a partial match is not an explicit window "
                    "selection (pass allow_partial_match=true to accept the risk)"
                    % (", ".join(missing),)
                )

        if problems:
            raise CaptureError(
                TARGET_UNSAFE_PRIORITY if any("priority" in p or "degraded" in p for p in problems)
                else TARGET_AMBIGUOUS,
                "; ".join(problems),
                {"target": self.to_dict(), "problems": problems},
            )
        return problems


def redact_value(value: str) -> str:
    """Stable, non-reversible fingerprint used instead of raw window text."""
    import hashlib

    digest = hashlib.sha256((value or "").encode("utf-8", "replace")).hexdigest()
    return "sha256:" + digest[:12]


def redact_window_item(item: Dict[str, Any], *, revealed: bool = False) -> Dict[str, Any]:
    """Redact a window list entry so reports do not leak other apps.

    ``GetInputPropertiesListPropertyItems(propertyName="window")`` returns every
    capturable window on the machine, including titles and executables of
    unrelated applications.  Reports default to fingerprints only; the raw
    values are only emitted for the one target the caller explicitly selected
    (or when ``reveal_window_details`` is set).
    """
    name = str(item.get("itemName") or "")
    value = str(item.get("itemValue") or "")
    title, window_class, exe = parse_window_string(value)
    out: Dict[str, Any] = {
        "title_fingerprint": redact_value(title or name),
        "class_fingerprint": redact_value(window_class),
        "exe_fingerprint": redact_value(exe),
        "item_enabled": bool(item.get("itemEnabled", True)),
        "redacted": not revealed,
    }
    if revealed:
        out.update(
            {
                "item_name": name,
                "title": title,
                "class": window_class,
                "exe": exe,
                "window_string": value,
            }
        )
    return out
