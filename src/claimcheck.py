#!/usr/bin/env python3
"""
ClaimCheck — "Show your work."

Agent reports make claims: "38 tests passed", "3 files changed",
"deployed successfully". Some of those claims have evidence behind
them and some are confident narrative. ClaimCheck audits a report
against its evidence folder: every checkable claim must point at an
artifact, every referenced artifact must exist, and every number a
report claims must match the artifact it cites.

ClaimCheck never judges whether a claim is TRUE in the world, and
it never fetches a URL. It verifies the paperwork: bindings,
existence, and numbers. A report can pass with perfect evidence
for trivial work — what it cannot do is pass with claims that
have nothing behind them.

    claimcheck check <report> --evidence <dir> [--strict]
    claimcheck init

Exit codes: 0 = PASS / WARN (with --strict: no medium-or-worse
findings), 1 = FAIL (any high finding; with --strict, any medium),
2 = usage / IO error. Stdlib only, no network.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

VERSION = "1.0.0"

MAX_FILE_BYTES = 5_000_000
MANIFEST_NAME = "evidence-manifest.json"

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}
SEVERITY_EMOJI = {"high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}
DEDUCTIONS = {"high": 15, "medium": 6, "low": 2, "info": 0}
VERDICT_EMOJI = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}

CHECK_ORDER = [
    "unbacked-claim", "missing-artifact", "number-mismatch",
    "hash-mismatch", "unverifiable", "stale-evidence",
    "external-url",
]
CHECK_CATEGORY = {
    "unbacked-claim": "backing",
    "missing-artifact": "reference",
    "number-mismatch": "numbers",
    "unverifiable": "numbers",
    "stale-evidence": "freshness",
    "hash-mismatch": "freshness",
    "external-url": "external",
}


def finding(check, severity, line, message, snippet=""):
    return {"check": check, "severity": severity,
            "category": CHECK_CATEGORY[check], "line": line,
            "message": message, "snippet": snippet}


def make_snippet(line):
    s = line.strip()
    if len(s) > 120:
        s = s[:117] + "..."
    return s


def _rx(p):
    return re.compile(p, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Claim extraction patterns
# ---------------------------------------------------------------------------

UNIT_WORDS = {
    "test", "tests", "file", "files", "commit", "commits",
    "line", "lines", "function", "functions", "module", "modules",
    "endpoint", "endpoints", "page", "pages", "issue", "issues",
    "warning", "warnings", "error", "errors", "case", "cases",
    "step", "steps", "repo", "repos", "repository", "repositories",
    "tool", "tools", "check", "checks", "run", "runs",
    "record", "records", "entry", "entries", "event", "events",
    "row", "rows", "artifact", "artifacts", "request", "requests",
    "bug", "bugs", "fix", "fixes", "change", "changes",
    "task", "tasks", "item", "items", "field", "fields",
    "table", "tables", "section", "sections", "question",
    "questions", "release", "releases", "build", "builds",
    "sample", "samples", "image", "images", "user", "users",
    "message", "messages", "email", "emails", "post", "posts",
    "download", "downloads", "endpoint", "chapter", "chapters",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "with", "without", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "all", "now", "see", "via",
    "per", "from", "into", "at", "by", "as", "now",
}

FRACTION_RX = re.compile(r"(\d+)\s*/\s*(\d+)")
PERCENT_RX = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)")
MONEY_RX = re.compile(
    r"[$€£₹]\s*(\d[\d,]*(?:\.\d+)?)"
    r"|\b(\d[\d,]*(?:\.\d+)?)\s*(dollars?|usd|euros?|inr|rupees)\b",
    re.IGNORECASE)
COUNT_RX = re.compile(r"\b(\d[\d,]*)\s+([a-z][a-z-]{2,})\b")
DATE_RX = re.compile(r"\b20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")
TIME_RX = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
URL_RX = re.compile(r"https?://[^\s)\]>\"']+")

COMPLETION_PATTERNS = [
    _rx(r"\b(shipped|deployed|published|released|merged|completed|"
        r"finished|verified|delivered|implemented|resolved|fixed)\b"),
    _rx(r"\bsuccessfully\b"),
    _rx(r"\ball tests pass\b"), _rx(r"\btests pass\b"),
    _rx(r"\bis (now )?live\b"), _rx(r"\bnow live\b"),
    _rx(r"\bin production\b"), _rx(r"\bup and running\b"),
    _rx(r"\bdeployed and verified\b"),
]

# ---------------------------------------------------------------------------
# Reference extraction patterns
# ---------------------------------------------------------------------------

BACKTICK_RX = re.compile(r"`([^`\n]+)`")
MDLINK_RX = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
BARE_PATH_RX = re.compile(
    r"(?<![\w/.:-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*"
    r"|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})(?![\w/-])")
QUOTED_RX = re.compile(r'"([^"\n]{1,60})"')


def _norm_token(tok):
    """Normalize a word for key/subject matching (naive singular)."""
    tok = tok.lower()
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        tok = tok[:-1]
    return tok


def _subject_tokens(text):
    toks = set()
    for tok in re.findall(r"[a-z][a-z0-9_-]*", text.lower()):
        if tok in STOPWORDS or len(tok) < 3:
            continue
        toks.add(_norm_token(tok))
    return toks


def _looks_like_path(token):
    token = token.strip()
    if not token or " " in token:
        return False
    if token.startswith(("http://", "https://", "mailto:")):
        return False
    if not re.search(r"[A-Za-z]", token):
        return False  # pure numbers: fractions (37/37), decimals
    if "/" in token:
        return True
    # A bare filename needs an extension containing a letter —
    # pytest.log yes, v1.4.0 (a version) no.
    m = re.search(r"\.([A-Za-z0-9]{1,8})$", token)
    return bool(m and re.search(r"[A-Za-z]", m.group(1)))


# ---------------------------------------------------------------------------
# Report structure: blocks (binding units)
# ---------------------------------------------------------------------------

def split_blocks(lines):
    """Split report lines into binding blocks.

    A block is: a heading on its own, a list item (including its
    continuation lines), or a paragraph (lines up to a blank line).
    Fenced code blocks are quoted material, not assertions: they are
    skipped entirely (audited=False).
    """
    blocks = []
    current = []
    fenced = False

    def flush(audited=True):
        nonlocal current
        if current:
            blocks.append({"lines": current, "audited": audited})
            current = []

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            flush(audited=not fenced)
            fenced = not fenced
            continue
        if fenced:
            current.append((lineno, line))
            continue
        if not stripped:
            flush()
            continue
        if re.match(r"^#{1,6}\s", stripped):
            flush()
            blocks.append({"lines": [(lineno, line)], "audited": True})
            continue
        if re.match(r"^([-*+]|\d+[.)])\s+", stripped):
            flush()
            current = [(lineno, line)]
            continue
        current.append((lineno, line))
    flush(audited=not fenced)
    return blocks


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def extract_references(block_lines):
    """File references and URLs in a block, with line numbers.

    Reference sources: markdown link targets, backtick-quoted
    paths, and bare path-like tokens. URLs are returned separately
    (they are inventoried, never fetched, and never count as local
    evidence on their own for number verification).
    """
    refs = []   # {"token", "line"}
    urls = []   # {"url", "line"}
    seen_ref = set()
    seen_url = set()
    for lineno, line in block_lines:
        masked = line
        for m in MDLINK_RX.finditer(line):
            target = m.group(2)
            if target.startswith(("http://", "https://")):
                if target not in seen_url:
                    seen_url.add(target)
                    urls.append({"url": target, "line": lineno})
            elif _looks_like_path(target) or target.endswith("/"):
                key = (target, lineno)
                if key not in seen_ref:
                    seen_ref.add(key)
                    refs.append({"token": target, "line": lineno})
        for m in URL_RX.finditer(masked):
            url = m.group(0).rstrip(".,;:")
            if url not in seen_url:
                seen_url.add(url)
                urls.append({"url": url, "line": lineno})
        # Mask URLs and markdown syntax before the bare-path scan.
        masked = URL_RX.sub(" ", masked)
        masked = MDLINK_RX.sub(lambda m: " " + m.group(1) + " ", masked)
        for m in BACKTICK_RX.finditer(masked):
            token = m.group(1).strip()
            if _looks_like_path(token) or token.endswith("/"):
                key = (token, lineno)
                if key not in seen_ref:
                    seen_ref.add(key)
                    refs.append({"token": token, "line": lineno})
        masked = BACKTICK_RX.sub(" ", masked)
        for m in BARE_PATH_RX.finditer(masked):
            token = m.group(1)
            if _looks_like_path(token):
                key = (token, lineno)
                if key not in seen_ref:
                    seen_ref.add(key)
                    refs.append({"token": token, "line": lineno})
    return refs, urls


def resolve_reference(token, report_dir, evidence_dir):
    """Resolve a referenced token to an existing file/dir, or None.

    Lookup order: evidence dir + token, report dir + token, then
    basename in either dir (reports often cite `evidence/x.log`
    while the checker is pointed at the evidence dir itself).
    """
    token = token.strip().rstrip(".,;:")
    if not token:
        return None
    name = Path(token).name
    candidates = [
        evidence_dir / token, report_dir / token,
        evidence_dir / name, report_dir / name,
    ]
    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

def _following_words(line, end, n=4):
    rest = line[end:end + 80]
    return " ".join(re.findall(r"[A-Za-z][A-Za-z-]*", rest)[:n])


def extract_claims(block_lines):
    """Extract checkable claims (numeric + completion) per line."""
    claims = []
    for lineno, line in block_lines:
        masked = URL_RX.sub(" ", line)
        masked = DATE_RX.sub(" ", masked)
        masked = TIME_RX.sub(" ", masked)
        numeric_here = []

        def add_claim(kind, value, unit, phrase, span_text):
            claim = {
                "line": lineno, "text": line.strip(),
                "kinds": [kind], "value": value, "unit": unit,
                "denominator": None,
                "subject": _subject_tokens(phrase + " " + span_text),
                "quoted": None,
            }
            claims.append(claim)
            numeric_here.append(claim)
            return claim

        # Fractions first (mask their span so 38/38 isn't double-read).
        work = masked
        for m in FRACTION_RX.finditer(work):
            phrase = _following_words(work, m.end())
            c = add_claim("fraction", int(m.group(1)), "fraction",
                          phrase, m.group(0))
            c["denominator"] = int(m.group(2))
            c["subject"] |= _subject_tokens(m.group(0))
        work = FRACTION_RX.sub(" ", work)
        for m in PERCENT_RX.finditer(work):
            phrase = _following_words(work, m.end())
            add_claim("percent", float(m.group(1)),
                      phrase.split()[0].lower() if phrase else "percent",
                      phrase, m.group(0))
        work = PERCENT_RX.sub(" ", work)
        for m in MONEY_RX.finditer(work):
            raw = m.group(1) or m.group(2)
            phrase = _following_words(work, m.end())
            add_claim("money", float(raw.replace(",", "")), "money",
                      phrase, m.group(0))
        work = MONEY_RX.sub(" ", work)
        for m in COUNT_RX.finditer(work):
            unit_raw = m.group(2).lower()
            if unit_raw not in UNIT_WORDS:
                continue
            phrase = _following_words(work, m.end())
            add_claim("count", int(m.group(1).replace(",", "")),
                      unit_raw, phrase, m.group(0))
        # A quoted/backticked non-path token on a claim line is a
        # pattern the claim may be counting in a log.
        for claim in numeric_here:
            for q in QUOTED_RX.findall(line):
                if not _looks_like_path(q):
                    claim["quoted"] = q
                    break
            if claim["quoted"] is None:
                for b in BACKTICK_RX.findall(line):
                    b = b.strip()
                    if b and not _looks_like_path(b) \
                            and not b.endswith("/"):
                        claim["quoted"] = b
                        break
        # Subject tokens come from the whole line (the words that
        # name what a number counts often precede it: "37 tests
        # passed (37/37)").
        line_subject = _subject_tokens(masked)
        for claim in numeric_here:
            claim["subject"] |= line_subject
        # Completion claims: attach to the line's first numeric
        # claim if one exists, else stand alone.
        if any(p.search(line) for p in COMPLETION_PATTERNS):
            if numeric_here:
                if "completion" not in numeric_here[0]["kinds"]:
                    numeric_here[0]["kinds"].append("completion")
            else:
                claims.append({
                    "line": lineno, "text": line.strip(),
                    "kinds": ["completion"], "value": None,
                    "unit": None, "denominator": None,
                    "subject": _subject_tokens(line), "quoted": None,
                })
    return claims


# ---------------------------------------------------------------------------
# Artifact number probes
# ---------------------------------------------------------------------------

def _json_number_leaves(obj, prefix=""):
    leaves = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            leaves.extend(_json_number_leaves(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            leaves.extend(_json_number_leaves(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        key_tokens = _subject_tokens(prefix.replace(".", " ")
                                     .replace("_", " "))
        leaves.append({"key": prefix, "value": obj,
                       "tokens": key_tokens})
    return leaves


def probe_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8",
                                         errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return _json_number_leaves(data)


def probe_pytest_log(text):
    metrics = {}
    for name, rx in (
            ("passed", r"(\d+)\s+passed\b"),
            ("failed", r"(\d+)\s+failed\b"),
            ("error", r"(\d+)\s+errors?\b"),
            ("skipped", r"(\d+)\s+skipped\b")):
        hits = re.findall(rx, text)
        if hits:
            metrics[name] = int(hits[-1])
    if not metrics:
        return None
    metrics["total"] = sum(v for k, v in metrics.items())
    return metrics


def count_dir_files(path):
    n = 0
    for child in path.rglob("*"):
        if child.is_file() and child.name != MANIFEST_NAME:
            n += 1
    return n


def count_jsonl_lines(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(1 for ln in text.splitlines() if ln.strip())


def count_csv_rows(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rows = [r for r in csv.reader(io.StringIO(text))
            if any(cell.strip() for cell in r)]
    return max(0, len(rows) - 1)  # minus header


# Probe result vocabulary: ("verified", detail) |
# ("mismatch", detail) | ("unverifiable", detail)
COUNT_UNITS_JSONL = {"line", "lines", "record", "records", "entry",
                     "entries", "event", "events", "row", "rows",
                     "item", "items"}
COUNT_UNITS_CSV = {"row", "rows", "record", "records", "entry",
                   "entries", "line", "lines", "item", "items"}


def probe_number(claim, artifact):
    """Check a numeric claim against one resolved artifact."""
    value = claim["value"]
    unit = (claim["unit"] or "").lower()
    subj = claim["subject"]
    if artifact.is_dir():
        if unit in ("file", "files"):
            n = count_dir_files(artifact)
            if n == value:
                return ("verified", f"{n} files in {artifact.name}/")
            return ("mismatch",
                    f"{artifact.name}/ holds {n} files, not {value}")
        return ("unverifiable",
                f"{artifact.name}/ is a directory; only file-count "
                "claims can be checked against it")
    suffix = artifact.suffix.lower()
    if suffix == ".json":
        leaves = probe_json(artifact)
        if leaves is None:
            return ("unverifiable",
                    f"{artifact.name} is not parseable JSON")
        if not leaves:
            return ("unverifiable",
                    f"{artifact.name} has no numeric values")
        relevant = [lf for lf in leaves if lf["tokens"] & subj]
        if claim["kinds"][0] == "fraction" \
                or (claim["denominator"] is not None):
            den = claim["denominator"]
            num_ok = any(lf["value"] == value for lf in relevant) \
                or any(lf["value"] == value for lf in leaves)
            totals = [lf for lf in leaves
                      if "total" in lf["tokens"]]
            den_ok = (not totals) or any(lf["value"] == den
                                         for lf in totals)
            if num_ok and den_ok:
                return ("verified",
                        f"{artifact.name} records {value}/{den}")
            if relevant or totals:
                found = ", ".join(
                    f"{lf['key']}={lf['value']}" for lf in
                    (relevant + totals)[:4])
                return ("mismatch",
                        f"{artifact.name} records {found}, "
                        f"not {value}/{den}")
            return ("unverifiable",
                    f"no matching numbers in {artifact.name}")
        if relevant:
            if any(lf["value"] == value for lf in relevant):
                return ("verified",
                        f"{artifact.name} records {value}")
            found = ", ".join(f"{lf['key']}={lf['value']}"
                              for lf in relevant[:4])
            return ("mismatch",
                    f"{artifact.name} records {found}, not {value}")
        if any(lf["value"] == value for lf in leaves):
            return ("verified",
                    f"{artifact.name} contains {value}")
        return ("unverifiable",
                f"no number in {artifact.name} is tied to this claim")
    if suffix == ".jsonl":
        n = count_jsonl_lines(artifact)
        if n is None:
            return ("unverifiable", f"{artifact.name} unreadable")
        if claim["denominator"] is not None:
            if n == claim["denominator"] and value <= n:
                return ("verified",
                        f"{artifact.name} has {n} records")
            return ("mismatch",
                    f"{artifact.name} has {n} records, "
                    f"not {claim['denominator']}")
        if unit in COUNT_UNITS_JSONL:
            if n == value:
                return ("verified",
                        f"{artifact.name} has {n} records")
            return ("mismatch",
                    f"{artifact.name} has {n} records, not {value}")
        return ("unverifiable",
                f"a {unit} claim cannot be checked against a "
                "JSONL line count")
    if suffix == ".csv":
        n = count_csv_rows(artifact)
        if n is None:
            return ("unverifiable", f"{artifact.name} unreadable")
        if claim["denominator"] is not None:
            if n == claim["denominator"] and value <= n:
                return ("verified", f"{artifact.name} has {n} rows")
            return ("mismatch",
                    f"{artifact.name} has {n} rows, "
                    f"not {claim['denominator']}")
        if unit in COUNT_UNITS_CSV:
            if n == value:
                return ("verified", f"{artifact.name} has {n} rows")
            return ("mismatch",
                    f"{artifact.name} has {n} rows, not {value}")
        return ("unverifiable",
                f"a {unit} claim cannot be checked against a "
                "CSV row count")
    if suffix in (".log", ".txt"):
        try:
            text = artifact.read_text(encoding="utf-8",
                                      errors="replace")
        except OSError:
            return ("unverifiable", f"{artifact.name} unreadable")
        if claim.get("quoted"):
            n = sum(1 for ln in text.splitlines()
                    if claim["quoted"] in ln)
            if n == value:
                return ("verified",
                        f"{n} lines in {artifact.name} contain "
                        f"\"{claim['quoted']}\"")
            return ("mismatch",
                    f"{n} lines in {artifact.name} contain "
                    f"\"{claim['quoted']}\", not {value}")
        metrics = probe_pytest_log(text)
        if metrics is None:
            if unit in ("line", "lines"):
                n = len(text.splitlines())
                if n == value:
                    return ("verified",
                            f"{artifact.name} has {n} lines")
                return ("mismatch",
                        f"{artifact.name} has {n} lines, not {value}")
            return ("unverifiable",
                    f"no countable summary in {artifact.name}")
        if claim["denominator"] is not None:
            if metrics.get("passed") == value \
                    and metrics.get("total") == claim["denominator"]:
                return ("verified",
                        f"{artifact.name} records {value}/"
                        f"{claim['denominator']}")
            return ("mismatch",
                    f"{artifact.name} records "
                    f"{metrics.get('passed', 0)}/{metrics['total']}, "
                    f"not {value}/{claim['denominator']}")
        metric = None
        if subj & {"passed", "pass", "passing"}:
            metric = "passed"
        elif subj & {"failed", "failure", "failures"}:
            metric = "failed"
        elif subj & {"skipped", "skip"}:
            metric = "skipped"
        elif subj & {"error", "errors"}:
            metric = "error"
        if metric and metric in metrics:
            if metrics[metric] == value:
                return ("verified",
                        f"{artifact.name} records {value} {metric}")
            return ("mismatch",
                    f"{artifact.name} records {metrics[metric]} "
                    f"{metric}, not {value}")
        if unit in ("test", "tests", "case", "cases"):
            candidates = {k: metrics[k]
                          for k in ("passed", "total")
                          if k in metrics}
            if value in candidates.values():
                which = [k for k, v in candidates.items()
                         if v == value][0]
                return ("verified",
                        f"{artifact.name} records {value} ({which})")
            found = ", ".join(f"{k}={v}"
                              for k, v in candidates.items())
            return ("mismatch",
                    f"{artifact.name} records {found}, not {value}")
        return ("unverifiable",
                f"no matching metric in {artifact.name}")
    return ("unverifiable",
            f"{artifact.name} ({suffix or 'no extension'}) is "
            "existence-only evidence; its numbers cannot be parsed")


# ---------------------------------------------------------------------------
# Manifest (integrity + freshness, no raw mtimes)
# ---------------------------------------------------------------------------

def load_manifest(evidence_dir):
    """Return (entries, error) — entries: name -> {sha256, recorded_at}."""
    path = evidence_dir / MANIFEST_NAME
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8",
                                         errors="replace"))
        files = data.get("files", {})
        if not isinstance(files, dict):
            return {}, "manifest 'files' is not an object"
        return files, None
    except (OSError, json.JSONDecodeError) as e:
        return {}, f"manifest unreadable: {e}"


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _date_part(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def compute_score(findings):
    return max(0, 100 - sum(DEDUCTIONS[f["severity"]]
                            for f in findings))


def compute_verdict(findings):
    if any(f["severity"] == "high" for f in findings):
        return "FAIL"
    if any(f["severity"] == "medium" for f in findings):
        return "WARN"
    return "PASS"


def audit_report(report_path, evidence_dir):
    report_path = Path(report_path)
    evidence_dir = Path(evidence_dir)
    text = report_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    report_dir = report_path.parent
    m = DATE_RX.search(text)
    report_date = _date_part(m.group(0)) if m else None

    findings = []
    claims_out = []
    manifest, manifest_err = load_manifest(evidence_dir)
    if manifest_err:
        findings.append(finding(
            "unverifiable", "medium", None,
            f"{MANIFEST_NAME} exists but could not be used: "
            f"{manifest_err}. Integrity/freshness checks skipped."))

    # Manifest integrity: every recorded file must exist, unchanged.
    manifest_paths = {}
    for name, meta in manifest.items():
        meta = meta if isinstance(meta, dict) else {}
        resolved = resolve_reference(name, report_dir, evidence_dir)
        if resolved is None or not resolved.is_file():
            findings.append(finding(
                "missing-artifact", "high", None,
                f"{MANIFEST_NAME} records '{name}' but no such "
                "file exists in the evidence folder."))
            continue
        manifest_paths[resolved] = meta
        want = meta.get("sha256")
        if want:
            got = sha256_of(resolved)
            if got.lower() != str(want).lower():
                findings.append(finding(
                    "hash-mismatch", "high", None,
                    f"'{name}' has changed since the manifest "
                    f"recorded it (sha256 mismatch) — the evidence "
                    "on disk is not the evidence the report was "
                    "written against."))

    seen_missing = {}
    url_seen = set()
    for block in split_blocks(lines):
        if not block["audited"]:
            continue
        block_lines = block["lines"]
        refs, urls = extract_references(block_lines)
        resolved_refs = []
        for ref in refs:
            resolved = resolve_reference(ref["token"], report_dir,
                                         evidence_dir)
            if resolved is None:
                key = ref["token"]
                if key not in seen_missing:
                    seen_missing[key] = ref["line"]
                    findings.append(finding(
                        "missing-artifact", "high", ref["line"],
                        f"Referenced artifact '{ref['token']}' "
                        "does not exist in the evidence folder (or "
                        "next to the report).",
                        make_snippet(
                            dict(block_lines).get(ref["line"], ""))))
            else:
                resolved_refs.append(resolved)
        for u in urls:
            if u["url"] not in url_seen:
                url_seen.add(u["url"])
                findings.append(finding(
                    "external-url", "info", u["line"],
                    f"External URL inventoried as UNVERIFIED-OFFLINE "
                    f"— ClaimCheck never fetches: {u['url']}"))
        block_claims = extract_claims(block_lines)
        for claim in block_claims:
            claim["bound"] = [str(p) for p in resolved_refs]
            claim["urls"] = [u["url"] for u in urls]
            is_numeric = claim["value"] is not None
            if not resolved_refs and not urls:
                claim["status"] = "unbacked"
                kinds = " and ".join(sorted(set(claim["kinds"])))
                findings.append(finding(
                    "unbacked-claim", "high", claim["line"],
                    f"Unbacked {kinds} claim — no artifact is "
                    "referenced in the same bullet or paragraph. "
                    "Show your work.",
                    make_snippet(claim["text"])))
            elif not resolved_refs and urls:
                claim["status"] = "url-only"
                findings.append(finding(
                    "unverifiable", "medium", claim["line"],
                    "Claim is backed only by an external URL, "
                    "which ClaimCheck does not fetch — it cannot be "
                    "verified offline.",
                    make_snippet(claim["text"])))
            elif not is_numeric:
                claim["status"] = "backed"
            else:
                # A claim is verified if ANY bound artifact verifies
                # it; mismatched if at least one parseable artifact
                # contradicts it and none verifies it; otherwise
                # unverifiable.
                results = [probe_number(claim, artifact)
                           for artifact in resolved_refs]
                status, detail = "unverifiable", ""
                for wanted in ("verified", "mismatch",
                               "unverifiable"):
                    hit = [d for s, d in results if s == wanted]
                    if hit:
                        status, detail = wanted, hit[0]
                        break
                claim["status"] = status
                claim["detail"] = detail
                if status == "mismatch":
                    findings.append(finding(
                        "number-mismatch", "high", claim["line"],
                        f"Number mismatch — the claim says "
                        f"{claim['value']}"
                        f"{'/' + str(claim['denominator']) if claim['denominator'] is not None else ''}"
                        f", but the evidence says: {detail}.",
                        make_snippet(claim["text"])))
                elif status == "unverifiable":
                    findings.append(finding(
                        "unverifiable", "medium", claim["line"],
                        f"Claim cites evidence, but the number "
                        f"could not be checked: {detail}. "
                        "UNVERIFIABLE is not a pass.",
                        make_snippet(claim["text"])))
            # Freshness: manifest-recorded evidence predating the
            # report's own date is stale for the claims it backs.
            if report_date and claim["status"] in (
                    "verified", "backed", "mismatch"):
                for artifact in resolved_refs:
                    meta = manifest_paths.get(artifact)
                    if not meta:
                        continue
                    recorded = _date_part(meta.get("recorded_at"))
                    if recorded and recorded < report_date:
                        findings.append(finding(
                            "stale-evidence", "medium",
                            claim["line"],
                            f"Stale evidence — '{artifact.name}' "
                            f"was recorded {recorded.isoformat()} "
                            f"(per {MANIFEST_NAME}), before the "
                            f"report's date {report_date.isoformat()}. "
                            "Evidence older than the claim cannot "
                            "back it.",
                            make_snippet(claim["text"])))
                        break
            claims_out.append(claim)

    findings.sort(key=lambda f: (SEVERITY_RANK[f["severity"]],
                                 f["line"] or 0,
                                 CHECK_ORDER.index(f["check"])))
    summary = {"claims": len(claims_out)}
    for st in ("verified", "backed", "unbacked", "mismatch",
               "unverifiable", "url-only"):
        summary[st] = sum(1 for c in claims_out
                          if c["status"] == st)
    return {
        "report": str(report_path),
        "evidence": str(evidence_dir),
        "report_date": report_date.isoformat() if report_date else None,
        "claims": claims_out,
        "findings": findings,
        "counts": {sev: sum(1 for f in findings
                            if f["severity"] == sev)
                   for sev in SEVERITY_RANK},
        "summary": summary,
        "score": compute_score(findings),
        "verdict": compute_verdict(findings),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_check_text(result):
    print("=" * 68)
    print("ClaimCheck — report audit")
    print("=" * 68)
    print(f"Report:   {result['report']}")
    print(f"Evidence: {result['evidence']}")
    s = result["summary"]
    print(f"Claims:   {s['claims']} found — {s['verified']} verified, "
          f"{s['backed']} backed, {s['unbacked']} unbacked, "
          f"{s['mismatch']} mismatched, {s['unverifiable']} "
          f"unverifiable, {s['url-only']} URL-only")
    print(f"Findings: {len(result['findings'])}")
    print(f"Score:    {result['score']}/100 — "
          f"{VERDICT_EMOJI[result['verdict']]} {result['verdict']}")
    findings = result["findings"]
    if not findings:
        print("\n✅ PASS — every checkable claim is backed by its "
              "evidence.")
        return
    by_check = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)
    for check in CHECK_ORDER:
        if check not in by_check:
            continue
        print(f"\n## {check}")
        for f in sorted(by_check[check],
                        key=lambda x: x["line"] or 0):
            where = f"line {f['line']}" if f["line"] else "report"
            print(f"  {where:<9} {SEVERITY_EMOJI[f['severity']]} "
                  f"{f['severity'].upper():<6} {f['message']}")
            if f["snippet"]:
                print(f"             {f['snippet']}")
    if result["verdict"] == "FAIL":
        print(f"\n❌ FAIL — {result['counts']['high']} high "
              "finding(s). Every claim must show its work.")
    elif result["verdict"] == "WARN":
        print("\n⚠️ WARN — no unbacked claims, but some claims "
              "could not be fully verified.")
    else:
        print("\n✅ PASS — every checkable claim is backed by its "
              "evidence.")


# ---------------------------------------------------------------------------
# init scaffold
# ---------------------------------------------------------------------------

SCAFFOLD_LOG = """\
============================= test session starts ==============================
platform linux -- python 3.12, pytest-9.1.1
collected 12 items

tests/test_sample.py ............                                  [100%]

============================== 12 passed in 0.18s ==============================
"""

SCAFFOLD_COUNTS = """\
{
  "tests_passed": 12,
  "tests_total": 12,
  "files_changed": 2
}
"""

SCAFFOLD_DEPLOY = """\
deploy started: sample-service v0.1.0
build ok
health check ok — service responding
deploy finished successfully
"""


def _scaffold_report(today):
    return f"""\
# Sample service — run report

Date: {today}

## Tests

The sample suite ran clean: 12 tests passed (12/12).
Evidence: `evidence/pytest.log`.

Run totals are recorded in `evidence/counts.json`: 12 tests passed
and 2 files changed.

## Deploy

The sample service is deployed and verified — see
`evidence/deploy.log` for the deploy record.
"""


SAMPLE_EXPECTED = """\
# What ClaimCheck should say about the scaffold

`claimcheck check report.md --evidence evidence` must PASS
(exit 0, score 100):

- "12 tests passed (12/12)" is verified against evidence/pytest.log.
- "12 tests passed" and "2 files changed" are verified against
  the numbers recorded in evidence/counts.json.
- "deployed and verified" is backed by the existing
  evidence/deploy.log.
- evidence/evidence-manifest.json records the sha256 of each
  evidence file, and every hash still matches.

Now break it on purpose: change one number in report.md (claim 99
tests), or edit an evidence file without updating the manifest,
and watch ClaimCheck fail the report. That failure is the tool.
"""


def _print_error(message, code):
    print(f"error: {message}", file=sys.stderr)
    print(json.dumps({"error": {"code": code, "message": message}}),
          file=sys.stderr)


def cmd_init(args):
    base = Path(args.dir) if args.dir else Path(".")
    ev = base / "evidence"
    today = date.today().isoformat()
    files = {
        base / "report.md": _scaffold_report(today),
        base / "EXPECTED.md": SAMPLE_EXPECTED,
        ev / "pytest.log": SCAFFOLD_LOG,
        ev / "counts.json": SCAFFOLD_COUNTS,
        ev / "deploy.log": SCAFFOLD_DEPLOY,
    }
    manifest_path = ev / MANIFEST_NAME
    for dest in list(files) + [manifest_path]:
        if dest.exists() and not args.force:
            _print_error(
                f"{dest} exists (use --force to overwrite)",
                "already_exists")
            return 2
    ev.mkdir(parents=True, exist_ok=True)
    for dest, content in files.items():
        dest.write_text(content, encoding="utf-8")
        print(f"wrote {dest}")
    entries = {}
    for name in ("pytest.log", "counts.json", "deploy.log"):
        entries[name] = {
            "sha256": sha256_of(ev / name),
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
    manifest_path.write_text(
        json.dumps({"files": entries}, indent=2) + "\n",
        encoding="utf-8")
    print(f"wrote {manifest_path}")
    print("\nTry it:")
    print("  claimcheck check report.md --evidence evidence"
          "   # expect PASS")
    print("Then change a number in report.md and run it again.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_check(args):
    report = Path(args.report)
    if not report.is_file():
        _print_error(f"no such report file: {report}",
                     "file_not_found")
        return 2
    if report.stat().st_size > MAX_FILE_BYTES:
        _print_error(f"report too large: {report}",
                     "file_too_large")
        return 2
    evidence = Path(args.evidence) if args.evidence \
        else report.parent
    if not evidence.is_dir():
        _print_error(
            f"evidence directory not found: {evidence}",
            "evidence_not_found")
        return 2
    try:
        result = audit_report(report, evidence)
    except OSError as e:
        _print_error(f"{e}", "read_error")
        return 2
    if args.format == "json":
        view = dict(result)
        view["claims"] = [
            {**c, "subject": sorted(c["subject"])}
            for c in result["claims"]]
        counts = result["counts"]
        if counts.get("critical", 0) > 0:
            highest = "CRITICAL"
        elif counts.get("high", 0) > 0:
            highest = "HIGH"
        elif result["findings"]:
            highest = "INFO"
        else:
            highest = "NONE"
        print(json.dumps(
            {"tool": "claimcheck", "version": VERSION,
             "schema_version": 1,
             "findings_summary": {
                 "count": len(result["findings"]),
                 "highest_severity": highest},
             **view},
            indent=2, ensure_ascii=False, default=str))
    else:
        print_check_text(result)
    if result["verdict"] == "FAIL":
        return 1
    if args.strict and result["counts"]["medium"]:
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="claimcheck",
        description="Audit an agent report against its evidence "
                    "folder — every checkable claim must point at "
                    "an artifact, every artifact must exist, and "
                    "every number must match. Show your work.")
    ap.add_argument("--version", action="version",
                    version=f"claimcheck {VERSION}")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color output (this tool "
                         "emits no color; accepted harmlessly)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="audit one report against an "
                                     "evidence directory")
    c.add_argument("report", help="report file (.md / .txt)")
    c.add_argument("--evidence",
                   help="evidence directory (default: the report's "
                        "own directory)")
    c.add_argument("--format", choices=("text", "json"),
                   default="text")
    c.add_argument("--strict", action="store_true",
                   help="exit 1 on WARN-level (medium) findings too")
    c.set_defaults(fn=cmd_check)

    i = sub.add_parser("init", help="scaffold a sample backed "
                                    "report + evidence folder + "
                                    "EXPECTED.md")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dir", help="directory to write the sample "
                                 "files into")
    i.set_defaults(fn=cmd_init)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
