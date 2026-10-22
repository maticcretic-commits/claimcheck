"""Tests for ClaimCheck claim extraction, binding, number
verification, manifest integrity/freshness, scoring, and the CLI."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from claimcheck import (audit_report, compute_score, compute_verdict,
                        extract_claims, finding)

CLI = Path(__file__).parent.parent / "src" / "claimcheck.py"
EXAMPLES = Path(__file__).parent.parent / "examples"


def run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True, text=True, cwd=cwd)


def claims_of(text):
    return extract_claims([(1, text)])


def make_case(tmp_path, report_text, evidence_files=None,
              manifest=None):
    """Write a report + evidence dir under tmp_path; return paths."""
    ev = tmp_path / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    for name, content in (evidence_files or {}).items():
        dest = ev / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    if manifest is not None:
        entries = {}
        for name, recorded in manifest.items():
            target = ev / name
            entries[name] = {
                "sha256": hashlib.sha256(
                    target.read_bytes()).hexdigest(),
                "recorded_at": recorded,
            }
        (ev / "evidence-manifest.json").write_text(
            json.dumps({"files": entries}), encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text(report_text, encoding="utf-8")
    return report, ev


def findings_of(result, check):
    return [f for f in result["findings"] if f["check"] == check]


PYTEST_LOG = "============================== 37 passed in 0.42s "\
             "==============================\n"
COUNTS = json.dumps({"tests_passed": 37, "tests_total": 37,
                     "files_changed": 3})


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

def test_extract_count_claim():
    claims = claims_of("All 38 tests passed.")
    numeric = [c for c in claims if c["value"] is not None]
    assert len(numeric) == 1
    assert numeric[0]["value"] == 38
    assert numeric[0]["unit"] == "tests"


def test_extract_files_changed_claim():
    claims = claims_of("The fix touched 3 files changed in review.")
    numeric = [c for c in claims if c["value"] is not None]
    assert any(c["value"] == 3 and c["unit"] == "files"
               for c in numeric)


def test_extract_fraction_claim():
    claims = claims_of("Suite result: 38/38 tests passed.")
    frac = [c for c in claims if c["denominator"] is not None]
    assert len(frac) == 1
    assert frac[0]["value"] == 38 and frac[0]["denominator"] == 38


def test_extract_percent_claim():
    claims = claims_of("Coverage reached 92% coverage this run.")
    pct = [c for c in claims if "percent" in c["kinds"]]
    assert len(pct) == 1 and pct[0]["value"] == 92.0


def test_extract_money_claim():
    claims = claims_of("The run saved $1,200 in review time.")
    money = [c for c in claims if "money" in c["kinds"]]
    assert len(money) == 1 and money[0]["value"] == 1200.0


def test_extract_completion_claim():
    claims = claims_of("The dashboard fix is deployed.")
    assert any("completion" in c["kinds"] and c["value"] is None
               for c in claims)


def test_plain_prose_has_no_claims():
    assert claims_of("The weather in Patna was pleasant.") == []


def test_dates_and_urls_are_not_claims():
    claims = claims_of(
        "Released on 2026-10-20, notes at https://example.org/a/b "
        "and mirror https://example.org/x/37/37 for readers.")
    assert [c for c in claims if c["value"] is not None] == []


def test_fraction_is_not_a_file_reference(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Suite result: 37/37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG})
    result = audit_report(report, ev)
    assert findings_of(result, "missing-artifact") == []
    assert result["summary"]["verified"] == 1


# ---------------------------------------------------------------------------
# Binding rules
# ---------------------------------------------------------------------------

def test_unbacked_numeric_claim_fires(tmp_path):
    report, ev = make_case(tmp_path, "We ran 42 tests today.\n")
    result = audit_report(report, ev)
    unbacked = findings_of(result, "unbacked-claim")
    assert len(unbacked) == 1
    assert unbacked[0]["severity"] == "high"
    assert result["verdict"] == "FAIL"


def test_unbacked_completion_claim_fires(tmp_path):
    report, ev = make_case(
        tmp_path, "The new exporter is shipped and live.\n")
    result = audit_report(report, ev)
    assert len(findings_of(result, "unbacked-claim")) == 1


def test_reference_on_continuation_line_binds(tmp_path):
    report, ev = make_case(
        tmp_path,
        "- 37 tests passed in the nightly run\n"
        "  Evidence: `evidence/run.log`\n",
        {"run.log": PYTEST_LOG})
    result = audit_report(report, ev)
    assert findings_of(result, "unbacked-claim") == []
    assert result["summary"]["verified"] == 1


def test_reference_in_other_bullet_does_not_bind(tmp_path):
    report, ev = make_case(
        tmp_path,
        "- 37 tests passed in the nightly run\n"
        "- Raw output kept in `evidence/run.log`\n",
        {"run.log": PYTEST_LOG})
    result = audit_report(report, ev)
    assert len(findings_of(result, "unbacked-claim")) == 1


def test_fenced_code_is_not_audited(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Quoted sample report:\n\n```\n99 tests passed\n```\n")
    result = audit_report(report, ev)
    assert result["summary"]["claims"] == 0
    assert result["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# Missing artifacts
# ---------------------------------------------------------------------------

def test_missing_referenced_artifact_fires(tmp_path):
    report, ev = make_case(
        tmp_path, "Details in `evidence/nope.json` for review.\n")
    result = audit_report(report, ev)
    missing = findings_of(result, "missing-artifact")
    assert len(missing) == 1 and missing[0]["severity"] == "high"


def test_existing_reference_is_quiet(tmp_path):
    report, ev = make_case(
        tmp_path, "Details in `evidence/counts.json` for review.\n",
        {"counts.json": COUNTS})
    result = audit_report(report, ev)
    assert findings_of(result, "missing-artifact") == []


# ---------------------------------------------------------------------------
# Number verification
# ---------------------------------------------------------------------------

def test_pytest_log_number_verified(tmp_path):
    report, ev = make_case(
        tmp_path, "37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1
    assert result["verdict"] == "PASS"


def test_pytest_log_number_mismatch(tmp_path):
    report, ev = make_case(
        tmp_path, "120 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG})
    result = audit_report(report, ev)
    mm = findings_of(result, "number-mismatch")
    assert len(mm) == 1 and mm[0]["severity"] == "high"
    assert result["summary"]["mismatch"] == 1


def test_fraction_verified_against_log(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Result: 37/37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1


def test_json_deep_number_match(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Totals in `evidence/counts.json`: 37 tests passed.\n",
        {"counts.json": COUNTS})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1


def test_json_number_mismatch(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Totals in `evidence/counts.json`: 41 tests passed.\n",
        {"counts.json": COUNTS})
    result = audit_report(report, ev)
    assert len(findings_of(result, "number-mismatch")) == 1


def test_json_unverifiable_when_no_relevant_number(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Totals in `evidence/meta.json`: 9 files changed.\n",
        {"meta.json": json.dumps({"owner": "nightly", "mode": 2})})
    result = audit_report(report, ev)
    un = findings_of(result, "unverifiable")
    assert len(un) == 1 and un[0]["severity"] == "medium"
    assert result["verdict"] == "WARN"


def test_percent_verified_against_json(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Coverage hit 92% coverage — see `evidence/cov.json`.\n",
        {"cov.json": json.dumps({"coverage_percent": 92})})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1


def test_jsonl_line_count_verified(tmp_path):
    jsonl = "\n".join('{"event": %d}' % i for i in range(5)) + "\n"
    report, ev = make_case(
        tmp_path,
        "The tape holds 5 records — see `evidence/tape.jsonl`.\n",
        {"tape.jsonl": jsonl})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1


def test_jsonl_line_count_mismatch(tmp_path):
    jsonl = "\n".join('{"event": %d}' % i for i in range(5)) + "\n"
    report, ev = make_case(
        tmp_path,
        "The tape holds 8 records — see `evidence/tape.jsonl`.\n",
        {"tape.jsonl": jsonl})
    result = audit_report(report, ev)
    assert len(findings_of(result, "number-mismatch")) == 1


def test_csv_row_count_verified(tmp_path):
    csv_text = "name,score\nana,3\nbob,4\ncid,5\n"
    report, ev = make_case(
        tmp_path,
        "The export has 3 rows — see `evidence/export.csv`.\n",
        {"export.csv": csv_text})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1


def test_directory_file_count_verified(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Artifacts in `evidence/`: 2 files total.\n",
        {"a.txt": "one\n", "b.txt": "two\n"})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1


def test_quoted_pattern_count_in_log(tmp_path):
    log = "INFO start\nERROR disk full\nINFO ok\nERROR timeout\n"
    report, ev = make_case(
        tmp_path,
        'The log shows 2 lines containing "ERROR" — see '
        "`evidence/app.log`.\n",
        {"app.log": log})
    result = audit_report(report, ev)
    assert result["summary"]["verified"] == 1


def test_unparseable_artifact_is_unverifiable_not_pass(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Totals in `evidence/notes.md`: 9 files changed.\n",
        {"notes.md": "# notes\nsome prose with no tallies\n"})
    result = audit_report(report, ev)
    assert len(findings_of(result, "unverifiable")) == 1
    assert result["summary"]["verified"] == 0


# ---------------------------------------------------------------------------
# Manifest: integrity + freshness
# ---------------------------------------------------------------------------

def test_manifest_hash_ok_is_quiet(tmp_path):
    report, ev = make_case(
        tmp_path,
        "37 tests passed — see `evidence/run.log`.\n\n"
        "Date: 2026-10-20\n",
        {"run.log": PYTEST_LOG},
        manifest={"run.log": "2026-10-20T09:00:00"})
    result = audit_report(report, ev)
    assert findings_of(result, "hash-mismatch") == []
    assert findings_of(result, "stale-evidence") == []


def test_manifest_hash_mismatch_fires(tmp_path):
    report, ev = make_case(
        tmp_path,
        "37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG},
        manifest={"run.log": "2026-10-20T09:00:00"})
    # Tamper with the evidence after the manifest was written.
    (ev / "run.log").write_text("0 passed\n", encoding="utf-8")
    result = audit_report(report, ev)
    hm = findings_of(result, "hash-mismatch")
    assert len(hm) == 1 and hm[0]["severity"] == "high"


def test_manifest_missing_file_fires(tmp_path):
    report, ev = make_case(
        tmp_path, "Nothing claimed here.\n",
        {"run.log": PYTEST_LOG},
        manifest={"run.log": "2026-10-20T09:00:00"})
    (ev / "run.log").unlink()
    result = audit_report(report, ev)
    assert len(findings_of(result, "missing-artifact")) == 1


def test_stale_evidence_fires(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Date: 2026-10-21\n\n"
        "37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG},
        manifest={"run.log": "2026-10-20T09:00:00"})
    result = audit_report(report, ev)
    stale = findings_of(result, "stale-evidence")
    assert len(stale) == 1 and stale[0]["severity"] == "medium"


def test_fresh_evidence_is_not_stale(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Date: 2026-10-20\n\n"
        "37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG},
        manifest={"run.log": "2026-10-20T23:59:00"})
    result = audit_report(report, ev)
    assert findings_of(result, "stale-evidence") == []


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def test_url_inventoried_as_info(tmp_path):
    report, ev = make_case(
        tmp_path, "Changelog: https://example.org/notes for context.\n")
    result = audit_report(report, ev)
    urls = findings_of(result, "external-url")
    assert len(urls) == 1 and urls[0]["severity"] == "info"
    assert result["verdict"] == "PASS"


def test_url_only_backing_is_unverifiable(tmp_path):
    report, ev = make_case(
        tmp_path,
        "99 tests passed — dashboard at https://example.org/ci\n")
    result = audit_report(report, ev)
    assert findings_of(result, "unbacked-claim") == []
    un = findings_of(result, "unverifiable")
    assert len(un) == 1 and un[0]["severity"] == "medium"


# ---------------------------------------------------------------------------
# Scoring / verdicts / CLI
# ---------------------------------------------------------------------------

def test_score_arithmetic():
    one_high = [finding("unbacked-claim", "high", 1, "x")]
    assert compute_score(one_high) == 85
    mixed = [finding("unbacked-claim", "high", 1, "x"),
             finding("unverifiable", "medium", 2, "y"),
             finding("external-url", "info", 3, "z")]
    assert compute_score(mixed) == 79
    many = [finding("unbacked-claim", "high", i, "x")
            for i in range(10)]
    assert compute_score(many) == 0


def test_verdict_rules():
    assert compute_verdict([]) == "PASS"
    assert compute_verdict(
        [finding("external-url", "info", 1, "x")]) == "PASS"
    assert compute_verdict(
        [finding("unverifiable", "medium", 1, "x")]) == "WARN"
    assert compute_verdict(
        [finding("unbacked-claim", "high", 1, "x")]) == "FAIL"


def test_cli_pass_and_fail_exit_codes(tmp_path):
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good, ev = make_case(
        good_dir, "37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG})
    r = run_cli("check", str(good), "--evidence", str(ev))
    assert r.returncode == 0
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad, ev2 = make_case(
        bad_dir, "120 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG})
    r = run_cli("check", str(bad), "--evidence", str(ev2))
    assert r.returncode == 1


def test_cli_missing_inputs_exit_2(tmp_path):
    r = run_cli("check", str(tmp_path / "nope.md"),
                "--evidence", str(tmp_path))
    assert r.returncode == 2
    report, _ev = make_case(tmp_path, "hello\n")
    r = run_cli("check", str(report), "--evidence",
                str(tmp_path / "no-such-dir"))
    assert r.returncode == 2


def test_cli_strict_fails_on_medium_only(tmp_path):
    report, ev = make_case(
        tmp_path,
        "Totals in `evidence/meta.json`: 9 files changed.\n",
        {"meta.json": json.dumps({"owner": "nightly"})})
    r = run_cli("check", str(report), "--evidence", str(ev))
    assert r.returncode == 0  # WARN passes without --strict
    r = run_cli("check", str(report), "--evidence", str(ev),
                "--strict")
    assert r.returncode == 1


def test_cli_json_output_shape(tmp_path):
    report, ev = make_case(
        tmp_path, "37 tests passed — see `evidence/run.log`.\n",
        {"run.log": PYTEST_LOG})
    r = run_cli("check", str(report), "--evidence", str(ev),
                "--format", "json")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["tool"] == "claimcheck"
    assert data["verdict"] == "PASS"
    assert data["score"] == 100
    assert data["summary"]["verified"] == 1
    assert isinstance(data["claims"], list)
    assert isinstance(data["findings"], list)


def test_init_scaffold_passes_its_own_check(tmp_path):
    target = tmp_path / "scaffold"
    r = run_cli("init", "--dir", str(target))
    assert r.returncode == 0
    assert (target / "report.md").exists()
    assert (target / "EXPECTED.md").exists()
    assert (target / "evidence" / "evidence-manifest.json").exists()
    r = run_cli("check", str(target / "report.md"),
                "--evidence", str(target / "evidence"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_init_refuses_to_overwrite(tmp_path):
    target = tmp_path / "scaffold"
    assert run_cli("init", "--dir", str(target)).returncode == 0
    assert run_cli("init", "--dir", str(target)).returncode == 2


# ---------------------------------------------------------------------------
# Shipped examples
# ---------------------------------------------------------------------------

def test_good_example_passes():
    result = audit_report(EXAMPLES / "report-good.md",
                          EXAMPLES / "evidence")
    assert result["verdict"] == "PASS"
    assert result["score"] == 100
    assert result["summary"]["verified"] == 4
    assert result["summary"]["backed"] == 1


def _stderr_error_envelope(stderr):
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    assert lines[0].startswith("error: ")
    return json.loads(lines[-1])["error"]


def test_json_envelope_on_good_example():
    r = run_cli("check", str(EXAMPLES / "report-good.md"),
                "--evidence", str(EXAMPLES / "evidence"),
                "--format", "json")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["schema_version"] == 1
    assert isinstance(data["schema_version"], int)
    assert data["findings_summary"]["count"] == len(data["findings"])
    # good example: 1 info finding, no high/critical -> INFO
    assert data["findings_summary"]["highest_severity"] == "INFO"


def test_json_envelope_hype_highest_severity():
    r = run_cli("check", str(EXAMPLES / "report-hype.md"),
                "--evidence", str(EXAMPLES / "evidence"),
                "--format", "json")
    assert r.returncode == 1
    data = json.loads(r.stdout)
    assert data["schema_version"] == 1
    assert data["findings_summary"]["count"] == len(data["findings"])
    # hand-verified from counts: high == 4 (>0), no critical -> HIGH
    assert data["counts"]["high"] == 4
    assert data["findings_summary"]["highest_severity"] == "HIGH"


def test_cli_version():
    r = run_cli("--version")
    assert r.returncode == 0
    assert r.stdout.strip() == "claimcheck 1.0.1"


def test_cli_missing_report_error_envelope(tmp_path):
    r = run_cli("check", str(tmp_path / "nope.md"),
                "--evidence", str(tmp_path))
    assert r.returncode == 2
    err = _stderr_error_envelope(r.stderr)
    assert err["code"] == "file_not_found"
    assert "no such report file" in err["message"]


def test_cli_missing_evidence_error_envelope(tmp_path):
    report, _ev = make_case(tmp_path, "hello\n")
    r = run_cli("check", str(report), "--evidence",
                str(tmp_path / "no-such-dir"))
    assert r.returncode == 2
    err = _stderr_error_envelope(r.stderr)
    assert err["code"] == "evidence_not_found"
    assert "evidence directory not found" in err["message"]


def test_hype_example_fails_with_all_high_classes():
    result = audit_report(EXAMPLES / "report-hype.md",
                          EXAMPLES / "evidence")
    assert result["verdict"] == "FAIL"
    checks = {f["check"] for f in result["findings"]}
    assert {"unbacked-claim", "missing-artifact",
            "number-mismatch"} <= checks
    assert result["summary"]["mismatch"] == 2
    assert result["summary"]["unbacked"] == 1


# ---------------------------------------------------------------------------
# Family severity tiers (CONVENTIONS.md)
# ---------------------------------------------------------------------------

def test_finding_carries_tier():
    f = finding("unbacked-claim", "high", 1, "x")
    assert f["severity"] == "high" and f["tier"] == "HIGH"
    f = finding("unverifiable", "medium", 1, "x")
    assert f["tier"] == "INFO"
    f = finding("external-url", "info", 1, "x")
    assert f["tier"] == "INFO"


# ---------------------------------------------------------------------------
# docs — documentation integrity
# ---------------------------------------------------------------------------

FAMILY_LINKS = "\n".join(
    f"[{n}](https://github.com/maticcretic-commits/{n})"
    for n in ("agentproof", "promptre", "agentci", "secretscan",
              "costledger", "schemaguard", "skillaudit", "promptdiff",
              "tracetape", "promptlint", "claimcheck", "trustbadge"))

GOOD_README = f"""# SomeTool

A fine tool.

## Install

pip install sometool

## Usage

Run it:

```bash
sometool run
```

## Family

{FAMILY_LINKS}

## License

MIT
"""


def write_readme(tmp_path, text, name="README.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def docs_json(*argv):
    r = run_cli("docs", *argv, "--format", "json")
    return r, json.loads(r.stdout)


def test_docs_good_readme_passes(tmp_path):
    p = write_readme(tmp_path, GOOD_README)
    r, data = docs_json(str(p))
    assert r.returncode == 0
    assert data["tool"] == "claimcheck"
    assert data["kind"] == "docs"
    assert data["verdict"] == "PASS"
    assert data["score"] == 100
    assert data["findings"] == []
    assert data["findings_summary"] == {"count": 0,
                                        "highest_severity": "NONE"}
    assert data["summary"] == {"readmes": 1, "failed": 0,
                               "findings": 0}


def test_docs_unbalanced_fences_fail(tmp_path):
    p = write_readme(tmp_path,
                     GOOD_README + "\n```python\nnever closed\n")
    r, data = docs_json(str(p))
    assert r.returncode == 1
    assert data["verdict"] == "FAIL"
    checks = {f["check"] for f in data["findings"]}
    assert "unbalanced-fences" in checks
    f = [f for f in data["findings"]
         if f["check"] == "unbalanced-fences"][0]
    assert f["severity"] == "high" and f["tier"] == "HIGH"
    assert data["findings_summary"]["highest_severity"] == "HIGH"


def test_docs_truncation_marker_fails(tmp_path):
    bad = GOOD_README.replace("A fine tool.",
                              "A fine tool...[truncated 2901 chars]")
    p = write_readme(tmp_path, bad)
    r, data = docs_json(str(p))
    assert r.returncode == 1
    assert data["verdict"] == "FAIL"
    assert any(f["check"] == "truncation-marker"
               for f in data["findings"])


def test_docs_missing_sections_warn(tmp_path):
    bad = GOOD_README.replace("## Install", "## Setup")
    bad = bad.replace("## License", "## Legal")
    p = write_readme(tmp_path, bad)
    r, data = docs_json(str(p))
    assert r.returncode == 0           # WARN does not fail by default
    assert data["verdict"] == "WARN"
    msgs = [f["message"] for f in data["findings"]
            if f["check"] == "missing-section"]
    assert any("Install" in m for m in msgs)
    assert any("License" in m for m in msgs)
    # --strict promotes WARN to exit 1
    r2 = run_cli("docs", str(p), "--strict")
    assert r2.returncode == 1


def test_docs_missing_title_and_roster(tmp_path):
    bad = GOOD_README.replace("# SomeTool", "SomeTool")
    bad = bad.replace(
        "[trustbadge](https://github.com/maticcretic-commits/"
        "trustbadge)", "")
    p = write_readme(tmp_path, bad)
    r, data = docs_json(str(p))
    checks = {f["check"] for f in data["findings"]}
    assert "missing-title" in checks
    assert "incomplete-roster" in checks
    roster = [f for f in data["findings"]
              if f["check"] == "incomplete-roster"][0]
    assert "trustbadge" in roster["message"]


def test_docs_quick_start_counts_as_usage(tmp_path):
    p = write_readme(tmp_path,
                     GOOD_README.replace("## Usage", "## Quick start"))
    r, data = docs_json(str(p))
    assert r.returncode == 0
    assert data["verdict"] == "PASS"


def test_docs_directory_collects_readmes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    write_readme(tmp_path / "a", GOOD_README)
    write_readme(tmp_path / "b", GOOD_README + "\n```\nunclosed\n")
    r, data = docs_json(str(tmp_path))
    assert r.returncode == 1
    assert data["summary"]["readmes"] == 2
    assert data["summary"]["failed"] == 1


def test_docs_skips_examples_fixture_readmes(tmp_path):
    # Fixture documentation under examples/ is not a tool README and
    # must not be swept into the docs gate (regression: agentproof's
    # examples/ci-matrix/README.md broke the family-wide docs test).
    (tmp_path / "examples" / "ci-matrix").mkdir(parents=True)
    write_readme(tmp_path / "examples" / "ci-matrix",
                 "# Fixture notes\n\nNo sections here.\n")
    write_readme(tmp_path, GOOD_README)
    r, data = docs_json(str(tmp_path))
    assert r.returncode == 0
    assert data["verdict"] == "PASS"
    assert data["summary"]["readmes"] == 1


def test_docs_missing_target_is_caller_error(tmp_path):
    r = run_cli("docs", str(tmp_path / "nope"))
    assert r.returncode == 2
    assert '"code": "file_not_found"' in r.stderr


def test_docs_family_readmes_pass():
    # The family's own READMEs must pass their own gate: all 12 tool
    # READMEs, plus any family README added since (the .github
    # profile/index repo added two in Phase 2) — every one sound.
    root = Path(__file__).parent.parent.parent
    r, data = docs_json(str(root))
    assert r.returncode == 0
    assert data["verdict"] == "PASS"
    parents = {Path(res["file"]).parent.name
               for res in data["results"]}
    tools = {"agentproof", "promptre", "agentci", "secretscan",
             "costledger", "schemaguard", "skillaudit", "promptdiff",
             "tracetape", "promptlint", "claimcheck", "trustbadge"}
    assert tools <= parents
    assert data["summary"]["readmes"] >= 12


# ---------------------------------------------------------------------------
# Phase 2: SARIF output + stdin inputs
# ---------------------------------------------------------------------------

EXAMPLES_CC = Path(__file__).parent.parent / "examples"


def assert_sarif_shape(doc, tool_name):
    """SARIF 2.1.0 top-level required fields (structure assertion)."""
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    assert len(doc["runs"]) == 1
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] == tool_name
    assert driver["version"]
    assert driver["informationUri"].startswith(
        "https://github.com/maticcretic-commits/")
    rule_ids = {r["id"] for r in driver["rules"]}
    for res in run["results"]:
        assert res["ruleId"] in rule_ids
        assert res["level"] in ("error", "warning", "note", "none")
        assert res["message"]["text"]
    return run


def test_sarif_check_hype_report():
    js = run_cli("check", str(EXAMPLES_CC / "report-hype.md"),
                 "--evidence", str(EXAMPLES_CC / "evidence"),
                 "--format", "json")
    assert js.returncode == 1
    n_findings = len(json.loads(js.stdout)["findings"])
    proc = run_cli("check", str(EXAMPLES_CC / "report-hype.md"),
                   "--evidence", str(EXAMPLES_CC / "evidence"),
                   "--format", "sarif")
    assert proc.returncode == 1  # same verdict as text/json
    run = assert_sarif_shape(json.loads(proc.stdout), "claimcheck")
    assert len(run["results"]) == n_findings
    assert any(r["level"] == "error" for r in run["results"])
    loc = run["results"][0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"].endswith("report-hype.md")


def test_sarif_docs_on_family_readme(tmp_path):
    good = tmp_path / "README.md"
    good.write_text((Path(__file__).parent.parent / "README.md")
                    .read_text())
    proc = run_cli("docs", str(good), "--format", "sarif")
    assert proc.returncode == 0
    run = assert_sarif_shape(json.loads(proc.stdout), "claimcheck")
    assert run["results"] == []
    broken = tmp_path / "README-broken.md"
    broken.write_text("# T\n\n```\nunclosed fence\n")
    proc = run_cli("docs", str(broken), "--format", "sarif")
    assert proc.returncode == 1
    run = assert_sarif_shape(json.loads(proc.stdout), "claimcheck")
    rules = {r["ruleId"] for r in run["results"]}
    assert "unbalanced-fences" in rules
    assert any(r["level"] == "error" for r in run["results"])


def test_check_stdin_report():
    text = (EXAMPLES_CC / "report-hype.md").read_text()
    proc = subprocess.run(
        [sys.executable, str(CLI), "check", "-",
         "--evidence", str(EXAMPLES_CC / "evidence"),
         "--format", "json"],
        input=text, capture_output=True, text=True)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)  # stdout is pure JSON
    assert data["report"] == "<stdin>"
    assert data["verdict"] == "FAIL"


def test_docs_stdin_readme():
    text = (Path(__file__).parent.parent / "README.md").read_text()
    proc = subprocess.run(
        [sys.executable, str(CLI), "docs", "-", "--format", "json"],
        input=text, capture_output=True, text=True)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["target"] == "<stdin>"
    assert data["verdict"] == "PASS"
