"""
sentinel_review/eval/harness.py
==================================
Evaluation harness — runs the full pipeline against fixture repos and
reports detection rate, false-positive rate, and fix-success-rate.

Run with: python -m sentinel_review.eval.harness

Fixture structure:
  sentinel_review/eval/fixtures/<case_name>/
    *.py           # source files with injected bugs
    expected.yml   # expected findings: rule_id, file, line range, auto_fixable

Each fixture is small and single-purpose (one bug pattern per skill.md §8).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class ExpectedFinding(NamedTuple):
    rule_id: str
    file_path: str
    start_line: int
    end_line: int
    auto_fixable: bool = False


class HarnessResult(NamedTuple):
    case_name: str
    expected: list[ExpectedFinding]
    actual_rule_ids: list[str]
    fix_passed: bool
    detection_rate: float
    false_positive_rate: float


def _load_expected(fixture_dir: Path) -> list[ExpectedFinding]:
    """Load expected findings from expected.yml."""
    expected_file = fixture_dir / "expected.yml"
    if not expected_file.exists():
        raise FileNotFoundError(f"Missing expected.yml in '{fixture_dir}'")

    with expected_file.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    findings = []
    for item in data.get("findings", []):
        findings.append(ExpectedFinding(
            rule_id=item["rule_id"],
            file_path=item["file"],
            start_line=item.get("start_line", 1),
            end_line=item.get("end_line", 1),
            auto_fixable=item.get("auto_fixable", False),
        ))
    return findings


def _run_fixture(
    fixture_dir: Path,
    config,
    groq_api_key: str,
) -> HarnessResult:
    """Run the full pipeline on one fixture and compare against expected.yml."""
    from sentinel_review.ingestion.parser import parse_repo
    from sentinel_review.graph.build_graph import run_review

    case_name = fixture_dir.name
    expected = _load_expected(fixture_dir)

    chunks = parse_repo(fixture_dir, ignore_patterns=["expected.yml"])

    if not chunks:
        logger.warning("No chunks parsed from '%s'", fixture_dir)
        return HarnessResult(
            case_name=case_name,
            expected=expected,
            actual_rule_ids=[],
            fix_passed=False,
            detection_rate=0.0,
            false_positive_rate=0.0,
        )

    # Run the pipeline (no E2B in harness — fix_success_rate is 0 without E2B key)
    try:
        final_state = run_review(
            repo_path=str(fixture_dir),
            namespace=f"eval/{case_name}",
            chunks=chunks,
            config=config,
            groq_api_key=groq_api_key,
        )
    except Exception as exc:
        logger.error("Pipeline failed for '%s': %s", case_name, exc)
        return HarnessResult(
            case_name=case_name,
            expected=expected,
            actual_rule_ids=[],
            fix_passed=False,
            detection_rate=0.0,
            false_positive_rate=0.0,
        )

    actual_findings = final_state.get("deduplicated_findings") or final_state.get("findings", [])
    actual_rule_ids = [f.rule_id for f in actual_findings]

    # Detection rate: expected findings actually found
    expected_rule_ids = {e.rule_id for e in expected}
    found_expected = sum(1 for a in actual_rule_ids if a in expected_rule_ids)
    detection_rate = found_expected / len(expected) if expected else 0.0

    # False positive rate: findings not in expected
    unexpected = sum(1 for a in actual_rule_ids if a not in expected_rule_ids)
    total_actual = len(actual_findings)
    false_positive_rate = unexpected / total_actual if total_actual > 0 else 0.0

    # Fix success: any sandbox result passed?
    fix_passed = any(
        sr.passed for sr in final_state.get("sandbox_results", [])
    )

    return HarnessResult(
        case_name=case_name,
        expected=expected,
        actual_rule_ids=actual_rule_ids,
        fix_passed=fix_passed,
        detection_rate=detection_rate,
        false_positive_rate=false_positive_rate,
    )


def run_harness() -> None:
    """Run all fixtures and print the summary table."""
    from rich.console import Console
    from rich.table import Table

    from sentinel_review.config import SentinelConfig

    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_api_key:
        print("ERROR: GROQ_API_KEY not set. Harness requires a Groq key.")
        sys.exit(1)

    config = SentinelConfig(auto_fix=False, enabled_agents=["bug", "security"])

    fixture_dirs = sorted(
        d for d in FIXTURES_DIR.iterdir() if d.is_dir() and (d / "expected.yml").exists()
    )

    if not fixture_dirs:
        print(f"No fixtures found in {FIXTURES_DIR}")
        sys.exit(1)

    console = Console()
    table = Table(title="sentinel-review Eval Harness Results", show_lines=True)
    table.add_column("Fixture", style="cyan")
    table.add_column("Expected", justify="center")
    table.add_column("Found", justify="center")
    table.add_column("Detection Rate", justify="center")
    table.add_column("FP Rate", justify="center")
    table.add_column("Fix Passed", justify="center")

    aggregate_detection = []
    aggregate_fp = []

    for fixture_dir in fixture_dirs:
        console.print(f"[bold]Running:[/bold] {fixture_dir.name}…")
        result = _run_fixture(fixture_dir, config, groq_api_key)

        table.add_row(
            result.case_name,
            str(len(result.expected)),
            str(len(result.actual_rule_ids)),
            f"{result.detection_rate:.0%}",
            f"{result.false_positive_rate:.0%}",
            "✓" if result.fix_passed else "–",
        )
        aggregate_detection.append(result.detection_rate)
        aggregate_fp.append(result.false_positive_rate)

    # Summary row
    avg_detection = sum(aggregate_detection) / len(aggregate_detection) if aggregate_detection else 0
    avg_fp = sum(aggregate_fp) / len(aggregate_fp) if aggregate_fp else 0
    table.add_row(
        "[bold]AGGREGATE[/bold]",
        "–",
        "–",
        f"[bold]{avg_detection:.0%}[/bold]",
        f"[bold]{avg_fp:.0%}[/bold]",
        "–",
    )

    console.print(table)
    console.print(
        f"\n[bold green]Headline metrics:[/bold green]\n"
        f"  Detection rate:    {avg_detection:.1%}\n"
        f"  False-positive rate: {avg_fp:.1%}\n"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run_harness()
