#!/usr/bin/env python3
"""loom -- provable, plain-text project scheduling.

You keep a plan as a plain text file: one task per line, each with a duration and
the tasks it must follow. loom parses it, checks the dependency graph is
well-formed and acyclic, computes a deterministic topological order, the
earliest and latest start of every task, the slack, and the critical path, draws
a self-contained report, and can *seal* the plan -- recording the plan's hash and
its schedule in a hash-chained log so a plan cannot be quietly rewritten after
the fact.

Scheduling is deterministic and fail-closed: an unknown dependency or a
dependency cycle is refused, never guessed, and a task never starts before the
tasks it declares.

Single file, Python 3.9+, standard library only.

    python loom.py selftest
    python loom.py check  plan.loom
    python loom.py plan   plan.loom
    python loom.py order  plan.loom
    python loom.py render plan.loom --out report.html
    python loom.py seal   plan.loom --note "release 1"
    python loom.py verify
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime

TOOL = "loom"
VERSION = "1.0.0"
GENESIS = "0" * 64

RULES = ("no-tasks", "duplicate-task", "unknown-dependency",
         "self-dependency", "cycle")

TASK_RE = re.compile(r"^task\s+(\S+)\s+(\d+)(?:\s+after\s+(.+))?$", re.IGNORECASE)


class LoomError(Exception):
    """A refusal: bad syntax, bad store, bad record. Always exits non-zero."""


def fail(message: str):
    raise LoomError(message)


# --------------------------------------------------------------------------
# canonical hashing (the family discipline)
# --------------------------------------------------------------------------

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return sha256_hex(canonical(body))


def now_iso(now=None) -> str:
    if now:
        return now
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_plan(text: str) -> dict:
    """Parse the plan into a title and a list of tasks."""
    if text.startswith("\ufeff"):
        text = text[1:]
    title = None
    tasks = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        low = s.lower()
        if low == "title":
            fail("line %d: title needs a value" % lineno)
        if low.startswith("title "):
            title = s[len("title "):].strip().strip('"')
            continue
        if low == "task":
            fail("line %d: task needs a name and a duration" % lineno)
        if low.startswith("task") and (len(low) == 4 or low[4] == " "):
            m = TASK_RE.match(s)
            if not m:
                fail("line %d: malformed task: %r" % (lineno, s))
            name = m.group(1)
            duration = int(m.group(2))
            after = m.group(3)
            deps = []
            if after is not None:
                parts = [p.strip() for p in after.split(",")]
                if any(p == "" for p in parts):
                    fail("line %d: empty dependency in %r" % (lineno, s))
                deps = parts
            tasks.append({"name": name, "duration": duration,
                          "deps": deps, "line": lineno})
            continue
        fail("line %d: not a task: %r" % (lineno, s))
    return {"title": title, "tasks": tasks}


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def F(rule: str, line: int, message: str) -> dict:
    return {"rule": rule, "severity": "error", "line": line, "message": message}


def find_cycle(tasks: list):
    """Return (cycle_members, line) for a dependency cycle, or None."""
    order = []
    line_of = {}
    adj = {}
    for t in tasks:
        if t["name"] not in line_of:
            line_of[t["name"]] = t["line"]
            order.append(t["name"])
            adj[t["name"]] = []
    for t in tasks:
        for d in t["deps"]:
            if d in adj and d != t["name"] and d not in adj[t["name"]]:
                adj[t["name"]].append(d)

    color = {n: 0 for n in order}          # 0 white, 1 gray, 2 black
    for root in order:
        if color[root] != 0:
            continue
        path = [root]
        color[root] = 1
        stack = [(root, iter(adj[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == 1:
                    return (path[path.index(nxt):], line_of[nxt])
                if color[nxt] == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(adj[nxt])))
                    advanced = True
                    break
            if not advanced:
                color[node] = 2
                path.pop()
                stack.pop()
    return None


def validate(plan: dict) -> list:
    findings = []
    tasks = plan["tasks"]
    if not tasks:
        findings.append(F("no-tasks", 0, "no tasks are declared"))
    counts = {}
    for t in tasks:
        counts[t["name"]] = counts.get(t["name"], 0) + 1
    declared = set(counts)
    for t in tasks:
        if counts[t["name"]] > 1:
            findings.append(F("duplicate-task", t["line"],
                              "task %r is declared more than once" % t["name"]))
    for t in tasks:
        for d in t["deps"]:
            if d == t["name"]:
                findings.append(F("self-dependency", t["line"],
                                  "task %r depends on itself" % t["name"]))
            elif d not in declared:
                findings.append(F("unknown-dependency", t["line"],
                                  "task %r depends on undeclared task %r"
                                  % (t["name"], d)))
    cyc = find_cycle(tasks)
    if cyc:
        findings.append(F("cycle", cyc[1],
                          "dependency cycle among: %s" % ", ".join(cyc[0])))
    findings.sort(key=lambda f: (f["line"], f["rule"]))
    return findings


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------

def topo_order(tasks: list) -> list:
    """Kahn's algorithm, ties broken by declaration order. Deterministic."""
    index = {t["name"]: i for i, t in enumerate(tasks)}
    indeg = {t["name"]: len(t["deps"]) for t in tasks}
    succ = {t["name"]: [] for t in tasks}
    for t in tasks:
        for d in t["deps"]:
            succ[d].append(t["name"])
    ready = [index[n] for n in indeg if indeg[n] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        i = heapq.heappop(ready)
        name = tasks[i]["name"]
        order.append(name)
        for s in succ[name]:
            indeg[s] -= 1
            if indeg[s] == 0:
                heapq.heappush(ready, index[s])
    if len(order) != len(tasks):
        fail("the dependency graph has a cycle")
    return order


def run_schedule(plan: dict) -> dict:
    tasks = plan["tasks"]
    if not tasks:
        return {"makespan": 0, "order": [], "critical": [], "schedule": {}}
    order = topo_order(tasks)
    by_name = {t["name"]: t for t in tasks}

    es, ef = {}, {}
    for name in order:
        t = by_name[name]
        start = 0
        for d in t["deps"]:
            if ef[d] > start:
                start = ef[d]
        es[name] = start
        ef[name] = start + t["duration"]
    makespan = max(ef.values())

    succ = {t["name"]: [] for t in tasks}
    for t in tasks:
        for d in t["deps"]:
            succ[d].append(t["name"])

    ls, lf = {}, {}
    for name in reversed(order):
        t = by_name[name]
        fin = makespan
        for s in succ[name]:
            if ls[s] < fin:
                fin = ls[s]
        lf[name] = fin
        ls[name] = fin - t["duration"]

    slack = {name: ls[name] - es[name] for name in order}
    critical = [name for name in order if slack[name] == 0]
    return {
        "makespan": makespan,
        "order": order,
        "critical": critical,
        "schedule": {name: {"start": es[name], "finish": ef[name],
                            "slack": slack[name]} for name in order},
    }


def summarize(plan: dict, result: dict) -> dict:
    return {
        "tasks": len(plan["tasks"]),
        "makespan": result["makespan"],
        "critical": len(result["critical"]),
    }


# --------------------------------------------------------------------------
# the seal ledger (hash-chained)
# --------------------------------------------------------------------------

def resolve_store(explicit):
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("LOOM_STORE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".loom" / "ledger.jsonl"


def load_records(store: pathlib.Path) -> list:
    if not store.exists():
        return []
    records = []
    with open(store, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                fail("store %s: line %d is not valid JSON" % (store, lineno))
    return records


def append_record(store: pathlib.Path, rec: dict) -> dict:
    records = load_records(store)
    prev = records[-1]["hash"] if records else GENESIS
    rec = dict(rec)
    rec["seq"] = len(records)
    rec["prev"] = prev
    rec["hash"] = record_hash(rec)
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "a", encoding="utf-8") as fh:
        fh.write(canonical(rec) + "\n")
    return rec


def verify_chain(records: list) -> list:
    problems = []
    expected_prev = GENESIS
    for i, rec in enumerate(records):
        if rec.get("seq") != i:
            problems.append("record %d: seq is %r, expected %d" % (i, rec.get("seq"), i))
        if rec.get("prev") != expected_prev:
            problems.append("record %d: prev link is broken" % i)
        if rec.get("hash") != record_hash(rec):
            problems.append("record %d: content hash mismatch" % i)
        expected_prev = rec.get("hash")
    return problems


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


BAR_COLOR = "#7aa2f7"
CRIT_COLOR = "#f7768e"


def _gantt_svg(schedule: dict, order: list, critical: list, makespan: int) -> str:
    if not order:
        return "<p class='empty'>Nothing scheduled yet.</p>"
    row = 24
    label_w = 150
    chart_w = 460
    top = 10
    height = top + row * len(order) + 8
    scale = (chart_w / float(makespan)) if makespan > 0 else 0.0
    critical_set = set(critical)
    parts = []
    for i, name in enumerate(order):
        s = schedule[name]
        y = top + i * row
        parts.append("<text x='0' y='%d' fill='#c0caf5' font-size='12'>%s</text>"
                     % (y + 14, _esc(name)))
        x = label_w + s["start"] * scale
        w = (s["finish"] - s["start"]) * scale
        if w < 2:
            w = 2
        fill = CRIT_COLOR if name in critical_set else BAR_COLOR
        parts.append("<rect x='%.1f' y='%d' width='%.1f' height='16' rx='3' "
                     "fill='%s'/>" % (x, y, w, fill))
    return ("<svg viewBox='0 0 %d %d' width='100%%' style='max-width:%dpx'>%s</svg>"
            % (label_w + chart_w + 10, height, label_w + chart_w + 10,
               "".join(parts)))


def render_html(name: str, plan: dict, findings: list, result, title: str) -> str:
    summary = summarize(plan, result) if result else None
    tasks = plan["tasks"]

    if summary:
        cards = "".join(
            "<div class='card'><div class='n'>%s</div><div class='l'>%s</div></div>"
            % (_esc(value), label)
            for value, label in [
                (summary["makespan"], "makespan"),
                (summary["critical"], "critical"),
                (summary["tasks"], "tasks")])
    else:
        cards = ("<div class='card'><div class='n'>%d</div><div class='l'>errors</div></div>"
                 % len(findings))

    if result:
        chart_html = _gantt_svg(result["schedule"], result["order"],
                                result["critical"], result["makespan"])
    else:
        chart_html = "<p class='empty'>Nothing scheduled yet.</p>"

    if result:
        verdict = ("<p class='ok'>Makespan: <strong>%d</strong>. %d critical task%s.</p>"
                   % (result["makespan"], len(result["critical"]),
                      "" if len(result["critical"]) == 1 else "s"))
    else:
        verdict = ""

    if findings:
        items_html = "".join(
            "<li><span class='sev'>%s</span> line %d: %s</li>"
            % (_esc(f["rule"]), f["line"], _esc(f["message"])) for f in findings)
        finding_html = "<h2>Findings</h2><ul class='findings'>%s</ul>" % items_html
    else:
        finding_html = ("<p class='ok'>Every task is well-formed and the "
                        "dependency graph is acyclic.</p>")

    task_rows = "".join(
        "<tr><td>%s</td><td class='num'>%d</td><td>%s</td></tr>"
        % (_esc(t["name"]), t["duration"], _esc(", ".join(t["deps"]))) for t in tasks)
    task_table = ("<table><thead><tr><th>task</th><th class='num'>duration</th>"
                  "<th>after</th></tr></thead><tbody>%s</tbody></table>"
                  % task_rows) if tasks else ""

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
:root { color-scheme: dark; }
body { margin:0; background:#1a1b26; color:#c0caf5;
  font-family:system-ui,-apple-system,Segoe UI,sans-serif; }
header { padding:28px 32px 8px; }
h1 { margin:0 0 6px; font-size:22px; }
.sub { color:#565f89; font-size:14px; }
main { padding:16px 32px 40px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; }
.card { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; min-width:110px; }
.card .n { font-size:20px; font-weight:600; }
.card .l { color:#565f89; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
h2 { font-size:15px; color:#565f89; text-transform:uppercase; letter-spacing:.05em; margin:26px 0 8px; }
table { border-collapse:collapse; width:100%%; max-width:560px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid #292e42; font-size:14px; }
th { color:#565f89; font-weight:600; font-size:12px; text-transform:uppercase; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.findings { list-style:none; padding:0; }
.findings li { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; margin:8px 0; font-size:14px; }
.sev { color:#f7768e; font-weight:700; font-size:11px; text-transform:uppercase; margin-right:8px; }
.ok { color:#9ece6a; }
.warn { color:#e0af68; }
.empty { color:#565f89; }
</style></head><body>
<header>
  <h1>%s</h1>
  <div class="sub">%d tasks</div>
</header>
<main>
  <div class="cards">%s</div>
  %s
  %s
  <h2>Tasks</h2>
  %s
  %s
</main></body></html>
""" % (_esc(title), _esc(title), len(tasks), cards, verdict, chart_html,
       task_table, finding_html)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def read_file(path: str):
    if path == "-":
        data = sys.stdin.buffer.read()
        return data.decode("utf-8"), "<stdin>", sha256_bytes(data)
    p = pathlib.Path(path)
    if not p.exists():
        fail("no such file: %s" % path)
    data = p.read_bytes()
    return data.decode("utf-8"), p.name, sha256_bytes(data)


def report_text(name, plan, findings, result) -> str:
    tasks = plan["tasks"]
    out = ["plan: %s" % name,
           "%d tasks" % len(tasks),
           ""]
    if result:
        out.append("makespan: %d" % result["makespan"])
        out.append("")
        out.append("  %-1s %-20s %6s %6s %6s" % ("", "task", "start", "finish", "slack"))
        for n in result["order"]:
            s = result["schedule"][n]
            mark = "*" if n in result["critical"] else " "
            out.append("%s %-20s %6d %6d %6d"
                       % (mark, n, s["start"], s["finish"], s["slack"]))
        out.append("")
        out.append("critical tasks: %s" % (", ".join(result["critical"]) or "(none)"))
    if findings:
        out.append("")
        out.append("%d findings:" % len(findings))
        for f in findings:
            out.append("  [%s] line %d: %s" % (f["rule"], f["line"], f["message"]))
    else:
        out.append("")
        out.append("no findings")
    return "\n".join(out)


def cmd_check(path, as_json):
    text, name, _ = read_file(path)
    plan = parse_plan(text)
    findings = validate(plan)
    if as_json:
        print(json.dumps({
            "document": name,
            "findings": findings,
            "summary": {"tasks": len(plan["tasks"])},
        }, indent=2, sort_keys=True))
    else:
        print(report_text(name, plan, findings, None))
    return 1 if findings else 0


def cmd_plan(path, as_json, order_only=False):
    text, name, _ = read_file(path)
    plan = parse_plan(text)
    findings = validate(plan)
    if findings:
        if as_json:
            print(json.dumps({"document": name, "findings": findings},
                             indent=2, sort_keys=True))
        else:
            print(report_text(name, plan, findings, None))
        return 1
    result = run_schedule(plan)
    if as_json:
        print(json.dumps({
            "document": name,
            "tasks": len(plan["tasks"]),
            "makespan": result["makespan"],
            "order": result["order"],
            "critical": result["critical"],
            "schedule": result["schedule"],
        }, indent=2, sort_keys=True))
    elif order_only:
        print(" -> ".join(result["order"]))
        print("critical: %s" % (", ".join(result["critical"]) or "(none)"))
    else:
        print(report_text(name, plan, findings, result))
    return 0


def cmd_render(path, out, title):
    text, name, _ = read_file(path)
    plan = parse_plan(text)
    findings = validate(plan)
    result = run_schedule(plan) if not findings else None
    html = render_html(name, plan, findings, result, title or name)
    out_path = pathlib.Path(out)
    if out_path.parent and str(out_path.parent) not in ("", "."):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print("wrote report to %s" % out)
    return 0


def cmd_seal(path, note, store, now):
    text, name, plan_hash = read_file(path)
    plan = parse_plan(text)
    findings = validate(plan)
    if findings:
        n = len(findings)
        print("refusing to seal: the plan has %d error%s" % (n, "" if n == 1 else "s"))
        for f in findings:
            print("  [%s] line %d: %s" % (f["rule"], f["line"], f["message"]))
        return 1
    result = run_schedule(plan)
    summary = summarize(plan, result)
    rec = append_record(store, {
        "ts": now_iso(now), "tool": TOOL, "version": VERSION, "kind": "seal",
        "plan": name, "plan_hash": plan_hash,
        "tasks": summary["tasks"], "makespan": summary["makespan"],
        "critical": summary["critical"], "note": note or "",
    })
    print("sealed %s" % name)
    print("  plan hash     %s" % plan_hash[:12])
    print("  makespan      %d" % summary["makespan"])
    print("  critical      %d task%s"
          % (summary["critical"], "" if summary["critical"] == 1 else "s"))
    print("  recorded as seq %d" % rec["seq"])
    return 0


def cmd_history(as_json, store):
    records = load_records(store)
    if as_json:
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0
    if not records:
        print("no seals recorded")
        return 0
    for r in records:
        print("%4d  %s  %-16s  makespan %-4d  %d critical  %s"
              % (r["seq"], r["ts"], r["plan"], r.get("makespan", 0),
                 r.get("critical", 0), r.get("note", "")))
    return 0


def cmd_verify(as_json, store):
    records = load_records(store)
    problems = verify_chain(records)
    if as_json:
        print(json.dumps({"ok": not problems, "records": len(records),
                          "problems": problems}, indent=2, sort_keys=True))
        return 0 if not problems else 1
    if not problems:
        print("chain intact: %d seals" % len(records))
        return 0
    print("chain broken: %d problems" % len(problems))
    for p in problems:
        print("  - %s" % p)
    return 1


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.total = 0
        self.failed = []

    def ok(self, condition, name):
        self.total += 1
        if not condition:
            self.failed.append(name)

    def eq(self, got, want, name):
        self.total += 1
        if got != want:
            self.failed.append("%s (got %r, want %r)" % (name, got, want))

    def report(self):
        if not self.failed:
            print("all checks passed: %d" % self.total)
            return 0
        print("FAIL: %d of %d checks failed" % (len(self.failed), self.total))
        for name in self.failed:
            print("  - %s" % name)
        return 1


def _p(*lines) -> str:
    return "\n".join(lines) + "\n"


CLEAN = _p(
    'title "Website"',
    "task design 3",
    "task build 4 after design",
    "task content 2",
    "task test 3 after build, content",
    "task launch 1 after test",
)

PARALLEL = _p(
    "task start 1",
    "task short 1 after start",
    "task long 5 after start",
    "task end 1 after short, long",
)


def cmd_selftest():
    c = Checks()

    def has_rule(text, rule):
        return rule in [f["rule"] for f in validate(parse_plan(text))]

    # --- parsing -----------------------------------------------------------
    el = parse_plan(_p('title "Site"', "task design 3", "task build 4 after design",
                       "task test 2 after build"))
    c.eq([t["name"] for t in el["tasks"]], ["design", "build", "test"],
         "tasks parse in order")
    c.eq([t["duration"] for t in el["tasks"]], [3, 4, 2], "durations parse")
    c.eq(el["tasks"][1]["deps"], ["design"], "an after clause parses")
    c.eq(el["tasks"][2]["deps"], ["build"], "dependencies parse")
    c.eq(el["title"], "Site", "a title directive is unquoted")
    c.eq(el["tasks"][0]["line"], 2, "a task remembers its line")
    blank = parse_plan("; a comment\n# another\n\n")
    c.eq(blank["tasks"], [], "comment and blank lines are skipped")
    multi = parse_plan(_p("task a 1", "task b 1", "task c 1 after a, b"))
    c.eq(multi["tasks"][1]["deps"], [], "a task with no after has no dependencies")
    c.eq(multi["tasks"][2]["deps"], ["a", "b"], "an after list splits on commas")
    c.eq(parse_plan("task a 0")["tasks"][0]["duration"], 0,
         "a zero-duration milestone is allowed")

    bom = parse_plan("\ufefftask a 1\ntask b 1 after a")
    c.eq([t["name"] for t in bom["tasks"]], ["a", "b"],
         "a leading byte-order mark is tolerated")

    for bad, name in [
        (_p("task"), "a task with no name is refused"),
        (_p("task a"), "a task with no duration is refused"),
        (_p("task a b"), "a non-numeric duration is refused"),
        (_p("task a 1 after b,,c"), "an empty dependency is refused"),
        (_p("title"), "a title with no value is refused"),
        (_p("frobnicate a 1"), "an unknown line is refused"),
    ]:
        try:
            parse_plan(bad)
            c.ok(False, name)
        except LoomError:
            c.ok(True, name)

    # --- validation --------------------------------------------------------
    c.eq(validate(parse_plan(CLEAN)), [], "a clean plan has no findings")
    c.ok(has_rule(_p("task a 1 after ghost"), "unknown-dependency"),
         "an unknown dependency is reported")
    c.ok(has_rule(_p("task a 1", "task a 1"), "duplicate-task"),
         "a duplicate task is reported")
    c.ok(has_rule(_p("task a 1 after a"), "self-dependency"),
         "a self dependency is reported")
    c.ok(has_rule(_p("task a 1 after b", "task b 1 after a"), "cycle"),
         "a dependency cycle is reported")
    c.ok(has_rule("; empty\n", "no-tasks"),
         "a plan with no tasks is reported")

    both = [f["rule"] for f in validate(parse_plan(_p("task a 1 after x",
                                                       "task a 1")))]
    c.ok("duplicate-task" in both and "unknown-dependency" in both,
         "several findings are all reported")

    # --- scheduling --------------------------------------------------------
    sch = run_schedule(parse_plan(CLEAN))
    c.eq(sch["makespan"], 11, "makespan is the longest path")
    c.eq(sch["critical"], ["design", "build", "test", "launch"],
         "critical tasks are the zero-slack chain")
    c.eq(sch["schedule"]["design"]["start"], 0, "the first task starts at zero")
    c.eq(sch["schedule"]["build"]["start"], 3, "a task waits for a dependency")
    c.eq(sch["schedule"]["test"]["start"], 7,
         "a task waits for the latest dependency")
    c.eq(sch["schedule"]["content"]["slack"], 5, "an off-path task has slack")
    c.eq(sch["order"][0], "design", "the order starts with a source task")
    c.ok(sch["order"].index("design") < sch["order"].index("build"),
         "the order respects a dependency")
    c.eq(len(sch["order"]), 5, "every task appears in the order")

    par = run_schedule(parse_plan(PARALLEL))
    c.eq(par["makespan"], 7, "parallel branches take the longest path")
    c.eq(par["critical"], ["start", "long", "end"], "the long branch is critical")
    c.eq(par["schedule"]["short"]["slack"], 4, "the short branch has slack")

    one = run_schedule(parse_plan("task solo 5"))
    c.eq(one["makespan"], 5, "a single task schedules")
    c.eq(one["critical"], ["solo"], "a single task is critical")

    empty = run_schedule(parse_plan("; nothing\n"))
    c.eq(empty["makespan"], 0, "an empty plan has zero makespan")

    c.ok(all(sch["schedule"][n]["slack"] == 0 for n in sch["critical"]),
         "every critical task has zero slack")

    # --- exit codes --------------------------------------------------------
    def safe_code(text):
        try:
            plan = parse_plan(text)
        except LoomError:
            return 2
        if validate(plan):
            return 1
        return 1 if run_schedule(plan)["makespan"] is None else 0

    c.eq(safe_code(CLEAN), 0, "a clean plan exits 0")
    c.eq(safe_code(_p("task a 1 after ghost")), 1, "a plan with a finding exits 1")
    c.eq(safe_code(_p("task a b")), 2, "an unparseable plan exits 2")

    # --- chain -------------------------------------------------------------
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="loom-selftest-"))
    try:
        store = tmp / "ledger.jsonl"
        first = append_record(store, {"ts": "2026-01-01T00:00:00", "kind": "seal",
                                      "plan": "a", "plan_hash": "x"})
        c.eq(first["seq"], 0, "the first seal has seq 0")
        c.eq(first["prev"], GENESIS, "the first seal links to genesis")
        second = append_record(store, {"ts": "2026-01-02T00:00:00", "kind": "seal",
                                       "plan": "b", "plan_hash": "y"})
        c.eq(second["prev"], first["hash"], "the second seal links to the first hash")
        c.eq(verify_chain(load_records(store)), [], "a clean seal chain verifies")

        tampered = load_records(store)
        tampered[0]["plan"] = "forged"
        c.ok(any("hash" in p for p in verify_chain(tampered)),
             "editing a seal is caught")
        deleted = load_records(store)
        del deleted[0]
        c.ok(len(verify_chain(deleted)) > 0, "deleting the oldest seal is caught")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # --- render ------------------------------------------------------------
    demo = parse_plan(CLEAN)
    html = render_html("demo", demo, [], run_schedule(demo), "demo")
    c.ok("<svg" in html, "render emits an svg chart")
    c.ok("launch" in html, "render shows a task")
    c.ok("http://" not in html and "https://" not in html,
         "render contains no external URLs")
    c.ok("<script" not in html, "render contains no script")
    empty_html = render_html("empty", parse_plan(""), [], None, "empty")
    c.ok("Nothing scheduled" in empty_html, "render handles an empty plan")

    return c.report()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def extract_now(argv):
    now = None
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--now":
            if i + 1 >= len(argv):
                fail("--now needs a value")
            now = argv[i + 1]
            i += 2
            continue
        if a.startswith("--now="):
            now = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    return now, out


def build_parser():
    p = argparse.ArgumentParser(prog="loom", description="Plain-text project scheduling.")
    p.add_argument("--version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")

    def add(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--store", default=None)
        sp.add_argument("--version", action="version",
                        version="%s %s" % (TOOL, VERSION))
        return sp

    sp = add("check", "Validate the plan's structure.")
    sp.add_argument("plan")
    sp.add_argument("--json", action="store_true")

    sp = add("plan", "Schedule the tasks and report the critical path.")
    sp.add_argument("plan")
    sp.add_argument("--json", action="store_true")

    sp = add("order", "Print the topological order.")
    sp.add_argument("plan")

    sp = add("render", "Write a self-contained HTML report.")
    sp.add_argument("plan")
    sp.add_argument("--out", required=True)
    sp.add_argument("--title", default=None)

    sp = add("seal", "Record the schedule in the seal ledger.")
    sp.add_argument("plan")
    sp.add_argument("--note", default="")

    sp = add("history", "List recorded seals.")
    sp.add_argument("--json", action="store_true")

    sp = add("verify", "Verify the seal ledger hash chain.")
    sp.add_argument("--json", action="store_true")

    add("selftest", "Run the built-in checks.")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    now, argv = extract_now(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage(sys.stderr)
        return 2
    store = resolve_store(getattr(args, "store", None))
    try:
        if args.command == "check":
            return cmd_check(args.plan, args.json)
        if args.command == "plan":
            return cmd_plan(args.plan, args.json)
        if args.command == "order":
            return cmd_plan(args.plan, False, order_only=True)
        if args.command == "render":
            return cmd_render(args.plan, args.out, args.title)
        if args.command == "seal":
            return cmd_seal(args.plan, args.note, store, now)
        if args.command == "history":
            return cmd_history(args.json, store)
        if args.command == "verify":
            return cmd_verify(args.json, store)
        if args.command == "selftest":
            return cmd_selftest()
        fail("unknown command %r" % args.command)
    except LoomError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
