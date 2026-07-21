"""
sentinel_review/cli.py
========================
CLI entry point — Typer app matching the commands in build.md Task 9.

Commands:
  sentinel review <path>                  # full scan, local repo
  sentinel review <path> --diff <ref>     # diff-aware, vs given base ref
  sentinel review <github-url>            # clones then scans
  sentinel review <path> --auto-fix       # enable confidence-gated auto-apply
  sentinel review <path> --sarif out.sarif
  sentinel eval                           # run the eval harness

Rich progress output driven by AgentState agent_timeline field.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import print as rprint

app = typer.Typer(
    name="sentinel",
    help="sentinel-review: AI-powered code review with LangGraph + Llama 3.3 70B",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s | %(name)s | %(message)s")


def _load_config_and_secrets(repo_path: Path, auto_fix: bool = False):
    """Load config and secrets, exiting with a clear error if Groq key is missing."""
    from sentinel_review.config import load_config
    from sentinel_review.secrets import load_secrets

    config = load_config(repo_path)
    if auto_fix:
        config = config.model_copy(update={"auto_fix": True})

    try:
        secrets = load_secrets()
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1)

    return config, secrets


def _display_findings_table(findings, fixes) -> None:
    """Print a Rich table of findings."""
    if not findings:
        console.print("\n[bold green]✓ No findings![/bold green] Clean code.\n")
        return

    table = Table(
        title=f"sentinel-review: {len(findings)} Finding(s)",
        show_lines=True,
        expand=True,
    )
    table.add_column("Severity", style="bold", width=10)
    table.add_column("Category", width=10)
    table.add_column("Rule", width=18)
    table.add_column("Location", width=30)
    table.add_column("Description")

    severity_style = {
        "critical": "bold red",
        "high": "red",
        "medium": "yellow",
        "low": "dim",
    }

    for f in findings:
        style = severity_style.get(f.severity, "")
        table.add_row(
            f"[{style}]{f.severity.upper()}[/{style}]",
            f.category,
            f.rule_id,
            f"{f.file_path}:{f.start_line}",
            f.description[:80] + ("…" if len(f.description) > 80 else ""),
        )

    console.print(table)

    if fixes:
        console.print(f"\n[bold cyan]Fixes generated:[/bold cyan] {len(fixes)}")
        for fix in fixes:
            confidence_color = "green" if fix.confidence >= 0.85 else "yellow"
            console.print(
                f"  • [{confidence_color}]{fix.finding.rule_id}[/{confidence_color}] "
                f"confidence={fix.confidence:.0%}  {fix.reasoning[:60]}"
            )


@app.command("review")
def review_command(
    target: str = typer.Argument(..., help="Local repo path or GitHub URL to review"),
    diff: Optional[str] = typer.Option(None, "--diff", help="Git ref to diff against (e.g. 'main')"),
    auto_fix: bool = typer.Option(False, "--auto-fix", help="Enable confidence-gated auto-apply of fixes"),
    sarif: Optional[Path] = typer.Option(None, "--sarif", help="Write SARIF output to this file"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write JSON report to this file"),
    staged: bool = typer.Option(False, "--staged", help="Review only git-staged files (for pre-commit hook)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    agents: Optional[str] = typer.Option(None, "--agents", help="Comma-separated agents (bug,security,docs)"),
) -> None:
    """Review a repository or file for bugs, security issues, and documentation problems."""
    _setup_logging(verbose)

    # --- Resolve target ---
    from sentinel_review.ingestion.github_loader import is_github_url, clone_github_repo

    tmp_dir = None
    if is_github_url(target):
        console.print(f"[bold]Cloning[/bold] {target}…")
        tmp_dir = Path(tempfile.mkdtemp(prefix="sentinel_"))
        repo_path, namespace = clone_github_repo(target, tmp_dir)
    else:
        repo_path = Path(target).resolve()
        if not repo_path.exists():
            console.print(f"[bold red]Error:[/bold red] Path not found: {target}")
            raise typer.Exit(1)
        namespace = repo_path.name

    config, secrets = _load_config_and_secrets(repo_path, auto_fix=auto_fix)

    # --staged: only review files staged for commit (pre-commit hook mode)
    if staged and diff is None:
        diff = "HEAD"  # diff against HEAD = show only staged changes

    if agents:
        agent_list = [a.strip() for a in agents.split(",")]
        config = config.model_copy(update={"enabled_agents": agent_list})
    elif staged:
        # Pre-commit: bug + security only (speed over completeness)
        config = config.model_copy(update={"enabled_agents": ["bug", "security"]})

    # --- Parse ---
    from sentinel_review.ingestion.parser import parse_repo
    from sentinel_review.ingestion.diff_selector import get_changed_chunks

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
    ) as progress:
        task = progress.add_task("Parsing repository…", total=None)
        all_chunks = parse_repo(repo_path, ignore_patterns=config.ignore_paths)
        progress.update(task, description=f"Parsed {len(all_chunks)} chunks")

        if diff:
            chunks = get_changed_chunks(repo_path, diff, "HEAD", all_chunks)
            progress.update(task, description=f"Diff-aware: {len(chunks)} chunks selected")
        else:
            chunks = all_chunks

    if not chunks:
        console.print("[yellow]No code chunks found to review.[/yellow]")
        raise typer.Exit(0)

    console.print(
        Panel(
            f"[bold]Reviewing[/bold] {namespace}\n"
            f"Chunks: {len(chunks)} | Agents: {', '.join(config.enabled_agents)} | "
            f"Auto-fix: {'enabled' if config.auto_fix else 'disabled'}",
            title="sentinel-review",
            expand=False,
        )
    )

    # --- Run agents ---
    from sentinel_review.graph.build_graph import run_review

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
    ) as progress:
        task = progress.add_task("Running review agents…", total=None)
        final_state = run_review(
            repo_path=str(repo_path),
            namespace=namespace,
            chunks=chunks,
            config=config,
            groq_api_key=secrets.groq_api_key,
        )
        progress.update(task, description="Review complete")

    findings = final_state.get("deduplicated_findings") or final_state.get("findings", [])
    fixes = final_state.get("fixes", [])
    timeline = final_state.get("agent_timeline", [])

    # --- Display ---
    if verbose and timeline:
        console.print("\n[bold]Agent timeline:[/bold]")
        for event in timeline:
            console.print(f"  › {event}")

    _display_findings_table(findings, fixes)

    # --- Build report ---
    from sentinel_review.report.json_report import build_report, write_report
    from sentinel_review.report.sarif import findings_to_sarif

    report = build_report(
        namespace=namespace,
        findings=findings,
        fixes=fixes,
        sandbox_results=final_state.get("sandbox_results", []),
        review_score=final_state.get("review_score"),
        fix_success_rate=final_state.get("fix_success_rate"),
        config=config,
        agent_timeline=timeline,
    )

    if output:
        write_report(report, output)
        console.print(f"\n[bold]JSON report:[/bold] {output}")

    if sarif:
        sarif_doc = findings_to_sarif(findings)
        with sarif.open("w", encoding="utf-8") as fh:
            json.dump(sarif_doc, fh, indent=2)
        console.print(f"[bold]SARIF:[/bold] {sarif}")

    # Exit code: 1 if any high/critical findings (useful for CI)
    high_or_critical = any(f.severity in ("critical", "high") for f in findings)
    raise typer.Exit(code=1 if high_or_critical else 0)


@app.command("eval")
def eval_command(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the evaluation harness against all fixture repos."""
    _setup_logging(verbose)
    from sentinel_review.eval.harness import run_harness
    run_harness()


def main():
    app()


if __name__ == "__main__":
    main()
