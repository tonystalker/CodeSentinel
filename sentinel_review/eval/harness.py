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

Matching (see skill.md §8): an actual finding matches an expected finding
if rule_id and file_path are equal and their line ranges overlap. Each
expected finding can be matched by at most one actual finding — extra
reports of an already-matched bug are tracked as `duplicate_reports`,
not counted as additional detections and not counted as false positives.
This keeps detection_rate mathematically capped at 100% and keeps a real
signal (the agent is re-reporting the same bug via multiple agents)
visible instead of silently inflating or deflating other numbers.
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


class MatchResult(NamedTuple):
    true_positives: int          # distinct expected findings matched
    false_positives: int         # actual findings matching nothing expected
    duplicate_reports: int       # actual findings re-matching an already-matched expected finding


class HarnessResult(NamedTuple):
    case_name: str
    expected_count: int
    actual_count: int
    true_positives: int
    false_positives: int
    duplicate_reports: int
    fix_passed: bool
    detection_rate: float        # true_positives / expected_count, naturally capped at 1.0
    false_positive_rate: float   # false_positives / actual_count


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


def _match_findings(actual: list, expected: list[ExpectedFinding]) -> MatchResult:
    """Match actual findings against expected findings.

    Each expected finding can be matched at most once. Fixture sets are
    small (single bug pattern each, per skill.md §8) so a greedy
    first-fit match is equivalent to an optimal bipartite match here —
    don't reach for the Hungarian algorithm for a handful of findings.
    """
    matched_expected_idx: set[int] = set()
    false_positives = 0
    duplicate_reports = 0

    def overlaps(f, exp: ExpectedFinding) -> bool:
        return (
            f.file_path == exp.file_path
            and f.rule_id == exp.rule_id
            and f.start_line <= exp.end_line
            and f.end_line >= exp.start_line
        )

    for finding in actual:
        fresh_match_idx = next(
            (i for i, exp in enumerate(expected)
             if i not in matched_expected_idx and overlaps(finding, exp)),
            None,
        )
        if fresh_match_idx is not None:
            matched_expected_idx.add(fresh_match_idx)
            continue

        is_duplicate_of_matched = any(
            overlaps(finding, exp)
            for i, exp in enumerate(expected)
            if i in matched_expected_idx
        )
        if is_duplicate_of_matched:
            duplicate_reports += 1
        else:
            false_positives += 1

    return MatchResult(
        true_positives=len(matched_expected_idx),
        false_positives=false_positives,
        duplicate_reports=duplicate_reports,
    )


def _run_fixture(
    fixture_dir: Path,
    config,
    groq_api_key: str,
    e2b_runner=None,
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
            case_name=case_name, expected_count=len(expected), actual_count=0,
            true_positives=0, false_positives=0, duplicate_reports=0,
            fix_passed=False, detection_rate=0.0, false_positive_rate=0.0,
        )

    try:
        final_state = run_review(
            repo_path=str(fixture_dir),
            namespace=f"eval/{case_name}",
            chunks=chunks,
            config=config,
            groq_api_key=groq_api_key,
            e2b_runner=e2b_runner,
        )
    except Exception as exc:
        logger.error("Pipeline failed for '%s': %s", case_name, exc)
        return HarnessResult(
            case_name=case_name, expected_count=len(expected), actual_count=0,
            true_positives=0, false_positives=0, duplicate_reports=0,
            fix_passed=False, detection_rate=0.0, false_positive_rate=0.0,
        )

    # Explicit, visible fallback — an empty-but-valid dedup list must not
    # silently pull in raw undeduped findings (see skill.md / LOG.md
    # note on the LangGraph fan-out double-counting bug).
    if "deduplicated_findings" in final_state:
        actual_findings = final_state["deduplicated_findings"]
    else:
        logger.warning(
            "'%s': no deduplicated_findings in final_state — reviewer_agent "
            "may not have run. Falling back to raw findings (UNDEDUPED, "
            "counts may be inflated).",
            case_name,
        )
        actual_findings = final_state.get("findings", [])

    match = _match_findings(actual_findings, expected)
    total_actual = len(actual_findings)

    detection_rate = match.true_positives / len(expected) if expected else 0.0
    false_positive_rate = match.false_positives / total_actual if total_actual > 0 else 0.0

    fix_passed = any(sr.passed for sr in final_state.get("sandbox_results", []))

    return HarnessResult(
        case_name=case_name,
        expected_count=len(expected),
        actual_count=total_actual,
        true_positives=match.true_positives,
        false_positives=match.false_positives,
        duplicate_reports=match.duplicate_reports,
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

    e2b_api_key = os.environ.get("E2B_API_KEY", "")
    e2b_key_present = bool(e2b_api_key)

    config = SentinelConfig(auto_fix=e2b_key_present, enabled_agents=["bug", "security"])

    e2b_runner = None
    if e2b_key_present:
        from sentinel_review.sandbox.e2b_runner import E2BRunner
        e2b_runner = E2BRunner(api_key=e2b_api_key, config=config)
    else:
        print(
            "WARNING: E2B_API_KEY not set — fix validation will be skipped "
            "for every fixture, so fix-success-rate will not be measured "
            "in this run.\n"
        )

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
    table.add_column("TP", justify="center")
    table.add_column("FP", justify="center")
    table.add_column("Dup Reports", justify="center")
    table.add_column("Detection Rate", justify="center")
    table.add_column("FP Rate", justify="center")
    table.add_column("Fix Passed", justify="center")

    results: list[HarnessResult] = []

    for fixture_dir in fixture_dirs:
        console.print(f"[bold]Running:[/bold] {fixture_dir.name}…")
        result = _run_fixture(fixture_dir, config, groq_api_key, e2b_runner=e2b_runner)
        results.append(result)

        table.add_row(
            result.case_name,
            str(result.expected_count),
            str(result.actual_count),
            str(result.true_positives),
            str(result.false_positives),
            str(result.duplicate_reports),
            f"{result.detection_rate:.0%}",
            f"{result.false_positive_rate:.0%}",
            "✓" if result.fix_passed else ("skipped" if not e2b_key_present else "✗"),
        )

    total_expected = sum(r.expected_count for r in results)
    total_tp = sum(r.true_positives for r in results)
    total_actual = sum(r.actual_count for r in results)
    total_fp = sum(r.false_positives for r in results)
    total_dupes = sum(r.duplicate_reports for r in results)
    # Fix-success denominator: only fixtures with ≥1 auto_fixable expected finding.
    # Fixtures with no auto_fixable bugs (e.g. race_condition) are excluded so
    # unfixable bugs don't drag down the rate.
    fixable_results = [
        r for r in results
        if any(
            ef.auto_fixable
            for ef in _load_expected(FIXTURES_DIR / r.case_name)
        )
    ]
    fix_pass_rate = (
        sum(1 for r in fixable_results if r.fix_passed) / len(fixable_results)
        if fixable_results and e2b_key_present
        else None
    )

    aggregate_detection = total_tp / total_expected if total_expected else 0.0
    aggregate_fp = total_fp / total_actual if total_actual else 0.0

    table.add_row(
        "[bold]AGGREGATE[/bold]",
        str(total_expected), str(total_actual), str(total_tp), str(total_fp), str(total_dupes),
        f"[bold]{aggregate_detection:.0%}[/bold]",
        f"[bold]{aggregate_fp:.0%}[/bold]",
        "-",
    )

    console.print(table)
    console.print(
        f"\n[bold green]Headline metrics:[/bold green]\n"
        f"  Detection rate:      {aggregate_detection:.1%}  ({total_tp}/{total_expected} known bugs found)\n"
        f"  False-positive rate: {aggregate_fp:.1%}  ({total_fp}/{total_actual} findings unmatched)\n"
        f"  Duplicate reports:   {total_dupes}  (same bug reported >1x — check agent fan-out dedup if nonzero)\n"
        + (
            f"  Fix success rate:    {fix_pass_rate:.1%}\n"
            if fix_pass_rate is not None
            else "  Fix success rate:    not measured (E2B_API_KEY not set)\n"
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    run_harness()
