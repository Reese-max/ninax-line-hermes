"""Regression checks for the fixed A01-J05 50-persona audit reports (issue #5)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / ".github" / "quality-audits"
REPORT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{4})Z-50-persona-audit-round-(\d+)\.md$")
PERSONAS = [f"{group}{index:02d}" for group in "ABCDEFGHIJ" for index in range(1, 6)]
PRODUCT_SHA = "e2c4785fbb222ddd1a2d54b6eb5ae3058418f4e6"
PROTOCOL_BLOB = "6e3499d6ef5be7e123050e1526946f6a40f99263"
QUALITY_BLOB = "8167e10798071d2276addaff6b201c6b0e904a2a"


def _reports():
    found = {}
    for path in sorted(AUDIT_DIR.glob("*-50-persona-audit-round-*.md")):
        match = REPORT_RE.match(path.name)
        assert match, f"audit report filename does not follow the convention: {path.name}"
        found[int(match.group(5))] = (match, path)
    return found


def test_round_three_report_exists():
    reports = _reports()
    assert sorted(reports) == list(range(1, len(reports) + 1)), "round numbering must be contiguous"
    assert 3 in reports, "fixed 50-persona round-3 audit report is missing"


def test_every_round_covers_all_50_personas():
    for number, (_, path) in _reports().items():
        rows = re.findall(r"^\|\s*([A-J]\d{2})\s*\|", path.read_text(encoding="utf-8"), re.M)
        assert sorted(rows) == PERSONAS, f"round {number} must exercise each fixed persona exactly once"


def test_round_reports_pin_immutable_inputs():
    for number, (match, path) in _reports().items():
        text = path.read_text(encoding="utf-8")
        assert PROTOCOL_BLOB in text, f"round {number} must pin the fixed-persona protocol blob"
        assert QUALITY_BLOB in text, f"round {number} must pin the Issue Quality v2 blob"
        assert PRODUCT_SHA in text, f"round {number} must record the inspected product SHA"
        assert "-".join(match.groups()[:3]) in text, f"round {number} filename date must appear in the report"


def test_latest_round_records_honest_status_and_open_findings():
    _, (_, latest) = sorted(_reports().items())[-1]
    text = latest.read_text(encoding="utf-8")
    assert "NOT CLEAN" in text, "latest round must not claim CLEAN while applicable P1/P2 remain open"
    assert "0/2" in text, "latest round must record the unqualified CLEAN streak"
    for issue in ("#1", "#4", "#8"):
        assert issue in text, f"latest round must track still-open finding {issue}"
