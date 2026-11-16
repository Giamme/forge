"""`forge findings triage`: start a read-only model, store what it proposes (#161).

No real model: most tests patch `forge_side.run_triage`; the detached ones drive the fake CLI
the way test_side_triage's TriageRunTests does. State, config and data homes are isolated.
"""
import contextlib
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_forge

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "forge-findings.py"
sys.path.insert(0, str(ROOT / "scripts"))
import forge_identity  # noqa: E402
import forge_registry  # noqa: E402
import forge_side  # noqa: E402
from forge_findings import triage  # noqa: E402

SHA = "abc1234"


def bullet(severity, text):
    return f"- [{severity}][CONFIRMED] src/app.py:12 — {text}"


BULLETS = [
    bullet("P3", "The label of the settings page has a typo in it."),
    bullet("P1", "The widget drops events when the list is empty."),
    bullet("P2", "The cache never expires an entry that was written twice."),
]

PLAN = {"tasks": [{
    "id": "guard-empty", "deps": [], "difficulty": "low", "risk": "low", "files": ["src/app.py"],
    "title": "Guard the empty list", "approach": "Return early in drain().",
    "prompt": "Guard the empty list in src/app.py.\n\n## Acceptance criteria\n\n- An empty list drops no event."}]}


def block(proposals):
    return "I looked at them.\n\n```forge-triage\n" + json.dumps({"proposals": proposals}) + "\n```\n"


def loaded_cli():
    spec = importlib.util.spec_from_file_location("forge_findings_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=os.environ)


def settle(proc):
    """The dead pid of a process that has ended and been reaped."""
    proc.wait()
    return proc.pid


class Base(unittest.TestCase):
    """A repository with three findings of different severity, an isolated state home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        patched = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.state), "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "FORGE_DATA_HOME": str(self.tmp / "data"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1"})
        patched.start()
        self.addCleanup(patched.stop)
        for name in ("FORGE_RUN_REGISTRY", "FORGE_TRIAGE"):
            os.environ.pop(name, None)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(self.repo, "remote", "add", "origin", "git@github.com:o/r.git")
        self.plans(BULLETS)

    def plans(self, bullets, start=1000.0):
        """One plan dir per bullet, each a minute newer than the one before."""
        for index, text in enumerate(bullets):
            run = self.tmp / "runs" / f"plan-{int(start)}-{index}"
            task = run / "tasks" / "t"
            task.mkdir(parents=True)
            (run / "repo").write_text(str(self.repo) + "\n")
            (task / "status").write_text("MERGED\n")
            (task / "merged").write_text(SHA + "\n")
            (task / "qa_threshold").write_text("P1\n")
            (task / "known_issues.md").write_text("## Known issues\n\n" + text + "\n")
            os.utime(run, (start + index * 60, start + index * 60))
            forge_registry.register_run_dir(self.repo, run)

    def ident(self):
        return forge_identity.identity(str(self.repo))

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env=env or os.environ.copy(), timeout=120)

    def rows(self):
        res = self.cli("show", str(self.repo), "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["findings"]

    def fp_of(self, severity):
        return next(row["fp"] for row in self.rows() if row["severity"] == severity)

    def dest(self, n=1):
        return forge_side.triage_home(self.ident()["key"]) / str(n)

    def read(self, name, n=1):
        return json.loads((self.dest(n) / name).read_text())


class InProcess(Base):
    """`main()` of the command, with the model and the doctor replaced."""

    def setUp(self):
        super().setUp()
        self.cli_module = loaded_cli()
        self.replies = []
        self.prompts = []
        self.calls = []
        for target in (patch.object(triage, "preflight", lambda spec: self.calls.append(("doctor", spec))),
                       patch.object(forge_side, "run_triage", self.fake_model)):
            target.start()
            self.addCleanup(target.stop)

    def fake_model(self, dest, repo, prompt, spec, timeout=3600):
        self.prompts.append(prompt)
        self.calls.append(("model", spec, timeout, str(repo)))
        reply = self.replies.pop(0)
        if isinstance(reply, dict):
            return reply
        return {"ok": True, "last": reply, "dir": str(dest), "reason": None}

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.cli_module.main([str(a) for a in args])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def triage(self, *extra):
        return self.main("triage", self.repo, "--spec", "sol:high", "--json", *extra)

    # --- a valid reply ---

    def test_a_valid_reply_is_stored_with_ids_hashes_and_pending_state(self):
        fp1, fp2, fp3 = self.fp_of("P1"), self.fp_of("P2"), self.fp_of("P3")
        proposals = [
            {"kind": "duplicate", "fp": fp3, "of": fp2, "why": "same cache defect"},
            {"kind": "fixed", "fp": fp1, "evidence": "drain() returns early now", "commit": "abc1234"},
            {"kind": "tag", "fps": [fp1, fp2], "tags": ["backend", "tests"]},
            {"kind": "issue", "fps": [fp2], "title": "Cache entries never expire", "body": "Details."},
            {"kind": "plan", "fps": [fp1], "title": "Guard", "request": "Guard the empty list.", "plan": PLAN},
        ]
        self.replies = [block(proposals)]
        code, out, err = self.triage()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"triage": 1, "state": "done", "proposals": 5})
        data = self.read("proposals.json")
        self.assertEqual((data["triage"], data["spec"]), (1, "sol:high"))
        self.assertRegex(data["at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual([p["id"] for p in data["proposals"]], ["p1", "p2", "p3", "p4", "p5"])
        for stored, original in zip(data["proposals"], proposals):
            self.assertEqual(stored["state"], "pending")
            self.assertEqual(stored["hash"], triage.proposal_hash(original))
            self.assertRegex(stored["hash"], r"^[0-9a-f]{12}$")
            self.assertEqual({k: v for k, v in stored.items() if k not in ("id", "hash", "state")}, original)
        state = self.read("state.json")
        self.assertEqual((state["state"], state["proposals"], state["findings"], state["spec"], state["actor"]),
                         ("done", 5, 3, "sol:high", "cli"))
        self.assertIn("finished", state)
        self.assertEqual((self.dest() / "done").read_text().strip(), "0")
        self.assertEqual(self.calls[0], ("doctor", "sol:high"))
        self.assertEqual(self.calls[1][2:], (3600, str(self.repo.resolve())))

    def test_the_prompt_states_the_contract(self):
        self.replies = [block([])]
        self.assertEqual(self.triage("--timeout", "77")[0], 0)
        prompt = self.prompts[0]
        self.assertEqual((self.dest() / "prompt.txt").read_text(), prompt)
        self.assertIn(str(self.dest() / "findings.json"), prompt)
        self.assertIn(str(self.repo.resolve()), prompt)
        for needle in ("read-only", "git log", "git show", "```forge-triage", "at most 200", "\"duplicate\"",
                       "\"fixed\"", "\"tag\"", "\"issue\"", "\"plan\"", "## Acceptance criteria", "forge-plan",
                       "7 to 40", "security"):
            self.assertIn(needle, prompt)
        self.assertEqual(self.calls[1][2], 77)

    def test_an_empty_list_is_a_valid_answer(self):
        self.replies = [block([])]
        code, out, _ = self.triage()
        self.assertEqual((code, json.loads(out)["proposals"]), (0, 0))
        self.assertEqual(self.read("proposals.json")["proposals"], [])

    # --- the one retry ---

    def test_an_invalid_reply_is_retried_once_with_a_reminder_quoting_the_error(self):
        fp = self.fp_of("P1")
        self.replies = [block([{"kind": "tag", "fps": ["f" * 12], "tags": ["ui"]}]),
                        block([{"kind": "tag", "fps": [fp], "tags": ["ui"]}])]
        code, out, _ = self.triage()
        self.assertEqual((code, json.loads(out)["state"]), (0, "done"))
        self.assertEqual(len(self.prompts), 2)
        self.assertNotIn("not usable", self.prompts[0])
        self.assertIn("Your last reply was not usable", self.prompts[1])
        self.assertIn("is not an fp of findings.json", self.prompts[1])
        self.assertTrue(self.prompts[1].startswith(self.prompts[0]))
        self.assertEqual(self.read("proposals.json")["proposals"][0]["fps"], [fp])

    def test_two_invalid_replies_fail_with_the_reason_and_no_proposals(self):
        self.replies = ["no block at all", block([{"kind": "nope"}])]
        code, out, _ = self.triage()
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual((result["state"], result["triage"]), ("failed", 1))
        self.assertTrue(result["reason"].startswith("invalid: proposal 1: kind must be one of"), result)
        self.assertFalse((self.dest() / "proposals.json").exists())
        state = self.read("state.json")
        self.assertEqual((state["state"], state["reason"]), ("failed", result["reason"]))
        self.assertEqual((self.dest() / "done").read_text().strip(), "1")
        self.assertEqual(len(self.prompts), 2)

    def test_no_block_twice_is_no_proposals_block(self):
        self.replies = ["prose", "more prose"]
        code, out, _ = self.triage()
        self.assertEqual((code, json.loads(out)["reason"]), (1, "no-proposals-block"))
        self.assertIn("no-proposals-block", self.prompts[1])

    def test_a_failed_dispatch_ends_with_its_reason_and_is_not_retried(self):
        self.replies = [{"ok": False, "last": None, "dir": None, "reason": "timeout"}]
        code, out, _ = self.triage()
        self.assertEqual((code, json.loads(out)["reason"]), (1, "timeout"))
        self.assertEqual(len(self.prompts), 1)
        self.assertFalse((self.dest() / "proposals.json").exists())

    def test_a_crash_in_the_job_is_a_failed_state(self):
        self.replies = []   # fake_model raises IndexError
        code, out, _ = self.triage()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["reason"], "error: IndexError")

    # --- findings.json ---

    def test_findings_json_holds_open_findings_by_severity_then_newest_and_the_cap(self):
        self.plans([bullet("P1", "A second, newer widget failure that nobody has seen."),
                    bullet("P2", "A dismissed one that nobody needs to look at.")], start=5000.0)
        dismissed = self.fp_of_text("dismissed one")
        self.assertEqual(self.cli("dismiss", str(self.repo), dismissed, "--reason", "no").returncode, 0)
        self.replies = [block([])]
        self.assertEqual(self.triage()[0], 0)
        found = self.read("findings.json")["findings"]
        self.assertEqual([row["severity"] for row in found], ["P1", "P1", "P2", "P3"])
        self.assertIn("newer widget", found[0]["summary"] if "summary" in found[0] else found[0]["text"])
        self.assertNotIn(dismissed, [row["fp"] for row in found])
        self.assertEqual({row["status"] for row in found}, {"open"})
        self.assertEqual(self.read("state.json")["findings"], 4)
        # The fps are the record keys `show` prints.
        self.assertEqual({row["fp"] for row in found}, {row["fp"] for row in self.rows() if row["fp"] != dismissed})
        self.replies = [block([])]
        self.assertEqual(self.triage("--max-findings", "2")[0], 0)
        capped = self.read("findings.json", 2)["findings"]
        self.assertEqual([row["fp"] for row in capped], [row["fp"] for row in found[:2]])
        self.assertEqual(self.read("state.json", 2)["findings"], 2)

    def fp_of_text(self, needle):
        return next(row["fp"] for row in self.rows() if needle in (row.get("summary") or row.get("text") or ""))

    def test_to_file_findings_are_read_too(self):
        fp = self.fp_of("P2")
        self.assertEqual(self.cli("to-file", str(self.repo), fp).returncode, 0)
        self.replies = [block([])]
        self.assertEqual(self.triage()[0], 0)
        self.assertIn(fp, [row["fp"] for row in self.read("findings.json")["findings"]])

    def test_max_findings_outside_1_to_2000_is_a_usage_error(self):
        for value in ("0", "2001", "-3"):
            code, _out, err = self.triage("--max-findings", value)
            self.assertEqual(code, 2, value)
            self.assertIn("--max-findings", err)
        self.assertEqual(self.calls, [])

    # --- refusals ---

    def test_a_running_triage_refuses_a_second_start(self):
        self.replies = [block([])]
        self.assertEqual(self.triage()[0], 0)
        dest = self.dest(1)
        state = self.read("state.json")
        (dest / "done").unlink()
        state.update(state="running", pid=os.getpid())
        (dest / "state.json").write_text(json.dumps(state))
        code, _out, err = self.triage()
        self.assertEqual(code, 3)
        self.assertIn("a triage is running: 1", err)
        self.assertFalse(self.dest(2).exists())

    def test_a_lost_triage_does_not_block_a_new_one(self):
        self.replies = [block([])]
        self.assertEqual(self.triage()[0], 0)
        state = self.read("state.json")
        (self.dest() / "done").unlink()
        state.update(state="running", pid=settle(subprocess.Popen(["true"])))
        (self.dest() / "state.json").write_text(json.dumps(state))
        self.replies = [block([])]
        self.assertEqual(self.triage()[0], 0)
        self.assertEqual(self.read("state.json", 2)["state"], "done")

    def test_the_cap_refuses(self):
        with patch.object(forge_side, "allocate_triage", side_effect=forge_side.TriageCapped("200 triage dirs")):
            code, _out, err = self.triage()
        self.assertEqual(code, 3)
        self.assertIn("200 triage dirs", err)

    def test_triage_allowed_reason_refuses(self):
        os.environ["FORGE_TRIAGE"] = "off"
        code, _out, err = self.triage()
        self.assertEqual(code, 3)
        self.assertIn("env", err)
        self.assertEqual(self.prompts, [])
        self.assertFalse(forge_side.triage_home(self.ident()["key"]).exists())

    def test_a_doctor_refusal_exits_3_with_its_message_and_writes_nothing(self):
        def refuse(spec):
            raise triage.Refused("doctor said no")
        with patch.object(triage, "preflight", refuse):
            code, _out, err = self.triage()
        self.assertEqual(code, 3)
        self.assertIn("doctor said no", err)
        self.assertFalse(forge_side.triage_home(self.ident()["key"]).exists())

    # --- reading ---

    def test_status_and_proposals_with_and_without_n(self):
        fp = self.fp_of("P1")
        self.replies = [block([{"kind": "tag", "fps": [fp], "tags": ["ui"]}]), block([])]
        self.assertEqual(self.triage()[0], 0)
        self.assertEqual(self.triage()[0], 0)
        code, out, _ = self.main("triage-status", self.repo, "--json")
        status = json.loads(out)
        self.assertEqual((code, status["triage"], status["state"], status["proposals"]), (0, 2, "done", 0))
        code, out, _ = self.main("triage-status", self.repo, 1, "--json")
        self.assertEqual((code, json.loads(out)["triage"], json.loads(out)["proposals"]), (0, 1, 1))
        code, out, _ = self.main("proposals", self.repo, 1, "--json")
        self.assertEqual((code, json.loads(out)["proposals"][0]["tags"]), (0, ["ui"]))
        code, out, _ = self.main("proposals", self.repo, 1)
        self.assertEqual(code, 0)
        self.assertIn("p1  ", out)
        self.assertIn("pending  tag", out)
        code, out, _ = self.main("proposals", self.repo, "--json")
        self.assertEqual((code, json.loads(out)["triage"], json.loads(out)["proposals"]), (0, 2, []))
        code, out, _ = self.main("triage-status", self.repo)
        self.assertEqual(code, 0)
        self.assertIn("triage 2: done", out)

    def test_nothing_to_read_or_an_unknown_n_exits_3(self):
        for args in (("triage-status", self.repo), ("proposals", self.repo)):
            self.assertEqual(self.main(*args)[0], 3)
        self.replies = [block([])]
        self.assertEqual(self.triage()[0], 0)
        self.assertEqual(self.main("triage-status", self.repo, 9)[0], 3)
        self.assertEqual(self.main("proposals", self.repo, 9)[0], 3)

    def test_proposals_exits_3_when_the_triage_stored_none(self):
        self.replies = ["prose", "prose"]
        self.assertEqual(self.triage()[0], 1)
        code, _out, err = self.main("proposals", self.repo)
        self.assertEqual(code, 3)
        self.assertIn("failed", err)

    def test_running_with_a_dead_pid_and_no_done_reads_lost(self):
        self.replies = [block([])]
        self.assertEqual(self.triage()[0], 0)
        state = self.read("state.json")
        (self.dest() / "done").unlink()
        for key in ("finished", "proposals"):
            state.pop(key, None)
        state.update(state="running", pid=settle(subprocess.Popen(["true"])))
        (self.dest() / "state.json").write_text(json.dumps(state))
        code, out, _ = self.main("triage-status", self.repo, "--json")
        status = json.loads(out)
        self.assertEqual((code, status["state"], status["reason"]), (0, "failed", "lost"))
        # A live pid is running.
        state["pid"] = os.getpid()
        (self.dest() / "state.json").write_text(json.dumps(state))
        self.assertEqual(json.loads(self.main("triage-status", self.repo, "--json")[1])["state"], "running")

    def test_the_state_is_running_while_the_model_works(self):
        seen = {}

        def watch(dest, repo, prompt, spec, timeout=3600):
            seen["state"] = json.loads((Path(dest) / "state.json").read_text())
            seen["done"] = (Path(dest) / "done").exists()
            return {"ok": True, "last": block([]), "dir": str(dest), "reason": None}

        with patch.object(forge_side, "run_triage", watch):
            self.assertEqual(self.triage()[0], 0)
        self.assertEqual((seen["state"]["state"], seen["state"]["pid"], seen["done"]), ("running", os.getpid(), False))
        self.assertEqual((seen["state"]["findings"], seen["state"]["spec"], seen["state"]["actor"]),
                         (3, "sol:high", "cli"))
        self.assertRegex(seen["state"]["started"], r"^\d{4}-")


class ValidationTests(unittest.TestCase):
    FPS = ["a" * 12, "b" * 12]

    def parse(self, proposals, fps=None):
        return triage.parse_proposals(block(proposals), self.FPS if fps is None else fps)

    def error(self, proposals):
        found, error = self.parse(proposals)
        self.assertIsNone(found)
        return error

    def test_an_unknown_fp_is_refused_everywhere_one_is_named(self):
        for item in ({"kind": "duplicate", "fp": "c" * 12, "of": "a" * 12, "why": "x"},
                     {"kind": "duplicate", "fp": "a" * 12, "of": "c" * 12, "why": "x"},
                     {"kind": "fixed", "fp": "c" * 12, "evidence": "x", "commit": None},
                     {"kind": "tag", "fps": ["a" * 12, "c" * 12], "tags": ["ui"]},
                     {"kind": "issue", "fps": ["c" * 12], "title": "t", "body": "b"},
                     {"kind": "plan", "fps": ["c" * 12], "title": "t", "request": "r", "plan": PLAN}):
            self.assertIn("is not an fp of findings.json", self.error([item]))

    def test_a_duplicate_of_itself_is_refused(self):
        self.assertIn("duplicate of itself",
                      self.error([{"kind": "duplicate", "fp": "a" * 12, "of": "a" * 12, "why": "x"}]))

    def test_unknown_kinds_and_wrong_keys_are_refused(self):
        self.assertIn("kind must be one of", self.error([{"kind": "delete", "fp": "a" * 12}]))
        self.assertIn("unexpected extra", self.error(
            [{"kind": "tag", "fps": ["a" * 12], "tags": ["ui"], "extra": 1}]))
        self.assertIn("missing why", self.error([{"kind": "duplicate", "fp": "a" * 12, "of": "b" * 12}]))
        self.assertIn("is not an object", self.error(["tag"]))

    def test_tags_must_match_and_be_at_most_eight(self):
        for bad in (["UI"], ["1ui"], ["a" * 25], ["has space"], [3], []):
            self.assertIn("tag", self.error([{"kind": "tag", "fps": ["a" * 12], "tags": bad}]))
        nine = [f"t{i}" for i in range(9)]
        self.assertIn("at most 8 tags", self.error([{"kind": "tag", "fps": ["a" * 12], "tags": nine}]))
        found, error = self.parse([{"kind": "tag", "fps": ["a" * 12], "tags": [f"t{i}" for i in range(7)] + ["a" * 24]}])
        self.assertIsNone(error)
        self.assertEqual(len(found[0]["tags"]), 8)   # eight, the last 24 characters long

    def test_lengths(self):
        fp = "a" * 12
        self.assertIn("title", self.error([{"kind": "issue", "fps": [fp], "title": "t" * 201, "body": "b"}]))
        self.assertIn("body", self.error([{"kind": "issue", "fps": [fp], "title": "t", "body": "b" * 20001}]))
        self.assertIn("why", self.error([{"kind": "duplicate", "fp": fp, "of": "b" * 12, "why": "w" * 2001}]))
        self.assertIn("evidence", self.error([{"kind": "fixed", "fp": fp, "evidence": "e" * 2001, "commit": None}]))
        found, error = self.parse([{"kind": "issue", "fps": [fp], "title": "t" * 200, "body": "b" * 20000},
                                   {"kind": "duplicate", "fp": fp, "of": "b" * 12, "why": "w" * 2000}])
        self.assertIsNone(error)
        self.assertEqual(len(found), 2)

    def test_a_non_hex_commit_is_dropped_not_refused(self):
        fp = "a" * 12
        cases = {"abc1234": "abc1234", "a" * 40: "a" * 40, "abc123": None, "a" * 41: None, "ABC1234": None,
                 "main": None, "zzzzzzz": None, 5: None, None: None, "": None}
        for given, wanted in cases.items():
            found, error = self.parse([{"kind": "fixed", "fp": fp, "evidence": "gone", "commit": given}])
            self.assertIsNone(error, given)
            self.assertEqual(found[0]["commit"], wanted, given)

    def test_a_plan_has_to_pass_the_planner(self):
        fp = "a" * 12

        def plan(**change):
            task = dict(PLAN["tasks"][0], **change)
            return {"kind": "plan", "fps": [fp], "title": "t", "request": "r", "plan": {"tasks": [task]}}

        found, error = self.parse([plan()])
        self.assertIsNone(error)
        self.assertEqual(found[0]["plan"], PLAN)
        self.assertIn("plan refused by the planner", self.error([plan(prompt="No criteria here.")]))
        self.assertIn("plan refused by the planner", self.error([plan(difficulty="trivial")]))
        self.assertIn("plan refused by the planner", self.error([plan(deps=["ghost"])]))
        bad = plan()
        bad["plan"] = {"tasks": []}
        self.assertIn("plan refused by the planner", self.error([bad]))
        bad["plan"] = "tasks"
        self.assertIn("plan must be an object", self.error([bad]))
        many = [dict(PLAN["tasks"][0], id=f"t{i}") for i in range(21)]
        bad["plan"] = {"tasks": many}
        self.assertIn("plan refused by the planner", self.error([bad]))

    def test_the_block_rules(self):
        fps = self.FPS
        parse = lambda reply: triage.parse_proposals(reply, fps)   # noqa: E731
        self.assertEqual(parse("no block")[1], triage.NO_BLOCK)
        self.assertEqual(parse(None)[1], triage.NO_BLOCK)
        self.assertEqual(parse("```json\n{\"proposals\": []}\n```")[1], triage.NO_BLOCK)
        two = block([]) + block([])
        self.assertIn("2 forge-triage blocks", parse(two)[1])
        self.assertIn("never closed", parse("```forge-triage\n{\"proposals\": []}\n")[1])
        self.assertIn("not valid JSON", parse("```forge-triage\n{nope\n```")[1])
        self.assertIn("exactly", parse("```forge-triage\n[]\n```")[1])
        self.assertIn("exactly", parse("```forge-triage\n{\"proposals\": [], \"x\": 1}\n```")[1])
        self.assertIn("duplicate key", parse("```forge-triage\n{\"proposals\": [], \"proposals\": []}\n```")[1])
        self.assertEqual(parse(block([]))[0], [])
        self.assertEqual(parse(block([]).replace("\n", "\r\n"))[0], [])

    def test_at_most_200_proposals(self):
        item = {"kind": "tag", "fps": ["a" * 12], "tags": ["ui"]}
        self.assertEqual(len(self.parse([item] * 200)[0]), 200)
        self.assertIn("at most 200 proposals", self.error([item] * 201))

    def test_reasons(self):
        self.assertEqual(triage.reason_of(triage.NO_BLOCK), "no-proposals-block")
        self.assertEqual(triage.reason_of("x broke"), "invalid: x broke")

    def test_the_hash_ignores_key_order_and_stored_fields_and_follows_content(self):
        one = {"kind": "tag", "fps": ["a" * 12], "tags": ["ui", "docs"]}
        two = {"tags": ["ui", "docs"], "fps": ["a" * 12], "kind": "tag"}
        self.assertEqual(triage.proposal_hash(one), triage.proposal_hash(two))
        stored = dict(one, id="p3", hash="x", state="applied", decided={"at": 1})
        self.assertEqual(triage.proposal_hash(stored), triage.proposal_hash(one))
        self.assertNotEqual(triage.proposal_hash(one), triage.proposal_hash(dict(one, tags=["docs", "ui"])))
        self.assertRegex(triage.proposal_hash(one), r"^[0-9a-f]{12}$")
        text = json.dumps(one, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        import hashlib
        self.assertEqual(triage.proposal_hash(one), hashlib.sha256(text.encode()).hexdigest()[:12])
        self.assertEqual(triage.proposal_hash({"kind": "issue", "title": "é"}),
                         triage.proposal_hash({"title": "é", "kind": "issue"}))


class DoctorTests(Base):
    def test_an_unknown_harness_is_refused_by_the_real_doctor(self):
        res = self.cli("triage", str(self.repo), "--spec", "nosuch:high")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("nosuch", res.stderr)
        self.assertIn("not a known alias", res.stderr)
        self.assertFalse(forge_side.triage_home(self.ident()["key"]).exists())

    def test_the_usage_errors(self):
        self.assertEqual(self.cli("triage", str(self.repo)).returncode, 2)
        self.assertEqual(self.cli("triage", str(self.tmp / "nowhere"), "--spec", "sol").returncode, 2)
        self.assertEqual(self.cli("triage-status", str(self.repo), "x").returncode, 2)


class FakeModel(unittest.TestCase):
    """The whole command, detached, against the fake CLI, borrowed the way test_side_triage does.

    Not a ForgeTests subclass: that class's own tests would run again.
    """

    git = test_forge.ForgeTests.git
    scenario = test_forge.ForgeTests.scenario

    def setUp(self):
        test_forge.ForgeTests.setUp(self)
        self.cache = self.root / "capability-cache"
        self.cache.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        for name, value in (("XDG_STATE_HOME", str(self.state)), ("XDG_CONFIG_HOME", str(self.root / "config")),
                            ("FORGE_DATA_HOME", str(self.root / "data")), ("FORGE_CAPABILITY_CACHE", str(self.cache)),
                            ("GIT_CONFIG_GLOBAL", os.devnull), ("GIT_CONFIG_NOSYSTEM", "1")):
            self.env[name] = value
        for name in ("FORGE_TRIAGE", "FORGE_RUN_REGISTRY", "FORGE_REGISTRY"):
            self.env.pop(name, None)
        self.env["FORGE_SIDE_TIMEOUT"] = "0"
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "git@github.com:o/r.git"],
                       check=True, env=self.env)
        run = self.root / "runs" / "plan"
        task = run / "tasks" / "t"
        task.mkdir(parents=True)
        (run / "repo").write_text(str(self.repo) + "\n")
        (task / "status").write_text("MERGED\n")
        (task / "merged").write_text(SHA + "\n")
        (task / "qa_threshold").write_text("P1\n")
        (task / "known_issues.md").write_text("## Known issues\n\n" + BULLETS[1] + "\n")
        with patch.dict(os.environ, self.env):
            forge_registry.register_run_dir(self.repo, run)
        self.addCleanup(self.stop_leftovers)

    def stop_leftovers(self):
        for done in (self.state / "forge" / "triage").glob("*/*/state.json"):
            try:
                pid = json.loads(done.read_text()).get("pid")
                if triage.alive(pid) and not (done.parent / "done").exists():
                    os.kill(pid, 15)
            except (OSError, ValueError):
                pass

    def forge(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                              env=self.env, timeout=120)

    def triage_dir(self):
        with patch.dict(os.environ, self.env):
            ident = forge_identity.identity(str(self.repo))
            return forge_side.triage_home(ident["key"]) / "1"

    def test_detach_returns_at_once_then_the_job_finishes_in_its_own_session(self):
        with patch.dict(os.environ, self.env):
            fp = json.loads(subprocess.run(
                [sys.executable, str(SCRIPT), "show", str(self.repo), "--json"], capture_output=True, text=True,
                env=self.env).stdout)["findings"][0]["fp"]
        reply = block([{"kind": "tag", "fps": [fp], "tags": ["backend"]}])
        self.scenario({"*.qa.*": {"findings": reply, "verdict": "none", "sleep": 4}})
        began = time.time()
        res = self.forge("triage", self.repo, "--spec", "sol", "--detach", "--json")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(json.loads(res.stdout), {"triage": 1, "state": "running"})
        self.assertLess(time.time() - began, 3.5)
        dest = self.triage_dir()
        for name in ("findings.json", "prompt.txt", "state.json"):
            self.assertTrue((dest / name).is_file(), name)
        self.assertFalse((dest / "done").exists())
        state = json.loads((dest / "state.json").read_text())
        self.assertEqual((state["state"], state["findings"], state["spec"]), ("running", 1, "sol"))
        self.assertNotEqual(state["pid"], os.getpid())
        self.assertTrue(triage.alive(state["pid"]))
        self.assertIn(fp, (dest / "findings.json").read_text())
        # Running: status says so, and a second start is refused.
        status = json.loads(self.forge("triage-status", self.repo, "--json").stdout)
        self.assertEqual(status["state"], "running")
        second = self.forge("triage", self.repo, "--spec", "sol", "--detach")
        self.assertEqual(second.returncode, 3)
        self.assertIn("a triage is running: 1", second.stderr)
        self.assertEqual(self.forge("proposals", self.repo).returncode, 3)
        limit = time.time() + 30
        while time.time() < limit and not (dest / "done").exists():
            time.sleep(0.1)
        self.assertTrue((dest / "done").exists(), (dest / "triage.out").read_text() if (dest / "triage.out").exists() else "")
        self.assertEqual((dest / "done").read_text().strip(), "0")
        final = json.loads((dest / "state.json").read_text())
        self.assertEqual((final["state"], final["proposals"]), ("done", 1))
        data = json.loads((dest / "proposals.json").read_text())
        self.assertEqual(data["proposals"][0]["tags"], ["backend"])
        self.assertEqual(data["proposals"][0]["hash"], triage.proposal_hash(data["proposals"][0]))
        listing = self.forge("proposals", self.repo, "--json")
        self.assertEqual((listing.returncode, json.loads(listing.stdout)["triage"]), (0, 1))
        # The dispatch ran read-only and never touched the repository.
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True,
                                        text=True, env=self.env).stdout, "")

    def test_the_foreground_run_with_the_fake_cli_and_a_bad_reply(self):
        self.scenario({"*.qa.*": {"findings": "I have no block.", "verdict": "none"}})
        res = self.forge("triage", self.repo, "--spec", "sol", "--json")
        self.assertEqual(res.returncode, 1, res.stderr + res.stdout)
        self.assertEqual(json.loads(res.stdout)["reason"], "no-proposals-block")
        self.assertEqual((self.triage_dir() / "done").read_text().strip(), "1")
        self.assertEqual(self.forge("triage-status", self.repo, "--json").returncode, 0)


if __name__ == "__main__":
    unittest.main()
