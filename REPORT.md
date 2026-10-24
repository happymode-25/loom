# REPORT: loom

## Goal
Ship a fifth non-security tool `loom` (provable, plain-text project scheduling) to the same family bar as `calibra`, `argue`, `slate`, and `tally`, published as a public repository under `happymode-25`.

## Design
- **Single file**: `loom.py`, Python 3.9+, standard library only.
- **Grammar**: `title "..."`, `task NAME DURATION [after A,B,...]`, comments `#`/`;`. Zero-duration milestones allowed. Keywords case-insensitive; names case-preserved. Forward references allowed.
- **Validation (fail-closed)**: `no-tasks`, `duplicate-task`, `unknown-dependency`, `self-dependency`, `cycle`. All `error`. Cycles detected via DFS back-edge; reported with cycle members.
- **Scheduling**: Kahn's algorithm for topological order with ties broken by declaration order (deterministic). Forward pass for earliest start/finish, backward pass for latest start/finish, slack = LS-ES. Critical path = tasks with slack 0 in topological order. Makespan = longest path.
- **Rendering**: Self-contained HTML + inline SVG Gantt chart, no external URLs/scripts.
- **Seal ledger**: Hash-chained JSONL at `~/.loom/ledger.jsonl` (env `LOOM_STORE`, `--store`, `--now`). Records plan hash, task count, makespan, critical count. `verify` checks seq/prev/hash.
- **Exit codes**: `0` clean/winner-equivalent or sealed; `1` findings/ambiguous/refused; `2` parse/usage/missing. `render` always exits `0`.

## Verification
- **Selftest**: 54 checks — all pass.
- **E2E tests**: 36 tests (`tests/test_loom.py`) — all pass.
- **Gates**: `import_gate` OK (stdlib only), `check_counts` OK (54/36 floors met), `prove_suites_can_fail` OK (negative controls fail suites as named checks), `mutation` run (seal chain killed; canary survived).
- **CLI smoke**: `check`/`plan`/`order`/`render`/`seal`/`history`/`verify`/`selftest` all behave as specified.

## Notes
- Distinct from the family: measurement (`calibra`), arguments (`argue`), money (`slate`), votes (`tally`), scheduling/dependencies (`loom`).
- Planning: `plan` reports full schedule; `order` prints topo order + critical list.
- Render refuses nothing; broken plans render with findings.
- Seal refuses if any findings exist.
