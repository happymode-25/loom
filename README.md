# loom

Provable, plain-text project scheduling. One file, Python 3.9+, standard library only.

You keep a plan as plain text: declare tasks with durations and dependencies. `loom` parses and validates the dependency graph (fail-closed on unknown deps, self-deps, duplicates, or cycles), computes a deterministic topological order and the critical path, draws a self-contained HTML report, and can *seal* the plan — recording its hash and schedule in a hash-chained log so a plan can't be quietly rewritten.

## Quick start

```bash
python loom.py selftest
python loom.py check  plan.loom
python loom.py plan   plan.loom
python loom.py order  plan.loom
python loom.py render plan.loom --out report.html
python loom.py seal   plan.loom --note "release 1"
python loom.py history
python loom.py verify
```

## Plan grammar

```text
# comment
; comment
blank line

title "Website rebuild"

task brief      2
task design     5 after brief
task build      8 after design
task launch     2 after build
```

- `task NAME DURATION [after A, B, ...]` — `NAME` is a token, `DURATION` is a non-negative integer, `after` lists dependencies (comma-separated)
- A duration of `0` is a milestone
- Keywords are case-insensitive; task names are case-preserved
- Forward references are allowed
- Comments start with `#` or `;`

## Validation (fail-closed)

| Rule | Meaning |
|---|---|
| `no-tasks` | No tasks declared |
| `duplicate-task` | Same task declared more than once |
| `unknown-dependency` | Task depends on an undeclared task |
| `self-dependency` | Task depends on itself |
| `cycle` | Dependency graph contains a cycle |

All findings are `error` severity. `check`/`plan` exit `1` if any findings exist. `plan` only schedules if the graph is clean and acyclic.

## Scheduling

- Deterministic topological order (Kahn's algorithm, ties broken by declaration order)
- Earliest/latest start/finish, slack, critical path (tasks with zero slack)
- Makespan = duration of the longest path

## Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `check` | Clean | Findings | Parse/usage/missing |
| `plan` | Clean schedule | Findings | Parse/usage/missing |
| `order` | Clean schedule | Findings | Parse/usage/missing |
| `render` | Always | — | Parse/usage/missing |
| `seal` | Sealed | Findings/refused | Parse/usage/missing |
| `history`/`verify` | OK | Chain broken (verify) | Usage/store error |
| `selftest` | All passed | Failures | — |

## Seal ledger

Seals are recorded in a hash-chained JSONL ledger:

- Default: `~/.loom/ledger.jsonl`
- Override: `LOOM_STORE` env var or `--store`
- Deterministic timestamps: pass `--now ISO8601`

Each record links to the previous record's hash (`prev`), includes `plan_hash`, `tasks`, `makespan`, `critical`, and a `hash` over the canonicalized record. `verify` detects edits, deletions, and reordering.

## Gates (CI)

- `ci/import_gate.py` — stdlib-only imports (local module exempt)
- `ci/check_counts.py` — enforces `selftest >= 54`, `e2e >= 36`
- `ci/prove_suites_can_fail.py` — proves suites fail on negative controls
- `ci/mutation.py` — mutation sweep; requires named check failures, kills real mutants, canary must survive
- 5-cell CI matrix + gate job

## Files

- `loom.py` — single-file implementation
- `tests/test_loom.py` — e2e tests (subprocess)
- `ci/*.py` — gates
- `.github/workflows/ci.yml` — CI
- `examples/website.loom` — worked example
- `REPORT.md` — design notes and verification

## License

MIT (c) 2026 Hema
