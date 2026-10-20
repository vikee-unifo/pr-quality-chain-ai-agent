"""Command line: offline/local reviews and the benchmark.

python -m app.cli review --case authorization-bypass
python -m app.cli review --diff my.diff [--mock-script script.json] [--publishable]
python -m app.cli evaluate [--live] [--out results.md] [--db results.db]

--live requires LLM_PROVIDER=litellm, LLM_MODEL and the provider's credentials; otherwise it exits with
status 2 before writing anything. evaluate exits 0 when every review completed, 3 when the evaluation is
partial (some agents failed), and 1 when nothing could be measured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app import report
from app.benchmark import load_case
from app.config import Settings
from app.evaluation import render_markdown, require_live_provider, run_benchmark
from app.llm import ProviderError
from app.llm.connector import build_connector
from app.models import PRInput
from app.observability import configure_logging
from app.store import Store

EXIT_CODES = {"complete": 0, "failed": 1, "partial": 3}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-quality-chain")
    sub = parser.add_subparsers(dest="command", required=True)
    rv = sub.add_parser("review", help="review a diff file or a benchmark case")
    source = rv.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff", type=Path)
    source.add_argument("--case", help="benchmark case name; uses its scripted mock responses unless --live")
    rv.add_argument("--title", default="")
    rv.add_argument("--mock-script", type=Path)
    rv.add_argument("--live", action="store_true", help="use LLM_PROVIDER/LLM_MODEL from the environment")
    rv.add_argument("--publishable", action="store_true", help="show only what would be published")
    rv.add_argument("--json", action="store_true")
    ev = sub.add_parser("evaluate", help="run every benchmark case and score it")
    ev.add_argument("--live", action="store_true", help="use the configured real provider (costs money)")
    ev.add_argument("--out", type=Path)
    ev.add_argument("--db", type=Path, help="SQLite file for results (default: DATABASE_PATH)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.secret_values())  # logs go to stderr, reports to stdout

    if args.live:
        # Checked before any connector, store or review exists: explicit live mode never falls back to the mock.
        try:
            require_live_provider(settings)
            connector = build_connector(settings)  # also rejects unknown providers
            check_credentials = getattr(connector.provider, "check_credentials", None)
            if check_credentials:  # offline: reports missing variable names, never values
                check_credentials(settings.llm_model)
        except (ValueError, ProviderError, ImportError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.command == "evaluate":
        store = Store(str(args.db) if args.db else settings.database_path)
        rows, summary = asyncio.run(run_benchmark(args.live, settings, store=store))
        text = render_markdown(rows, summary)
        if args.out:
            args.out.write_text(
                text + "\n\n```json\n" + json.dumps({"rows": rows, "summary": summary}, indent=2) + "\n```\n"
            )
        print(text)
        return EXIT_CODES[summary["evaluation_status"]]

    if args.case:
        pr, _, case_dir = load_case(args.case)
        script = None if args.live else (args.mock_script or case_dir / "mock_responses.json")
    else:
        pr = PRInput(title=args.title, diff=args.diff.read_text(encoding="utf-8"))
        script = args.mock_script
    if not args.live:
        settings = settings.model_copy(
            update={
                "llm_provider": "mock",
                "llm_model": "mock-model",
                "mock_script_path": str(script) if script else None,
            }
        )
        if script is None:
            print("note: mock provider without a script returns no findings", file=sys.stderr)

    from app.orchestrator import run_review

    result = asyncio.run(run_review(pr, build_connector(settings), settings))
    print(result.model_dump_json(indent=2) if args.json else report.render(result, publishable=args.publishable))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
