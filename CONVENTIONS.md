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

## CLI flags

- `--version` prints `<tool> <version>` and exits 0.
- `--no-color` disables ANSI color output; the `NO_COLOR` environment
  variable does the same. (Tools that emit no color accept the flag
  harmlessly.)
- `-h` / `--help` is help only.

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
