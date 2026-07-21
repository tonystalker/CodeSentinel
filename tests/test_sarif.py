"""
tests/test_sarif.py
====================
Task 7 acceptance tests for SARIF output.

Done-when criteria (build.md Task 7):
  ✓ findings_to_sarif() produces valid SARIF 2.1.0 structure
  ✓ rule_id → ruleId mapping correct
  ✓ severity → SARIF level mapping correct (critical/high=error, medium=warning, low=note)
  ✓ All rule_ids appear in runs[].tool.driver.rules[]
"""
from __future__ import annotations

import json

import pytest

from sentinel_review.graph.schemas import Finding
from sentinel_review.report.sarif import findings_to_sarif


def make_finding(
    rule_id: str = "null-deref",
    severity: str = "high",
    category: str = "bug",
    file_path: str = "src/main.py",
    start_line: int = 10,
    end_line: int = 12,
) -> Finding:
    return Finding(
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        severity=severity,
        category=category,
        description=f"Test finding for {rule_id}",
        rule_id=rule_id,
    )


class TestFindingsToSarif:
    def test_top_level_structure(self) -> None:
        """SARIF doc must have version, $schema, and runs array."""
        doc = findings_to_sarif([make_finding()])
        assert doc["version"] == "2.1.0"
        assert "$schema" in doc
        assert "runs" in doc
        assert len(doc["runs"]) == 1

    def test_tool_driver_present(self) -> None:
        doc = findings_to_sarif([])
        driver = doc["runs"][0]["tool"]["driver"]
        assert driver["name"] == "sentinel-review"
        assert "version" in driver
        assert "rules" in driver

    def test_empty_findings_produces_valid_sarif(self) -> None:
        doc = findings_to_sarif([])
        assert doc["runs"][0]["results"] == []
        assert doc["runs"][0]["tool"]["driver"]["rules"] == []

    def test_rule_id_appears_in_results_and_rules(self) -> None:
        """Each finding's rule_id must appear in both results[] and rules[]."""
        findings = [
            make_finding("sql-injection", "critical"),
            make_finding("missing-import", "medium"),
        ]
        doc = findings_to_sarif(findings)
        run = doc["runs"][0]

        result_rule_ids = {r["ruleId"] for r in run["results"]}
        registry_rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}

        assert result_rule_ids == {"sql-injection", "missing-import"}
        assert result_rule_ids == registry_rule_ids

    def test_severity_to_sarif_level_mapping(self) -> None:
        """critical/high → error, medium → warning, low → note."""
        mapping_checks = [
            ("critical", "error"),
            ("high", "error"),
            ("medium", "warning"),
            ("low", "note"),
        ]
        for severity, expected_level in mapping_checks:
            findings = [make_finding(f"test-{severity}", severity=severity)]
            doc = findings_to_sarif(findings)
            result_level = doc["runs"][0]["results"][0]["level"]
            assert result_level == expected_level, (
                f"severity='{severity}' should map to level='{expected_level}', got '{result_level}'"
            )

    def test_physical_location_has_correct_lines(self) -> None:
        """Findings must map file_path and line numbers into physicalLocation."""
        finding = make_finding(start_line=42, end_line=45, file_path="src/db.py")
        doc = findings_to_sarif([finding])
        loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "src/db.py"
        assert loc["region"]["startLine"] == 42
        assert loc["region"]["endLine"] == 45

    def test_deduplication_of_rules(self) -> None:
        """Same rule_id appearing in multiple findings → only one entry in rules[]."""
        findings = [
            make_finding("null-deref", start_line=1),
            make_finding("null-deref", start_line=20),
            make_finding("null-deref", start_line=40),
        ]
        doc = findings_to_sarif(findings)
        rule_ids = [r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]]
        assert rule_ids.count("null-deref") == 1

    def test_output_is_json_serialisable(self) -> None:
        """findings_to_sarif() output must be JSON-serialisable without error."""
        findings = [make_finding(severity=s) for s in ["critical", "high", "medium", "low"]]
        doc = findings_to_sarif(findings)
        serialised = json.dumps(doc)
        assert len(serialised) > 0

    def test_artifacts_list(self) -> None:
        """Artifacts array must list unique files referenced in findings."""
        findings = [
            make_finding(file_path="a.py"),
            make_finding(file_path="a.py"),  # duplicate
            make_finding(file_path="b.py"),
        ]
        doc = findings_to_sarif(findings)
        artifact_uris = {a["location"]["uri"] for a in doc["runs"][0]["artifacts"]}
        assert artifact_uris == {"a.py", "b.py"}
