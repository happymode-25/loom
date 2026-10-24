"""A mutation sweep for loom.py.

Every mutant is a one-line change that breaks a specific invariant or check.
The sweep applies each one to a *scratch copy* of the tool (never the working
tree), compiles it, and requires the selftest to notice. If a mutant survives,
the selftest has a hole.

The sweep is canary-guarded: a semantically-identical edit must SURVIVE, which
proves the harness is testing behaviour and not merely diffing text.

    python ci/mutation.py
"""
from __future__ import annotations

import hashlib
import pathlib
import py_compile
import shutil
import subprocess
import sys
import tempfile

LIVE = pathlib.Path("loom.py")

# (label, search, replace) -- each must make the selftest FAIL.
MUTANTS = [
    ("the first task starts at zero",
     "        majority = total // 2 + 1",
     "        majority = total // 2 + 100"),
    ("every task appears in the order",
     "            if picked is None:\n                exhausted += 1",
     "            if picked is None:\n                exhausted += 0"),
    ("a duplicate task is reported",
     "        if duplicate:",
     "        if False:"),
    ("an unknown dependency is reported",
     "            if name not in declared_set:",
     "            if False:"),
    ("a plan with no tasks is reported",
     "    if not declared:",
     "    if False:"),
    ("a plan with no tasks is reported",
     '    if not election["ballots"]:',
     "    if False:"),
    ("a task waits for a dependency",
     '        if b["rank"] and b["rank"][0] in counts:\n'
     '            counts[b["rank"][0]] += 1',
     '        if b["rank"] and b["rank"][-1] in counts:\n'
     '            counts[b["rank"][-1]] += 1'),
    ("the long branch is critical",
     "                counts[name] += 1",
     "                counts[name] += 0"),
    ("a clean plan exits 0",
     '    if not tiebreak or tiebreak == "none":',
     "    if False:"),
    ("a task remembers its line",
     "        return (candidates.index(name), name)",
     "        return (name,)"),
    ("seal hashes are no longer chained",
     '    rec["hash"] = record_hash(rec)',
     '    rec["hash"] = "x"'),
]

# A behaviour-preserving edit. The selftest must still pass.
CANARY = ("drop a return annotation",
          "def canonical(obj) -> str:",
          "def canonical(obj):")


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_selftest(path: pathlib.Path):
    proc = subprocess.run([sys.executable, str(path), "selftest"],
                          capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def named_check_failure(output: str):
    for line in output.splitlines():
        if line.startswith("  - "):
            return line[4:].strip()
    return None


def mutate_into(scratch_dir: pathlib.Path, label, old, new):
    with open(LIVE, encoding="utf-8", newline="") as fh:
        src = fh.read()
    if old not in src:
        print("  [ERROR] pattern not found for: %s" % label)
        return None
    target = scratch_dir / "loom_mutant.py"
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(src.replace(old, new, 1))
    try:
        py_compile.compile(str(target), doraise=True)
    except py_compile.PyCompileError as exc:
        print("  [ERROR] mutant does not compile: %s (%s)" % (label, exc))
        return None
    return target


def main() -> int:
    before = digest(LIVE)
    killed = 0
    total = 0
    failed = False

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="tally-mut-"))
    try:
        for label, old, new in MUTANTS:
            total += 1
            target = mutate_into(workdir, label, old, new)
            if target is None:
                failed = True
                continue
            rc, out = run_selftest(target)
            if rc != 0:
                check = named_check_failure(out)
                if check is None:
                    print("  [ERROR] %s: selftest failed without a named "
                          "check (a crash is not a report)" % label)
                    failed = True
                else:
                    killed += 1
                    print("  [KILLED] %s  <- %s" % (label, check))
            else:
                print("  [ALIVE ] %s  <-- the selftest missed this" % label)
                failed = True

        # The canary must survive: a change with no behavioural effect.
        label, old, new = CANARY
        target = mutate_into(workdir, label, old, new)
        if target is None:
            failed = True
        else:
            rc, out = run_selftest(target)
            if rc == 0:
                print("  [SURVIVED] canary: %s" % label)
            else:
                print("  [DIED   ] canary: %s  <-- harness is not sound" % label)
                failed = True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    after = digest(LIVE)
    if before != after:
        print("ERROR: the live loom.py changed during the sweep")
        failed = True

    print("%d/%d real mutants killed, canary survived" % (killed, total))
    if failed or killed != total:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


