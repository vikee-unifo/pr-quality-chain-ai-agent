"""Loads benchmark cases from benchmark/cases/<name>/ (see benchmark/README.md)."""

from __future__ import annotations

import json
from pathlib import Path

from app.models import PRInput

CASES_DIR = Path(__file__).resolve().parent.parent / "benchmark" / "cases"


def _read_tree(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()
    }


def load_case(name: str, cases_dir: Path = CASES_DIR) -> tuple[PRInput, dict, Path]:
    """Returns (PR input, expected labels, case directory)."""
    case_dir = cases_dir / name
    meta = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    pr = PRInput(
        title=meta.get("title", ""),
        description=meta.get("description", ""),
        diff=(case_dir / "pr.diff").read_text(encoding="utf-8"),
        repo=meta.get("repo"),
        head_sha=meta.get("head_sha"),
        file_contents=_read_tree(case_dir / "files"),
        context_files=_read_tree(case_dir / "context"),
        tool_results=meta.get("tool_results", []),
    )
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    return pr, expected, case_dir


def list_cases(cases_dir: Path = CASES_DIR) -> list[str]:
    return sorted(p.name for p in cases_dir.iterdir() if (p / "case.json").is_file())
