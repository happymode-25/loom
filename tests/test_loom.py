import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOL = Path(__file__).parent.parent / "loom.py"


def run(*args, store=None, env=None):
    cmd = [sys.executable, str(TOOL), *args]
    e = os.environ.copy()
    if env:
        e.update(env)
    if store:
        e["LOOM_STORE"] = str(store)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=e)
    return proc


CLEAN = textwrap.dedent("""\
    title "Website"
    task design 3
    task build 4 after design
    task content 2
    task test 3 after build, content
    task launch 1 after test
    """)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="loom-e2e-"))
        self.store = self.td / "ledger.jsonl"
        self.env_store = {"LOOM_STORE": str(self.store)}

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)


class TestSelfTest(Base):
    def test_selftest_passes(self):
        proc = run("selftest")
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestCLI(Base):
    def test_version(self):
        proc = run("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("loom", proc.stdout)

    def test_subcommand_version(self):
        proc = run("plan", "--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1.0.0", proc.stdout)

    def test_help(self):
        self.assertEqual(run("--help").returncode, 0)

    def test_unknown_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL), "frobnicate"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_no_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


class TestCheck(Base):
    def test_clean_exits_zero(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_unknown_dependency_exits_one(self):
        p = self.td / "plan.loom"
        p.write_text("task a 1 after ghost\n", encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 1)

    def test_no_tasks_exits_one(self):
        p = self.td / "plan.loom"
        p.write_text("; empty\n", encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no-tasks", proc.stdout)

    def test_duplicate_task_exits_one(self):
        p = self.td / "plan.loom"
        p.write_text("task a 1\ntask a 2\n", encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 1)

    def test_cycle_exits_one(self):
        p = self.td / "plan.loom"
        p.write_text("task a 1 after b\ntask b 1 after a\n", encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cycle", proc.stdout)

    def test_parse_error_exits_two(self):
        p = self.td / "plan.loom"
        p.write_text("frobnicate x\n", encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 2)

    def test_json_shape(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        proc = run("check", str(p), "--json")
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("findings", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["tasks"], 5)

    def test_reads_stdin(self):
        p = subprocess.Popen([sys.executable, str(TOOL), "check", "-"],
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             text=True)
        out, err = p.communicate(input=CLEAN)
        self.assertEqual(p.returncode, 0, err)

    def test_tolerates_utf8_bom(self):
        p = self.td / "plan.loom"
        p.write_text("\ufeff" + CLEAN, encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 0)

    def test_comments_and_blanks_are_skipped(self):
        p = self.td / "plan.loom"
        p.write_text("; comment\n\n# another\n" + CLEAN, encoding="utf-8")
        proc = run("check", str(p))
        self.assertEqual(proc.returncode, 0)


class TestPlan(Base):
    def test_plan_exits_zero_on_clean(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        proc = run("plan", str(p))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("makespan", proc.stdout)
        self.assertIn("critical", proc.stdout)

    def test_plan_with_findings_exits_one(self):
        p = self.td / "plan.loom"
        p.write_text("task a 1 after ghost\n", encoding="utf-8")
        proc = run("plan", str(p))
        self.assertEqual(proc.returncode, 1)

    def test_plan_json_has_fields(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        proc = run("plan", str(p), "--json")
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("order", data)
        self.assertIn("critical", data)
        self.assertIn("makespan", data)
        self.assertIn("schedule", data)
        self.assertEqual(data["makespan"], 11)
        self.assertEqual(data["critical"], ["design", "build", "test", "launch"])


class TestOrder(Base):
    def test_order_shows_path_and_critical(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        proc = run("order", str(p))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("design", proc.stdout)
        self.assertIn("launch", proc.stdout)
        self.assertIn("critical", proc.stdout)


class TestRender(Base):
    def test_render_writes_file(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        out = self.td / "report.html"
        proc = run("render", str(p), "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.exists())
        content = out.read_text(encoding="utf-8")
        self.assertIn("<svg", content)
        self.assertIn("design", content)
        self.assertIn("launch", content)
        self.assertNotIn("http://", content)
        self.assertNotIn("https://", content)
        self.assertNotIn("<script", content)

    def test_render_empty_plan_exits_zero_and_has_nothing(self):
        p = self.td / "plan.loom"
        p.write_text("; empty\n", encoding="utf-8")
        out = self.td / "report.html"
        proc = run("render", str(p), "--out", str(out))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Nothing scheduled", out.read_text(encoding="utf-8"))

    def test_render_broken_plan_still_exits_zero(self):
        p = self.td / "plan.loom"
        p.write_text("task a 1 after ghost\n", encoding="utf-8")
        out = self.td / "report.html"
        proc = run("render", str(p), "--out", str(out))
        self.assertEqual(proc.returncode, 0)
        content = out.read_text(encoding="utf-8")
        self.assertIn("findings", content.lower())

    def test_render_title_override(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        out = self.td / "report.html"
        proc = run("render", str(p), "--out", str(out), "--title", "Sprint Plan")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Sprint Plan", out.read_text(encoding="utf-8"))


class TestSealLedger(Base):
    def test_seal_clean(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        proc = run("seal", str(p), store=self.store)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.store.exists())
        self.assertIn("recorded as seq 0", proc.stdout)
        recs = self.store.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(recs), 1)
        rec = json.loads(recs[0])
        self.assertEqual(rec["plan"], "plan.loom")
        self.assertEqual(rec["tasks"], 5)
        self.assertEqual(rec["makespan"], 11)
        self.assertEqual(rec["critical"], 4)

    def test_seal_refuses_findings(self):
        p = self.td / "plan.loom"
        p.write_text("task a 1 after ghost\n", encoding="utf-8")
        proc = run("seal", str(p), store=self.store)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to seal", proc.stdout)

    def test_seal_refuses_cycle(self):
        p = self.td / "plan.loom"
        p.write_text("task a 1 after b\ntask b 1 after a\n", encoding="utf-8")
        proc = run("seal", str(p), store=self.store)
        self.assertEqual(proc.returncode, 1)

    def test_second_seal_is_seq_one(self):
        p1 = self.td / "a.loom"
        p1.write_text(CLEAN, encoding="utf-8")
        proc = run("seal", str(p1), store=self.store)
        self.assertEqual(proc.returncode, 0)
        p2 = self.td / "b.loom"
        p2.write_text("task x 1\ntask y 2 after x\n", encoding="utf-8")
        proc = run("seal", str(p2), store=self.store)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("recorded as seq 1", proc.stdout)

    def test_seal_deterministic_timestamp(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        run("seal", str(p), "--now", "2026-01-01T00:00:00", store=self.store)
        rec = json.loads(self.store.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["ts"], "2026-01-01T00:00:00")

    def test_seal_uses_env_store(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        proc = subprocess.run([sys.executable, str(TOOL), "seal", str(p)],
                              capture_output=True, text=True,
                              env={**os.environ, "LOOM_STORE": str(self.store)})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_history_lists_seals(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        run("seal", str(p), "--note", "January close", store=self.store)
        proc = run("history", store=self.store)
        self.assertIn("January close", proc.stdout)

    def test_history_json(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        run("seal", str(p), store=self.store)
        proc = run("history", "--json", store=self.store)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data), 1)


class TestVerify(Base):
    def test_verify_clean(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        run("seal", str(p), store=self.store)
        run("seal", str(p), store=self.store)
        proc = run("verify", store=self.store)
        self.assertEqual(proc.returncode, 0)

    def test_verify_empty(self):
        proc = run("verify", store=self.store)
        self.assertEqual(proc.returncode, 0)

    def test_verify_detects_an_edit(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        run("seal", str(p), store=self.store)
        text = self.store.read_text(encoding="utf-8").replace('"note":""', '"note":"forged"')
        self.store.write_text(text, encoding="utf-8")
        proc = run("verify", store=self.store)
        self.assertNotEqual(proc.returncode, 0)

    def test_verify_detects_a_deleted_middle(self):
        p = self.td / "plan.loom"
        p.write_text(CLEAN, encoding="utf-8")
        run("seal", str(p), store=self.store)
        run("seal", str(p), store=self.store)
        lines = self.store.read_text(encoding="utf-8").splitlines()
        self.store.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
        proc = run("verify", store=self.store)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
