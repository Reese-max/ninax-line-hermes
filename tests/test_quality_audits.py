"""Regression checks for the fixed A01-J05 50-persona audit reports (issue #5)."""
import re
from pathlib import Path

import pytest

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
        number = int(match.group(5))
        assert number not in found, f"duplicate audit round {number}: {found[number][1].name}, {path.name}"
        found[number] = (match, path)
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
        product = re.search(r"^- Inspected(?:/default)? product SHA: `([0-9a-f]{40})`$", text, re.M)
        assert product, f"round {number} must record the inspected product SHA"
        if number <= 3:
            assert product.group(1) == PRODUCT_SHA, f"historical round {number} must preserve its product SHA"
        assert "-".join(match.groups()[:3]) in text, f"round {number} filename date must appear in the report"


def test_round_three_records_honest_status_and_open_findings():
    _, report = _reports()[3]
    text = report.read_text(encoding="utf-8")
    assert "NOT CLEAN" in text, "round 3 must not claim CLEAN while its applicable P1/P2 remain open"
    assert "0/2" in text, "round 3 must preserve its unqualified CLEAN streak"
    for issue in ("#1", "#4", "#8"):
        assert issue in text, f"round 3 must track its still-open finding {issue}"


def test_duplicate_round_numbers_are_rejected(tmp_path, monkeypatch):
    for date in ("2026-09-01-0000Z", "2026-09-02-0000Z"):
        (tmp_path / f"{date}-50-persona-audit-round-1.md").write_text("", encoding="utf-8")
    monkeypatch.setitem(globals(), "AUDIT_DIR", tmp_path)
    with pytest.raises(AssertionError, match="duplicate.*round"):
        _reports()


@pytest.mark.parametrize("check", [
    test_round_reports_pin_immutable_inputs,
    test_round_three_records_honest_status_and_open_findings,
])
def test_future_round_can_record_a_new_product_and_clean_status(tmp_path, monkeypatch, check):
    for _, path in _reports().values():
        (tmp_path / path.name).write_bytes(path.read_bytes())
    future = "\n".join([
        "# Fixed 50-Persona Audit — Round 4 — 2026-10-18",
        f"- Inspected product SHA: `{'a' * 40}`",
        f"- Fixed-persona protocol blob: `{PROTOCOL_BLOB}`",
        f"- Issue Quality v2 blob: `{QUALITY_BLOB}`",
        "**CLEAN — streak 1/2.**",
        *(f"| {persona} | verified synthetic scenario |" for persona in PERSONAS),
    ])
    (tmp_path / "2026-10-18-0000Z-50-persona-audit-round-4.md").write_text(future, encoding="utf-8")
    monkeypatch.setitem(globals(), "AUDIT_DIR", tmp_path)
    check()
