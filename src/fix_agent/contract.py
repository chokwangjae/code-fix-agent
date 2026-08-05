from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any

from .errors import FixAgentError


_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEVERITIES = ("Critical", "Major", "Minor")


@dataclass(frozen=True)
class Finding:
    fingerprint: str
    severity: str
    commit: str
    file: str
    line: int
    cause: str
    solution: str


@dataclass(frozen=True)
class ReviewEvent:
    repository: str
    branch: str
    baseline: str
    target: str
    findings: tuple[Finding, ...]


def parse_review_event(raw: Any) -> ReviewEvent:
    if not isinstance(raw, dict):
        raise FixAgentError("review event must be a JSON object")
    if raw.get("version") != 1:
        raise FixAgentError("review event version must be 1")
    baseline = _object_id(raw, "baseline", "review event")
    target = _object_id(raw, "target", "review event")
    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list) or not raw_findings:
        raise FixAgentError("review event findings must be a non-empty array")
    findings = tuple(_finding(value, index) for index, value in enumerate(raw_findings))
    fingerprints = [finding.fingerprint for finding in findings]
    if len(fingerprints) != len(set(fingerprints)):
        raise FixAgentError("review event findings contain duplicate fingerprints")
    return ReviewEvent(
        repository=_string(raw, "repository", "review event"),
        branch=_string(raw, "branch", "review event"),
        baseline=baseline,
        target=target,
        findings=findings,
    )


def _finding(raw: Any, index: int) -> Finding:
    context = f"findings[{index}]"
    if not isinstance(raw, dict):
        raise FixAgentError(f"{context} must be an object")
    fingerprint = _string(raw, "fingerprint", context)
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise FixAgentError(f"{context}.fingerprint must be a sha256 value")
    severity = _string(raw, "severity", context)
    if severity not in _SEVERITIES:
        raise FixAgentError(f"{context}.severity is invalid")
    file = _string(raw, "file", context)
    path = PurePosixPath(file)
    if path.is_absolute() or ".." in path.parts:
        raise FixAgentError(f"{context}.file must be a repository-relative path")
    line = raw.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise FixAgentError(f"{context}.line must be a positive integer")
    return Finding(
        fingerprint=fingerprint,
        severity=severity,
        commit=_object_id(raw, "commit", context),
        file=file,
        line=line,
        cause=_bounded_string(raw, "cause", context, 20_000),
        solution=_bounded_string(raw, "solution", context, 20_000),
    )


def _object_id(raw: dict[str, Any], key: str, context: str) -> str:
    value = _string(raw, key, context).lower()
    if not _OBJECT_ID.fullmatch(value):
        raise FixAgentError(f"{context}.{key} must be a full Git object ID")
    return value


def _bounded_string(raw: dict[str, Any], key: str, context: str, limit: int) -> str:
    value = _string(raw, key, context)
    if len(value) > limit:
        raise FixAgentError(f"{context}.{key} exceeds {limit} characters")
    return value


def _string(raw: dict[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixAgentError(f"{context}.{key} must be a non-empty string")
    return value.strip()
