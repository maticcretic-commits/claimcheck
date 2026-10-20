# ClaimCheck 📋

**Show your work.**

Agent reports make claims. "38 tests passed." "3 files changed."
"Deployed successfully." Some of those claims have evidence behind
them, and some are confident narrative wearing a number as a
costume. Nothing in the report tells you which is which — until
someone opens the folder and checks by hand.

ClaimCheck is that check, automated. It audits a report against
its evidence folder: every checkable claim must point at an
artifact, every referenced artifact must exist, and every number
the report claims must match the artifact it cites. Claims with
nothing behind them fail the audit — loudly, with line numbers.

Offline and dependency-free: stdlib-only Python, no network, no
account, fully deterministic. Same report, same verdict, every
time. ClaimCheck never fetches a URL.

```bash
python src/claimcheck.py check report.md --evidence evidence/
python src/claimcheck.py check report.md --evidence evidence/ --strict
python src/claimcheck.py check report.md --evidence evidence/ --format json
```

## The commands

| Command | What it does |
|---------|--------------|
| `check <report> --evidence <dir>` | Audit one report (`.md` / `.txt`) against an evidence directory. Per-claim findings with line numbers, a 0–100 score, and a PASS / WARN / FAIL verdict. |
| `check … --strict` | Exit `1` on WARN-level (medium) findings too — for CI gates that accept no unverifiable claims. |
| `check … --format json` | Machine-readable report: every extracted claim with its status, all findings, score, verdict. |
| `init` | Scaffold a sample *backed* report + evidence folder + manifest + `EXPECTED.md`. Break it on purpose and watch it fail. |

Exit codes: `0` = PASS or WARN, `1` = FAIL (any high finding;
with `--strict`, any medium), `2` = usage or IO error.

## What counts as a claim

Extraction is deterministic pattern work — no model, no guessing:

- **Numeric claims** — counts with units ("38 tests", "3 files
  changed", "12 commits"), fractions ("38/38 passed"), percentages
  ("92% coverage"), money amounts ("$1,200").
- **Completion claims** — lines asserting something is done:
  shipped, deployed, published, verified, fixed, "successfully",
  "in production", and their kin.
- **References** — file paths, filenames, and markdown-link targets
  a report names. A claim is **bound** to the artifacts referenced
  in the same bullet (including its continuation lines) or the same
  paragraph. A reference in a *different* bullet backs nothing.
  Fenced code blocks are quoted material and are not audited.

## The checks

| Check | Severity | Fires when |
|-------|----------|------------|
| `unbacked-claim` | 🟠 high | A numeric or completion claim with no artifact referenced in its bullet/paragraph. |
| `missing-artifact` | 🟠 high | A referenced file does not exist in the evidence folder (or next to the report). The evidence manifest can also raise this — see below. |
| `number-mismatch` | 🟠 high | The claimed number ≠ the number in the bound artifact: a pytest-style log's pass/fail counts, a JSON value under a matching key, a JSONL line count, a CSV row count, a directory's file count. |
| `hash-mismatch` | 🟠 high | An evidence file's sha256 differs from `evidence-manifest.json` — the evidence changed after the report was written. |
| `unverifiable` | 🟡 medium | The claim cites evidence, but the number can't be parsed out of it (or the claim is backed only by a URL). UNVERIFIABLE is reported, never silently passed. |
| `stale-evidence` | 🟡 medium | The manifest records a bound artifact *before* the report's own date — evidence older than the claim cannot back it. |
| `external-url` | ⚪ info | Every URL in the report, inventoried as UNVERIFIED-OFFLINE. ClaimCheck never fetches. |

Number matching is keyed, not vibes: a JSON artifact is searched
for numeric values under keys that share words with the claim
("tests passed" ↔ `tests_passed`), a pytest log is read for its
summary counts, a claim that quotes a pattern (`14 lines
containing "ERROR"`) is counted literally against the log.

### The evidence manifest

Raw file timestamps lie (copies, checkouts, restores), so
freshness and integrity run on an optional manifest instead.
Drop an `evidence-manifest.json` in the evidence folder:

```json
{"files": {"pytest.log": {"sha256": "9de9224e…",
                          "recorded_at": "2026-10-20T09:35:00"}}}
```

When it exists, ClaimCheck verifies every recorded hash
(`hash-mismatch` if one changed), flags recorded files that have
gone missing, and compares `recorded_at` against the report's
date for the stale check. `claimcheck init` writes one for you.

## Scoring

Start at 100. High −15, medium −6, info −0. Floor 0. **Verdict is
a rule, not just a score:** any high means FAIL; any medium (and
no highs) means WARN; otherwise PASS. Info findings — the URL
inventory — never lower a verdict.

## What ClaimCheck does NOT do

This section matters more than the feature list.

- **It does not judge truth.** ClaimCheck verifies that a report's
  claims are backed by artifacts that exist and that the numbers
  match. Whether the underlying work actually happened in the
  world, was done well, or was worth doing is beyond its reach —
  a forged log with matching numbers passes, exactly as a forged
  receipt passes an expense audit's arithmetic.
- **It does not fetch or verify URLs.** Links are inventoried as
  UNVERIFIED-OFFLINE. A claim backed only by a URL is reported
  as unverifiable, because offline it is.
- **It does not understand your report.** Extraction is lexical:
  a number with a unit word is a claim, a path-shaped token is a
  reference. Numbers in dates and URLs are excluded, fenced code
  is treated as quotation — beyond that, if your prose names a
  file that doesn't exist, ClaimCheck will say so. That bluntness
  is the feature: reports should only name evidence they have.

What it kills is exactly one failure mode — the confident claim
with nothing behind it. That failure mode is, empirically, most
of what's wrong with agent reports.

## Quick start

```bash
# 1. Scaffold a backed report and watch it pass:
claimcheck init
claimcheck check report.md --evidence evidence    # PASS, 100/100

# 2. Break it: claim 99 tests in report.md, run again — FAIL.

# 3. Gate your real run reports in CI:
claimcheck check run-report.md --evidence evidence/ --strict
```

## Example

From `examples/` — one honest report and one hype report, audited
against the same evidence folder (a pytest log showing **37
passed**, a counts JSON, a deploy log, and a manifest recording
all three on 2026-10-20).

`report-good.md` binds every claim to its artifact. Real captured
output:

```
$ claimcheck check examples/report-good.md --evidence examples/evidence
====================================================================
ClaimCheck — report audit
====================================================================
Report:   examples/report-good.md
Evidence: examples/evidence
Claims:   5 found — 4 verified, 1 backed, 0 unbacked, 0 mismatched, 0 unverifiable, 0 URL-only
Findings: 1
Score:    100/100 — ✅ PASS

## external-url
  line 20   ⚪ INFO   External URL inventoried as UNVERIFIED-OFFLINE — ClaimCheck never fetches: https://example.org/releases/billing-1.4

✅ PASS — every checkable claim is backed by its evidence.
```

`report-hype.md` claims 120 tests against that same 37-test log,
declares a deployment with no artifact behind it, and cites a
results file that was never produced. It is also dated a day
after the evidence was recorded. Real captured output:

```
$ claimcheck check examples/report-hype.md --evidence examples/evidence
====================================================================
ClaimCheck — report audit
====================================================================
Report:   examples/report-hype.md
Evidence: examples/evidence
Claims:   3 found — 0 verified, 0 backed, 1 unbacked, 2 mismatched, 0 unverifiable, 0 URL-only
Findings: 6
Score:    28/100 — ❌ FAIL

## unbacked-claim
  line 9    🟠 HIGH   Unbacked completion claim — no artifact is referenced in the same bullet or paragraph. Show your work.
             The payment rewrite is fully deployed and shipped to production.

## missing-artifact
  line 11   🟠 HIGH   Referenced artifact 'evidence/full-results.json' does not exist in the evidence folder (or next to the report).
             Full numbers are in `evidence/full-results.json` for review.

## number-mismatch
  line 7    🟠 HIGH   Number mismatch — the claim says 120, but the evidence says: pytest.log records 37 passed, not 120.
             Headline result: 120 tests passed — see `evidence/pytest.log`.
  line 13   🟠 HIGH   Number mismatch — the claim says 120, but the evidence says: counts.json records tests_passed=37, tests_total=37, not 120.
             Run totals in `evidence/counts.json`: 120 tests passed.

## stale-evidence
  line 7    🟡 MEDIUM Stale evidence — 'pytest.log' was recorded 2026-10-20 (per evidence-manifest.json), before the report's date 2026-10-21. Evidence older than the claim cannot back it.
             Headline result: 120 tests passed — see `evidence/pytest.log`.
  line 13   🟡 MEDIUM Stale evidence — 'counts.json' was recorded 2026-10-20 (per evidence-manifest.json), before the report's date 2026
```

## Part of AgentForge

ClaimCheck is the eleventh tool in the [AgentForge](https://github.com/maticcretic-commits)
trust layer for AI agents:

- **AgentProof** — verify agent work — [github.com/maticcretic-commits/agentproof](https://github.com/maticcretic-commits/agentproof)
- **PromptRE** — reverse-engineer agent prompts — [github.com/maticcretic-commits/promptre](https://github.com/maticcretic-commits/promptre)
- **AgentCI** — regression harness for agents — [github.com/maticcretic-commits/agentci](https://github.com/maticcretic-commits/agentci)
- **SecretScan** — scan agent outputs for leaked secrets — [github.com/maticcretic-commits/secretscan](https://github.com/maticcretic-commits/secretscan)
- **CostLedger** — cost accounting for agent runs — [github.com/maticcretic-commits/costledger](https://github.com/maticcretic-commits/costledger)
- **SchemaGuard** — validate agent outputs against their contracts — [github.com/maticcretic-commits/schemaguard](https://github.com/maticcretic-commits/schemaguard)
- **SkillAudit** — audit agent skills for hidden instructions and quality — [github.com/maticcretic-commits/skillaudit](https://github.com/maticcretic-commits/skillaudit)
- **PromptDiff** — diff prompt versions — [github.com/maticcretic-commits/promptdiff](https://github.com/maticcretic-commits/promptdiff)
- **TraceTape** — the canonical run tape — [github.com/maticcretic-commits/tracetape](https://github.com/maticcretic-commits/tracetape)
- **PromptLint** — lint prompts for quality defects — [github.com/maticcretic-commits/promptlint](https://github.com/maticcretic-commits/promptlint)
- **ClaimCheck** — audit agent reports against their evidence (this repo)
- **TrustBadge** — one trust score and badge for the whole family — [github.com/maticcretic-commits/trustbadge](https://github.com/maticcretic-commits/trustbadge)

AgentProof checks what an agent *did*; PromptRE checks what it was *told*; AgentCI checks it still *works* after you change it; SecretScan checks that nothing *leaked*; CostLedger checks what it *cost*; SchemaGuard checks the output still *fits the contract*; SkillAudit checks the skill was *safe to install*; PromptDiff checks what the prompt change *changed*; TraceTape keeps the whole run on *tape*; PromptLint keeps the prompt itself *linted clean*; ClaimCheck makes sure every claim is *backed*; TrustBadge puts the family's score *on one badge*.

## License

MIT
