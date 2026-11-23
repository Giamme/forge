"""`forge findings apply` and `reject`: a triage's proposals go through the finding record (#161).

The triage dir is built by hand under an isolated state home; the command is a subprocess. The
planner is the real forge-planner.py. State, config and data homes are isolated.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_triage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from forge_findings import apply as approval, store, triage  # noqa: E402

PLAN = test_triage.PLAN


class ApplyBase(test_triage.Base):
    def setUp(self):
        super().setUp()
        self.fp1, self.fp2, self.fp3 = self.fp_of("P1"), self.fp_of("P2"), self.fp_of("P3")

    def make(self, proposals, n=1):
        """A finished triage dir with `proposals`, ids p1.., hashes from `proposal_hash`, all pending."""
        dest = self.dest(n)
        dest.mkdir(parents=True)
        stored = []
        for index, item in enumerate(proposals, 1):
            item = dict(item)
            item["id"] = f"p{index}"
            item["hash"] = triage.proposal_hash(item)
            item["state"] = "pending"
            stored.append(item)
        triage.write_json(dest / "state.json", {"state": "done", "spec": "sol:high", "proposals": len(stored)})
        triage.write_json(dest / "findings.json", {"triage": n, "findings": self.rows()})
        triage.write_json(dest / "proposals.json", {"triage": n, "spec": "sol:high", "at": "now", "proposals": stored})
        return stored

    def stored(self, n=1):
        return self.read("proposals.json", n)["proposals"]

    def apply(self, pid, hash_=None, n=1, *extra):
        if hash_ is None:
            hash_ = next(p["hash"] for p in self.stored(n) if p["id"] == pid)
        return self.cli("apply", str(self.repo), str(n), pid, "--approve", hash_, "--json", *extra)

    def entry(self, fp):
        return store.get(self.ident(), fp)

    def ok(self, res):
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        return json.loads(res.stdout)

    def refused(self, res, text=None):
        self.assertEqual(res.returncode, 3, res.stderr + res.stdout)
        self.assertFalse(json.loads(res.stdout)["ok"])
        if text:
            self.assertIn(text, res.stderr)

    def read_only_triage(self, n=1):
        """The triage dir stays readable, and proposals.json cannot be replaced."""
        dest = self.dest(n)
        (dest / approval.LOCK_NAME).touch()
        os.chmod(dest, 0o555)
        self.addCleanup(os.chmod, dest, 0o755)


class EffectTests(ApplyBase):
    def test_a_duplicate_is_recorded_by_triage_and_decided_by_the_asker(self):
        self.make([{"kind": "duplicate", "fp": self.fp3, "of": self.fp2, "why": "x" * 600}])
        out = self.ok(self.apply("p1", None, 1, "--actor", "ui"))
        self.assertEqual(out["changed"], [self.fp3])
        self.assertEqual(out["proposal"]["state"], "applied")
        entry = self.entry(self.fp3)
        self.assertEqual((entry["status"], entry["duplicate_of"], entry["actor"]), ("duplicate", self.fp2, "triage"))
        self.assertEqual(entry["reason"], "x" * 500)
        decided = self.stored()[0]["decided"]
        self.assertEqual(decided["actor"], "ui")
        self.assertIn("at", decided)

    def test_a_fixed_proposal_keeps_a_good_commit_and_cuts_the_evidence(self):
        self.make([{"kind": "fixed", "fp": self.fp1, "evidence": "e" * 700, "commit": "abc1234"},
                   {"kind": "fixed", "fp": self.fp2, "evidence": "gone now", "commit": "not-a-sha"},
                   {"kind": "fixed", "fp": self.fp3, "evidence": "gone too", "commit": None}])
        for pid in ("p1", "p2", "p3"):
            self.ok(self.apply(pid))
        first, second, third = (self.entry(fp) for fp in (self.fp1, self.fp2, self.fp3))
        self.assertEqual((first["status"], first["commit"], first["reason"]), ("fixed", "abc1234", "e" * 500))
        self.assertEqual((second["status"], second["reason"]), ("fixed", "gone now"))
        self.assertNotIn("commit", second)
        self.assertNotIn("commit", third)
        self.assertEqual(first["actor"], "triage")

    def test_tags_are_added_and_the_stored_ones_stay(self):
        self.assertEqual(self.cli("tag", str(self.repo), self.fp1, "--", "docs", "--add").returncode, 0)
        self.make([{"kind": "tag", "fps": [self.fp1, self.fp2], "tags": ["ui", "backend"]}])
        out = self.ok(self.apply("p1"))
        self.assertEqual(sorted(out["changed"]), sorted([self.fp1, self.fp2]))
        self.assertEqual(self.entry(self.fp1)["tags"], ["docs", "ui", "backend"])
        self.assertEqual(self.entry(self.fp2)["tags"], ["ui", "backend"])
        self.assertEqual(self.entry(self.fp2)["actor"], "triage")

    def test_an_issue_proposal_keeps_a_draft_and_never_files(self):
        self.make([{"kind": "issue", "fps": [self.fp1, self.fp2], "title": "Cache bugs", "body": "Both of them."}])
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(f"#!/bin/sh\necho called >> {self.tmp}/gh.called\nexit 1\n")
        (bin_dir / "gh").chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        proposal = self.stored()[0]
        res = self.cli("apply", str(self.repo), "1", "p1", "--approve", proposal["hash"], "--json", env=env)
        self.ok(res)
        for fp in (self.fp1, self.fp2):
            entry = self.entry(fp)
            self.assertEqual((entry["status"], entry["actor"]), ("to-file", "triage"))
            self.assertEqual(entry["draft"], {"title": "Cache bugs", "body": "Both of them."})
            self.assertNotIn("issue", entry)
        self.assertFalse((self.tmp / "gh.called").exists())

    def test_reject_records_the_decision_and_changes_no_finding(self):
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": ["ui"]}])
        res = self.cli("reject", str(self.repo), "1", "p1", "--reason", "not now", "--actor", "ui", "--json")
        out = self.ok(res)
        self.assertEqual(out["changed"], [])
        item = self.stored()[0]
        self.assertEqual(item["state"], "rejected")
        self.assertEqual((item["decided"]["actor"], item["decided"]["reason"]), ("ui", "not now"))
        self.assertIsNone(self.entry(self.fp1))
        self.refused(self.apply("p1"), "rejected, not pending")

    def test_a_long_reject_reason_is_refused(self):
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": ["ui"]}])
        self.refused(self.cli("reject", str(self.repo), "1", "p1", "--reason", "r" * 501, "--json"))
        self.assertEqual(self.stored()[0]["state"], "pending")

    def test_a_reject_then_apply_and_an_apply_then_reject_are_refused(self):
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": ["ui"]},
                   {"kind": "tag", "fps": [self.fp2], "tags": ["ui"]}])
        self.ok(self.cli("reject", str(self.repo), "1", "p1", "--json"))
        self.refused(self.apply("p1"))
        self.assertIsNone(self.entry(self.fp1))
        self.ok(self.apply("p2"))
        self.refused(self.cli("reject", str(self.repo), "1", "p2", "--json"), "applied, not pending")
        self.assertEqual(self.stored()[1]["state"], "applied")


class RefusalTests(ApplyBase):
    def test_a_wrong_hash_and_an_edited_proposal_change_nothing(self):
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": ["ui"]}])
        before = (self.dest() / "proposals.json").read_text()
        self.refused(self.apply("p1", "0" * 12), "hash")
        self.refused(self.apply("p1", "ABCDEF123456"), "hash")
        data = json.loads(before)
        data["proposals"][0]["tags"] = ["security"]   # edited after the person read it; the stored hash is stale
        stale = data["proposals"][0]["hash"]
        triage.write_json(self.dest() / "proposals.json", data)
        self.refused(self.apply("p1", stale), "hash")
        self.assertIsNone(self.entry(self.fp1))
        self.assertEqual(self.stored()[0]["state"], "pending")

    def test_unknown_triage_and_proposal_are_refused(self):
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": ["ui"]}])
        self.refused(self.apply("p9", "0" * 12), "no proposal p9")
        self.refused(self.apply("p1", "0" * 12, 7), "no triage 7")
        self.refused(self.apply("x1", "0" * 12), "not a proposal id")
        self.refused(self.cli("reject", str(self.repo), "1", "p9", "--json"), "no proposal p9")
        self.refused(self.cli("reject", str(self.repo), "4", "p1", "--json"), "no triage 4")

    def test_a_second_apply_is_refused_and_changes_nothing_more(self):
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": ["ui"]}])
        self.ok(self.apply("p1"))
        stamp = self.entry(self.fp1)["at"]
        self.refused(self.apply("p1"), "applied, not pending")
        self.assertEqual(self.entry(self.fp1)["at"], stamp)

    def test_a_finding_that_is_not_in_show_is_refused(self):
        ghost = "f" * 12
        self.make([{"kind": "duplicate", "fp": self.fp1, "of": ghost, "why": "same"},
                   {"kind": "fixed", "fp": ghost, "evidence": "gone", "commit": None},
                   {"kind": "tag", "fps": [self.fp1, ghost], "tags": ["ui"]}])
        for pid in ("p1", "p2", "p3"):
            self.refused(self.apply(pid), ghost)
        self.assertIsNone(self.entry(self.fp1))
        self.assertEqual({p["state"] for p in self.stored()}, {"pending"})

    def test_a_self_duplicate_is_refused(self):
        self.make([{"kind": "duplicate", "fp": self.fp1, "of": self.fp1, "why": "same"}])
        self.refused(self.apply("p1"), "duplicate of itself")
        self.assertIsNone(self.entry(self.fp1))
        self.assertEqual(self.stored()[0]["state"], "pending")

    def test_a_record_refusal_leaves_the_proposal_pending(self):
        many = ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"]
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": many}, {"kind": "tag", "fps": [self.fp1], "tags": ["t9"]}])
        self.ok(self.apply("p1"))
        self.refused(self.apply("p2"), "at most 8 tags")
        self.assertEqual(self.stored()[1]["state"], "pending")
        self.assertNotIn("t9", self.entry(self.fp1)["tags"])

    @unittest.skipIf(os.geteuid() == 0, "root can write a read-only directory")
    def test_an_unwritable_proposals_file_leaves_the_record_unchanged(self):
        self.assertEqual(self.cli("tag", str(self.repo), self.fp2, "--", "docs", "--add").returncode, 0)
        self.make([{"kind": "tag", "fps": [self.fp1], "tags": ["ui"]}])
        self.read_only_triage()
        res = self.apply("p1")
        self.assertEqual(res.returncode, 1, res.stderr + res.stdout)
        self.assertIn("proposals.json", res.stderr)
        self.assertEqual(self.stored()[0]["state"], "pending")
        self.assertNotIn("decided", self.stored()[0])
        self.assertIsNone(self.entry(self.fp1))
        self.assertEqual(self.entry(self.fp2)["tags"], ["docs"])


class PlanTests(ApplyBase):
    def plan_proposal(self, plan=None):
        return {"kind": "plan", "fps": [self.fp1, self.fp2], "title": "Guard", "request": "Guard the empty list.",
                "plan": PLAN if plan is None else plan}

    def default_dir(self, pid="p1", n=1):
        return Path(os.environ["XDG_STATE_HOME"]) / "forge" / "plans" / self.ident()["key"] / f"triage-{n}-{pid}"

    def test_a_good_plan_becomes_a_parsed_plan_dir_and_dispatches_nothing(self):
        self.make([self.plan_proposal()])
        self.assertEqual(self.cli("note", str(self.repo), self.fp2, "kept note").returncode, 0)
        out = self.ok(self.apply("p1"))
        target = self.default_dir()
        self.assertEqual(out["plan_dir"], str(target))
        self.assertTrue(out["next"].startswith(f"bash scripts/forge-parallel.sh plan {target} --repo "))
        self.assertIn(self.repo.name, out["next"])
        request = (target / "request.md").read_text()
        self.assertTrue(request.startswith("Guard the empty list.\n"))
        self.assertIn(f"- {self.fp1} [P1] src/app.py — The widget drops events when the list is empty.", request)
        self.assertIn(f"- {self.fp2} [P2]", request)
        self.assertIn("guard-empty", (target / "tasks.tsv").read_text())
        prompt = (target / "tasks" / "guard-empty" / "prompt.md").read_text()
        self.assertIn("## Acceptance criteria", prompt)
        self.assertIn("## The whole request", prompt)
        self.assertIn("Guard the empty list.", prompt.split("## The whole request")[1])
        self.assertEqual(sorted(p.name for p in target.iterdir() if not p.name.startswith(".")),
                         ["request.md", "tasks", "tasks.tsv"])
        self.assertEqual(self.stored()[0]["plan_dir"], str(target))
        self.assertEqual(self.stored()[0]["state"], "applied")
        self.assertEqual(self.entry(self.fp1)["note"], f"in plan {target}")
        self.assertEqual(self.entry(self.fp2)["note"], "kept note")
        self.assertEqual(out["changed"], [self.fp1])
        self.assertNotIn("status", self.entry(self.fp1), "a plan note records no status: a swept finding stays swept")

    def test_a_given_plan_dir_is_used_and_an_existing_one_is_refused(self):
        self.make([self.plan_proposal(), self.plan_proposal()])
        target = self.tmp / "my-plan"
        self.ok(self.apply("p1", None, 1, "--plan-dir", str(target)))
        self.assertTrue((target / "tasks.tsv").is_file())
        self.refused(self.apply("p2", None, 1, "--plan-dir", str(target)), "exists")
        self.assertEqual(self.stored()[1]["state"], "pending")
        taken = self.tmp / "taken"
        taken.mkdir()
        (taken / "mine.txt").write_text("keep")
        self.refused(self.apply("p2", None, 1, "--plan-dir", str(taken)), "exists")
        self.assertEqual((taken / "mine.txt").read_text(), "keep")

    def test_a_planner_refusal_leaves_no_dir_and_the_proposal_pending(self):
        bad = copy.deepcopy(PLAN)
        bad["tasks"][0]["prompt"] = "Guard the empty list in src/app.py."
        self.make([self.plan_proposal(bad)])
        res = self.apply("p1")
        self.refused(res, "Acceptance criteria")
        self.assertFalse(self.default_dir().exists())
        self.assertEqual(self.stored()[0]["state"], "pending")
        self.assertNotIn("plan_dir", self.stored()[0])
        self.assertIsNone(self.entry(self.fp1))

    @unittest.skipIf(os.geteuid() == 0, "root can write a read-only directory")
    def test_an_unwritable_proposals_file_leaves_no_plan_and_keeps_notes(self):
        self.assertEqual(self.cli("note", str(self.repo), self.fp2, "kept note").returncode, 0)
        self.make([self.plan_proposal()])
        self.read_only_triage()
        res = self.apply("p1")
        self.assertEqual(res.returncode, 1, res.stderr + res.stdout)
        self.assertIn("proposals.json", res.stderr)
        self.assertFalse(self.default_dir().exists())
        item = self.stored()[0]
        self.assertEqual(item["state"], "pending")
        self.assertNotIn("plan_dir", item)
        self.assertNotIn("decided", item)
        self.assertIsNone(self.entry(self.fp1))
        self.assertEqual(self.entry(self.fp2)["note"], "kept note")

    def test_a_note_set_while_the_plan_note_is_chosen_is_kept(self):
        """A note that lands after the 'no note yet' read is not replaced by `in plan`."""
        self.make([self.plan_proposal()])
        ident = self.ident()
        text = "from the person"
        started = threading.Event()
        errors = []
        real_get = store.get

        def writer():
            try:
                if not started.wait(30):
                    errors.append("the plan apply never read the finding")
                    return
                if not store.set_note(ident, self.fp1, text, "cli"):
                    errors.append("the note was not written")
            except BaseException as exc:
                errors.append(repr(exc))

        def get_then_let_the_note_in(ident_, fp):
            entry = real_get(ident_, fp)
            if fp == self.fp1 and not started.is_set():
                started.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    if (real_get(ident_, fp) or {}).get("note") == text:
                        break
                    time.sleep(0.02)
            return entry

        proposal = self.stored()[0]
        rows = self.rows()
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch.object(store, "get", get_then_let_the_note_in):
                approval.apply(ident, 1, "p1", proposal["hash"], lambda: rows)
        finally:
            started.set()
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), errors)
        self.assertEqual(errors, [])
        self.assertEqual(self.stored()[0]["state"], "applied")
        self.assertEqual(self.entry(self.fp1)["note"], text)
        self.assertEqual(self.entry(self.fp2)["note"], f"in plan {self.default_dir()}")

    def test_the_plan_dir_can_be_planned_by_the_next_command(self):
        self.make([self.plan_proposal()])
        out = self.ok(self.apply("p1"))
        # What `plan` reads first is what the planner wrote: the same tsv header, the task, the request.
        lines = (Path(out["plan_dir"]) / "tasks.tsv").read_text().splitlines()
        self.assertTrue(lines[0].startswith("# id\tdeps"))
        self.assertTrue(lines[1].startswith("guard-empty\t-\tlow\tsrc/app.py"))


class ConcurrencyTests(ApplyBase):
    def test_two_concurrent_approvals_apply_once(self):
        self.make([{"kind": "issue", "fps": [self.fp1], "title": "T", "body": "B"}])
        hash_ = self.stored()[0]["hash"]
        argv = [sys.executable, str(test_triage.SCRIPT), "apply", str(self.repo), "1", "p1", "--approve", hash_, "--json"]
        procs = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        codes = []
        for proc in procs:
            proc.communicate(timeout=120)
            codes.append(proc.returncode)
        self.assertEqual(sorted(codes), [0, 3, 3, 3])
        self.assertEqual(self.stored()[0]["state"], "applied")
        self.assertEqual(self.entry(self.fp1)["status"], "to-file")


if __name__ == "__main__":
    unittest.main()
