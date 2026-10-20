# AgentForge — Family Conventions

This is the family-shared conventions file. An identical copy lives in the
root of all 12 AgentForge tool repos; the canonical copy is kept with the
family board (`agentforge/CONVENTIONS.md`). If you change it in one place,
change it everywhere.

The family: agentproof · promptre · agentci · secretscan · costledger ·
schemaguard · skillaudit · promptdiff · tracetape · promptlint · claimcheck ·
trustbadge. Every tool is a single-file, stdlib-only Python CLI: no
dependencies, fully offline, deterministic.

## Exit codes (contract — all 12 tools)

| Code | Meaning |
|------|---------|
| 0 | Clean / PASS — no findings at or above the tool's fail threshold |
| 1 | Findings / FAIL verdict — the tool ran fine and the answer is "no" |
| 2 | Caller error — bad arguments, missing or unreadable input file, invalid input data the caller supplied |
| 3 | Environment error — reserved; the tool itself could not run (e.g. a required external program is missing) |

Rules:
- A verdict of "could not verify / unknown" is a FAIL-class outcome → exit 1,
  never 2. Exit 2 is only for mistakes the *caller* made.
- argparse usage errors exit 2 (argparse default), consistent with this table.

## Error envelope (caller errors)

On a caller error (exit 2), the tool prints BOTH:

1. the human-readable line to stderr, e.g. `error: no such file: x.json`, and
2. one JSON line to stderr:

```json
{"error": {"code": "file_not_found", "message": "no such file: x.json"}}
```

`code` is a short snake_case string (`file_not_found`, `invalid_json`,
`invalid_args`, `not_a_directory`, …). `message` matches the human line
(without the `error: ` prefix).

## JSON output envelope

Every JSON output mode includes these top-level keys, without removing or
renaming any existing payload keys:

```json
{"tool": "<toolname>", "version": "<semver>", "schema_version": 1}
```

- If the payload is naturally a JSON object, the keys are added to it.
- If the payload is naturally a JSON array, it is wrapped in an envelope
  object under its natural key (e.g. `findings`, `results`), with the
  envelope keys alongside.

Finding-based tools additionally include an optional standardized summary:

```json
{"findings_summary": {"count": 3, "highest_severity": "HIGH"}}
```

- `count` = total number of findings in the report.
- `highest_severity` ∈ `CRITICAL | HIGH | INFO | NONE`: `CRITICAL` if any
  finding is critical; else `HIGH` if any is high; else `INFO` when findings
  exist but none are high/critical (medium/low/info collapse to `INFO`);
  `NONE` when there are no findings.

Consumers (notably TrustBadge) read the envelope as a fast path and fall back
to payload-specific parsing for older reports.

## Severity tiers

Every finding carries TWO labels in JSON output: the tool's native
`severity` (unchanged, tool-specific vocabulary) and a family `tier`
— the mapping is additive, consumers may use either.

| Tier | Meaning |
|------|---------|
| `CRITICAL` | Act now: a real secret, a hostile instruction, a broken contract on a blocking path |
| `HIGH` | Fails the gate: must be fixed or explicitly allow-listed |
| `INFO` | Reported, never fatal: hygiene, candidates, inventory |

Per-tool mapping (native → tier):

| Tool | CRITICAL | HIGH | INFO |
|------|----------|------|------|
| SecretScan | critical | high | medium, low |
| SkillAudit | critical | high | medium, low, info |
| ClaimCheck | — | high | medium, low, info |
| PromptLint | — | high | medium, low, info |
| PromptDiff | — | high (risks) | medium, low (risks) |
| SchemaGuard | — | contract violations (validate errors, breaking diff changes) | unsupported-keyword warnings, non-breaking changes |

SchemaGuard's per-item records carry no native severity field, so its
mapping lives at the summary level: `findings_summary.highest_severity`
is `HIGH` when any output violates the contract (or a diff change is
breaking), `INFO` when only warnings/non-breaking changes exist.
For every tool, `findings_summary.highest_severity` is the tier of
the worst finding (`NONE` when there are no findings).

Family green means: **no un-allowed CRITICAL or HIGH finding** —
see the dogfood gate below.

## CLI flags

- `--version` prints `<tool> <version>` and exits 0.
- `--no-color` disables ANSI color output; the `NO_COLOR` environment
  variable does the same. (Tools that emit no color accept the flag
  harmlessly.)
- `-h` / `--help` is help only.

## SARIF output

The six finding-type tools (secretscan, schemaguard, skillaudit,
claimcheck, promptlint, promptdiff) accept `--format sarif` and emit
a SARIF 2.1.0 log on stdout — the shape GitHub code scanning and
other SARIF consumers ingest:

- `version: "2.1.0"` + `$schema`; one run; `tool.driver` carries the
  tool name, version, `informationUri` (the repo URL) and `rules[]`
  derived from the tool's check ids.
- `results[]` map one-to-one from findings: `ruleId` = the check id,
  `message` = the finding's message, `locations[0]` = the finding's
  file (`artifactLocation.uri`) plus `region.startLine` when the
  finding has a line.
- `level` follows the severity tiers: CRITICAL/HIGH → `error`,
  native medium → `warning`, low/info → `note`.
- SARIF is a rendering of the same verdict: exit codes are identical
  to text/JSON mode, and secret values are never copied into the log
  (SecretScan reports the detector's type label only).

## Pipe composability (stdin in, JSON out)

Tools compose in shell pipelines: JSON/SARIF modes write ONLY the
JSON document to stdout (progress, warnings and errors go to stderr,
errors as the envelope above), and a file argument of `-` reads that
input from stdin, labelled `<stdin>` in output. stdin can be read
once, so in two-input commands at most one side may be `-` (a second
`-` is a caller error, exit 2).

| Command | stdin (`-`) |
|---|---|
| `promptre analyze` | one prompt text |
| `promptlint lint` | one prompt text |
| `promptdiff diff` / `stats` | one diff side / one prompt |
| `secretscan scan` | text to scan |
| `tracetape validate` / `summary` | one tape (validated in memory) |
| `costledger tally` / `budget` | one log stream (JSON or JSONL) |
| `schemaguard validate` | one JSON document or JSONL stream of outputs (the `--schema` stays a file — stdin is read once) |
| `schemaguard diff` | one schema side |
| `agentci run` / `compare` | one of the two JSON inputs |
| `skillaudit audit` | a lone SKILL.md, audited as a one-file skill |
| `claimcheck check` | the report (evidence stays a directory; without `--evidence`, the current directory is used) |
| `claimcheck docs` | one README |

Not applicable, by construction:

- `agentproof check` — verifies the current repo's working tree and
  runs a test command; it has no primary file input.
- `secretscan redact` — writes redacted copies of a file/tree to
  disk; a stream has no tree to mirror.
- `costledger compare` — both inputs are saved ledger artifacts.
- `schemaguard snapshot` — infers a baseline from a corpus of output
  files on disk.
- `skillaudit audit-all` — the target is a directory of skill dirs.
- `trustbadge aggregate` / `badge` — the input is a directory of
  sibling report files; one stream cannot carry the report set.

Pipeline pattern:

```bash
cat trace.jsonl | tracetape validate - --format json
cat agent-output.md | secretscan scan - --format sarif
cat new-prompt.md | promptdiff diff old.md - --fail-on-risk
```

## Fixture policy — no literal secrets, ever

- No literal secret-shaped string may appear anywhere in a repo: source,
  tests, examples, templates, sample configs, README. This includes cloud
  access keys (AKIA/ASIA…), GitHub tokens (`ghp_`, `github_pat_`), API keys
  (`sk-…`), private-key blocks, and cloud providers' published
  documentation-example keys — GitHub secret scanning alerts on some of
  those even though they are "only examples".
- Where a test or template needs a secret-shaped value, assemble it at
  runtime from fragments, e.g. `"AKIA" + "IOSFODNN7" + "EXAMPLE"`, so the
  literal never sits in the file.
- Family check: grep every repo for secret-shaped literals before a release
  (and run SecretScan on each tool's own `src/` — it must report zero
  findings on itself).

## Dogfood allow-list (expiring)

Intentional findings — a fixture that exists to be caught, a sample
that demonstrates a failure — are suppressed ONLY through the
family allow-list, never by editing a tool to look away. The list
lives at `agentforge/dogfood-allowlist.json` (family workspace, not
in any repo):

```json
{"entries": [
  {"tool": "secretscan",
   "target": "secretscan/examples/agent-output-with-secrets.md",
   "finding": "*",
   "reason": "Deliberate bad-example fixture …",
   "expires": "2027-01-15"}
]}
```

- `tool`: the tool whose finding is suppressed.
- `target`: a repo name, a repo-relative path, or a glob — matched
  against the run target and the finding's file.
- `finding`: a check id, a substring of the finding's type/message,
  or `"*"` for every finding in the target.
- `reason`: why this finding is intentional. Required, human.
- `expires`: an ISO date (entry lapses after that date) or a commit
  sha (entry holds only while the target repo's HEAD is exactly
  that sha; an unverifiable HEAD counts as lapsed — fail-closed).

An entry suppresses a matching CRITICAL/HIGH finding only while
unexpired. Expired entries are reported loudly by the gate on every
run, and relying on an expired entry fails the gate.

## Dogfood gate

`agentforge/dogfood.py` runs the family against itself before any
release or badge publish:

- SecretScan over every repo's `src/` + `examples/` + `README.md`;
- ClaimCheck `docs` over all 12 READMEs (balanced fences, no
  truncation markers, required sections — title, Install, Usage,
  License — and a complete family roster);
- PromptLint over the family's example prompts; SkillAudit over
  SKILL.md fixtures in siblings' examples;
- every tool against its own bundled examples, asserting the exit
  code its README states.

The gate writes `agentforge/dogfood-report.json` and exits 1 when
any un-allowed CRITICAL/HIGH finding appears, any tool errors, any
expected exit code mismatches, or an expired allow-list entry is
relied on. INFO findings are reported, never fatal.
