from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import ValidationError

from .models import Dataset
from .scoring import compare


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Paired testing evaluation; no model calls are made."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--smoke",
        action="store_true",
        help="Run local scripted browser/MCP controls, not an agent benchmark",
    )
    source.add_argument(
        "--compare",
        type=Path,
        help="Score an externally collected, independently adjudicated dataset",
    )
    parser.add_argument(
        "--output", type=Path, help="Save the validated dataset for reproducible rescoring"
    )
    args = parser.parse_args()
    try:
        if args.smoke:
            from .smoke import run_smoke

            dataset = asyncio.run(run_smoke())
        else:
            if args.compare.stat().st_size > 8_000_000:
                raise ValueError("dataset exceeds the 8 MB limit")
            dataset = Dataset.model_validate_json(args.compare.read_text())
        if args.output:
            from argus.storage import atomic_write_text

            atomic_write_text(args.output, dataset.model_dump_json(indent=2))
        comparison = compare(dataset)
        print(json.dumps(comparison, indent=2, allow_nan=False))
        if args.smoke and any(
            arm[key]
            for arm in comparison["arms"].values()
            for key in (
                "false_positives",
                "false_negatives",
                "duplicate_findings",
                "budget_violations",
            )
        ):
            return 1
        return 1 if any(not trial.completed for trial in dataset.trials) else 0
    except (OSError, ValueError, ValidationError) as exc:
        parser.exit(
            2,
            f"Invalid evaluation: {type(exc).__name__}. Check dataset schema, pairs and file access.\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
