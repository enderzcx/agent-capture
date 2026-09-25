"""Small CLI adapter: one command, one explicit JSON config object, one JSON result.

    python -m agent_capture_win preflight --config cfg.json
    echo '{"run_id":"take-001"}' | python -m agent_capture_win capabilities --config -

Exit codes: 0 = ok, 2 = refused/failed (the JSON body still explains why),
3 = usage error (bad JSON, unknown command).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .api import COMMANDS, run

USAGE_ERROR = 3


def _load_config(source: str) -> Dict[str, Any]:
    if source == "-":
        raw = sys.stdin.read()
    else:
        with open(source, "r", encoding="utf-8") as handle:
            raw = handle.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("config must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_capture_win",
        description="Windows window recording backend over obs-websocket 5.x",
    )
    parser.add_argument("command", choices=list(COMMANDS))
    parser.add_argument(
        "--config",
        default="-",
        help="path to a JSON config object, or '-' for stdin (default)",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indent (0 for compact)")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": {"category": "CONFIG_INVALID", "message": str(exc)}}, indent=2))
        return USAGE_ERROR

    result = run(args.command, config)
    indent = args.indent if args.indent and args.indent > 0 else None
    print(json.dumps(result, indent=indent, ensure_ascii=False, sort_keys=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
