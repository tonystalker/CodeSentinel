"""
sentinel_review/report/sarif.py
=================================
SARIF 2.1.0 output generation (skill.md section 7).

Pure function — no LLM calls, no I/O. Easy to unit test against schema.

Mapping:
  Finding.rule_id   → results[].ruleId AND runs[].tool.driver.rules[].id
  Finding.severity  → results[].level: critical/high→"error", medium→"warning", low→"note"
  file_path/lines   → results[].locations[].physicalLocation

Entry point: findings_to_sarif(findings: list[Finding]) -> dict
"""
from __future__ import annotations

from sentinel_review.graph.schemas import Finding

# SARIF severity mapping (SARIF only has 3 levels)
_SARIF_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}

# Human-readable descriptions for rule IDs.
# Build from findings at report time; supplement with these well-known ones.
_KNOWN_RULE_DESCRIPTIONS: dict[str, str] = {
    "null-deref": "Null or None dereference — accessing an attribute or index on a value that may be None.",
    "missing-import": "Missing import statement — module or name used without being imported.",
    "sql-injection": "SQL injection — user-controlled input concatenated into a SQL query without parameterization.",
    "command-injection": "Command injection — unsanitized input passed to shell execution functions.",
    "path-traversal": "Path traversal — user-controlled path component allows access outside the intended directory.",
    "hardcoded-secret": "Hardcoded credential — API key, password, or secret embedded directly in source code.",
    "off-by-one": "Off-by-one error — incorrect loop bound, slice, or index comparison.",
    "unhandled-exception": "Unhandled exception — exception type raised but not caught in any reachable handler.",
    "type-mismatch": "Type mismatch — value of unexpected type passed to or returned from a function.",
    "resource-leak": "Resource leak — file, socket, or connection opened without guaranteed close.",
    "weak-crypto": "Weak cryptography — use of deprecated or broken algorithm (MD5, SHA1, DES, etc.).",
    "missing-auth": "Missing authentication check — protected endpoint or resource accessible without auth.",
    "race-condition": "Race condition — shared mutable state accessed concurrently without synchronization.",
    "insecure-deserial": "Insecure deserialization — untrusted data deserialized without validation.",
    "missing-docstring": "Missing docstring — public function or class has no documentation string.",
    "incorrect-docstring": "Incorrect docstring — documentation describes wrong behavior, types, or parameters.",
    "empty-docstring": "Empty docstring — documentation string contains only whitespace or placeholder text.",
}

SENTINEL_TOOL_VERSION = "0.1.0"
SENTINEL_TOOL_NAME = "sentinel-review"
SENTINEL_TOOL_URI = "https://github.com/sentinel-review/sentinel-review"


def _get_rule_description(rule_id: str, description: str) -> str:
    """Return the best description for a rule."""
    return _KNOWN_RULE_DESCRIPTIONS.get(rule_id, description)


def findings_to_sarif(findings: list[Finding]) -> dict:
    """Convert a list of Findings to a SARIF 2.1.0 document.

    Args:
        findings: Findings from the reviewer agent (deduplicated).

    Returns:
        A dict ready for json.dumps() that validates against SARIF 2.1.0 schema.
    """
    # Build rule registry from findings (each unique rule_id → one entry)
    rule_registry: dict[str, dict] = {}
    for f in findings:
        if f.rule_id not in rule_registry:
            rule_registry[f.rule_id] = {
                "id": f.rule_id,
                "name": _rule_id_to_name(f.rule_id),
                "shortDescription": {
                    "text": _get_rule_description(f.rule_id, f.description)
                },
                "fullDescription": {
                    "text": _get_rule_description(f.rule_id, f.description)
                },
                "defaultConfiguration": {
                    "level": _SARIF_LEVEL.get(f.severity, "warning")
                },
                "helpUri": f"{SENTINEL_TOOL_URI}#rule-{f.rule_id}",
            }

    # Build results
    results = []
    for f in findings:
        result = {
            "ruleId": f.rule_id,
            "level": _SARIF_LEVEL.get(f.severity, "warning"),
            "message": {
                "text": f.description
            },
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": f.file_path,
                            "uriBaseId": "%SRCROOT%",
                        },
                        "region": {
                            "startLine": f.start_line,
                            "endLine": f.end_line,
                        },
                    }
                }
            ],
            "properties": {
                "severity": f.severity,
                "category": f.category,
            },
        }
        results.append(result)

    sarif_doc = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": SENTINEL_TOOL_NAME,
                        "version": SENTINEL_TOOL_VERSION,
                        "informationUri": SENTINEL_TOOL_URI,
                        "rules": list(rule_registry.values()),
                    }
                },
                "results": results,
                "artifacts": _build_artifact_list(findings),
            }
        ],
    }
    return sarif_doc


def _rule_id_to_name(rule_id: str) -> str:
    """Convert 'sql-injection' → 'SqlInjection' (PascalCase for SARIF ruleName)."""
    return "".join(word.capitalize() for word in rule_id.split("-"))


def _build_artifact_list(findings: list[Finding]) -> list[dict]:
    """Build the artifacts array (unique file paths referenced in findings)."""
    seen: dict[str, dict] = {}
    for f in findings:
        if f.file_path not in seen:
            seen[f.file_path] = {
                "location": {
                    "uri": f.file_path,
                    "uriBaseId": "%SRCROOT%",
                }
            }
    return list(seen.values())
