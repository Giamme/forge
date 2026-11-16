"""`forge findings`: show a repository's review findings and record what happened to them.

The command is a subprocess. State and config homes are isolated; listings stay offline.
"""
import argparse
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "forge-findings.py"
LAUNCHER = ROOT / "forge"
sys.path.insert(0, str(ROOT / "scripts"))
import forge_registry  # noqa: E402
from forge_findings import tags  # noqa: E402

FP = "a" * 12
FP2 = "b" * 12
SHA = "abc1234"
BULLET = "- [P2][CONFIRMED] src/app.py:12 — The widget drops events when the list is empty."


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=os.environ)


class FindingsCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.config = self.tmp / "config"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        patched = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.state),
            "XDG_CONFIG_HOME": str(self.config),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        patched.start()
        self.addCleanup(patched.stop)
        os.environ.pop("FORGE_RUN_REGISTRY", None)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(self.repo, "remote", "add", "origin", "git@github.com:o/r.git")

    def plan(self, name, bullets, mtime=None):
        """A registered plan dir with one merged task, the shape `forge issues` collects."""
        run = self.tmp / "runs" / name
        task = run / "tasks" / "t"
        task.mkdir(parents=True)
        (run / "repo").write_text(str(self.repo) + "\n")
        (task / "status").write_text("MERGED\n")
        (task / "merged").write_text(SHA + "\n")
        (task / "qa_threshold").write_text("P1\n")
        (task / "known_issues.md").write_text("## Known issues\n\n" + "\n\n".join(bullets) + "\n")
        if mtime is not None:
            os.utime(run, (mtime, mtime))
        forge_registry.register_run_dir(self.repo, run)
        return run

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env=os.environ.copy(), timeout=60)

    def ok(self, *args):
        res = self.cli(*args)
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        return res

    def show(self, *args, repo=None):
        res = self.ok("show", str(repo or self.repo), *args, "--json")
        return json.loads(res.stdout)

    def record_path(self):
        return self.state / "forge" / "findings" / f"{forge_registry.repo_key(self.repo)}.record.json"

    def log_path(self):
        return self.record_path().with_name(f"{forge_registry.repo_key(self.repo)}.record.log.jsonl")

    def record(self):
        return json.loads(self.record_path().read_text())["findings"]

    def log_lines(self):
        path = self.log_path()
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def written(self):
        root = self.state / "forge" / "findings"
        if not root.is_dir():
            return []
        return sorted(path.name for path in root.iterdir() if not path.name.endswith(".lock"))

    def finding(self):
        self.plan("plan", [BULLET])
        rows = self.show()["findings"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["fp"]

    def test_show_lists_the_finding_open_with_default_tags_and_no_record(self):
        run = self.plan("plan", [BULLET])
        out = self.show()
        self.assertEqual(set(out), {"repo", "root", "key", "findings", "notes", "warnings"})
        self.assertEqual(out["repo"], "o/r")
        self.assertEqual(os.path.realpath(out["root"]), os.path.realpath(self.repo))
        self.assertEqual(out["key"], forge_registry.repo_key(self.repo))
        self.assertEqual(len(out["findings"]), 1)
        row = out["findings"][0]
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["record"])
        self.assertEqual(row["tags"], tags.default_tags(row["file"]))
        self.assertEqual(row["tags"], ["backend"])
        self.assertEqual(row["file"], "src/app.py")
        self.assertEqual(row["severity"], "P2")
        self.assertIn("widget drops events", row["summary"])
        # the plan dir names the same repository
        again = self.show(repo=run)
        self.assertEqual([item["fp"] for item in again["findings"]], [row["fp"]])
        text = self.ok("show", str(self.repo)).stdout.splitlines()
        self.assertEqual(len(text), 1)
        fp, status, severity, tag, place, summary = text[0].split("  ", 5)
        self.assertEqual((fp, status, severity, tag), (row["fp"], "open", "P2", "backend"))
        self.assertTrue(place == row["file"] or place.startswith(row["file"] + ":"))
        self.assertIn("widget drops events", summary)

    def test_fix_hides_it_from_open_and_reopen_brings_it_back(self):
        fp = self.finding()
        body = json.loads(self.ok("fix", str(self.repo), fp, "--commit", SHA, "--json").stdout)
        self.assertEqual(set(body), {"ok", "op", "changed", "unchanged"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["op"], "fix")
        self.assertEqual(body["unchanged"], [])
        self.assertEqual(body["changed"][0]["fp"], fp)
        self.assertIsNone(body["changed"][0]["before"])
        self.assertEqual(body["changed"][0]["after"]["status"], "fixed")
        self.assertEqual(body["changed"][0]["after"]["commit"], SHA)
        repeat = self.ok("fix", str(self.repo), fp, "--commit", SHA).stdout.strip()
        self.assertEqual(repeat, "fix: 0 changed, 1 unchanged")
        fixed = self.show()["findings"][0]
        self.assertEqual(fixed["status"], "fixed")
        self.assertEqual(fixed["record"]["commit"], SHA)
        self.assertEqual(self.show("--status", "open")["findings"], [])
        self.assertEqual([row["fp"] for row in self.show("--status", "fixed")["findings"]], [fp])
        self.ok("reopen", str(self.repo), fp)
        reopened = self.show("--status", "open")["findings"]
        self.assertEqual([row["fp"] for row in reopened], [fp])
        self.assertEqual(reopened[0]["status"], "open")
        self.assertEqual(self.record()[fp]["status"], "open")

    def test_open_means_open_and_to_file(self):
        fp = self.finding()
        self.ok("to-file", str(self.repo), fp, "--title", "File the widget")
        shown = self.show("--status", "open")["findings"]
        self.assertEqual([row["status"] for row in shown], ["to-file"])
        both = self.show("--status", "open", "--status", "fixed")
        self.assertEqual([row["fp"] for row in both["findings"]], [fp])
        self.assertEqual(self.show("--status", "dismissed")["findings"], [])

    def test_each_refusal_exits_2_or_3_and_writes_nothing(self):
        cases = (
            (2, ["dismiss", str(self.repo), FP]),
            (2, ["edit", str(self.repo), FP]),
            (2, ["edit", str(self.repo), FP, "--text-file", "-"]),
            (2, ["to-file", str(self.repo), FP, "--body-file", "-"]),
            (2, ["fix", str(self.repo)]),
            (3, ["fix", str(self.repo), "xyz", "--commit", SHA]),
            (3, ["fix", str(self.repo), "A" * 12, "--commit", SHA]),
            (3, ["fix", str(self.repo), FP]),
            (3, ["fix", str(self.repo), FP, "--commit", "NOPE"]),
            (3, ["dismiss", str(self.repo), FP, "--reason", "x" * 501]),
            (3, ["dup", str(self.repo), FP, "--of", FP]),
            (3, ["note", str(self.repo), "not-a-fingerprint", "hello"]),
            (3, ["tag", str(self.repo), FP, "--add", "Bad"]),
        )
        for code, args in cases:
            with self.subTest(args=args):
                before = self.written()
                res = self.cli(*args)
                self.assertIn(res.returncode, (2, 3), res.stderr)
                self.assertEqual(res.returncode, code, res.stderr or res.stdout)
                self.assertEqual(self.written(), before)
        res = self.cli("dup", str(self.repo), FP, "--of", FP, "--json")
        self.assertEqual(res.returncode, 3, res.stderr)
        body = json.loads(res.stdout)
        self.assertFalse(body["ok"])
        self.assertIn(body["error"], res.stderr)
        self.assertIn("duplicate of itself", body["error"])
        self.assertEqual(self.written(), [])

    def test_tag_add_then_remove_and_the_separator(self):
        fp = self.finding()
        self.ok("tag", str(self.repo), fp, "--add", "flaky", "hot")
        self.assertEqual(self.record()[fp]["tags"], ["flaky", "hot"])
        # stored tags replace the defaults; they are not merged with them
        self.assertEqual(self.show()["findings"][0]["tags"], ["flaky", "hot"])
        separated = self.ok("tag", str(self.repo), fp, "--", "extra", "--add", "--json")
        body = json.loads(separated.stdout)
        self.assertEqual(body["op"], "tag")
        self.assertEqual([item["fp"] for item in body["changed"]], [fp])
        self.assertEqual(self.record()[fp]["tags"], ["flaky", "hot", "extra"])
        self.ok("tag", str(self.repo), fp, "--remove", "hot", "extra")
        self.assertEqual(self.record()[fp]["tags"], ["flaky"])
        self.ok("tag", str(self.repo), fp, "--remove", "flaky")
        self.assertNotIn("tags", self.record()[fp])
        self.assertEqual(self.show()["findings"][0]["tags"], ["backend"])

    def test_note_sets_and_clears(self):
        fp = self.finding()
        self.ok("note", str(self.repo), fp, "look at the retry")
        self.assertEqual(self.record()[fp]["note"], "look at the retry")
        self.assertEqual(self.show()["findings"][0]["record"]["note"], "look at the retry")
        cleared = self.ok("note", str(self.repo), fp, "", "--json")
        body = json.loads(cleared.stdout)
        self.assertEqual(body["unchanged"], [])
        self.assertNotIn("note", self.record()[fp])

    def test_to_file_stores_the_draft(self):
        fp = self.finding()
        body_file = self.tmp / "body.md"
        body_file.write_text("The widget drops the second event.\n")
        text = self.ok("to-file", str(self.repo), fp, "--title", "Widget drops events",
                       "--body-file", str(body_file)).stdout
        self.assertIn("to-file: 1 changed, 0 unchanged", text)
        entry = self.record()[fp]
        self.assertEqual(entry["status"], "to-file")
        self.assertEqual(entry["draft"], {"title": "Widget drops events",
                                          "body": "The widget drops the second event.\n"})
        self.assertEqual(self.show()["findings"][0]["record"]["draft"]["title"], "Widget drops events")

    def test_actor_ui_is_recorded_in_the_record_and_its_log(self):
        fp = self.finding()
        self.ok("dismiss", str(self.repo), fp, "--reason", "noise", "--actor", "ui")
        self.assertEqual(self.record()[fp]["actor"], "ui")
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["actor"], "ui")
        self.assertEqual(lines[0]["fp"], fp)
        self.assertEqual(lines[0]["after"]["status"], "dismissed")
        # a flag after the tag remainder is still the actor of that write
        self.ok("tag", str(self.repo), fp, "--add", "flaky", "--actor", "triage")
        self.assertEqual(self.record()[fp]["actor"], "triage")
        self.assertEqual(self.log_lines()[-1]["actor"], "triage")

    def test_an_unlisted_fingerprint_is_recorded_and_show_does_not_invent_it(self):
        self.assertEqual(self.show()["findings"], [])
        self.ok("fix", str(self.repo), FP, "--reason", "fixed by hand")
        self.assertEqual(self.record()[FP]["status"], "fixed")
        self.assertEqual(self.record()[FP]["reason"], "fixed by hand")
        self.assertEqual(self.show()["findings"], [])

    def test_a_batch_is_one_change_and_a_repeat_is_unchanged(self):
        text = self.ok("dismiss", str(self.repo), FP, FP2, FP, "--reason", "bulk").stdout
        self.assertEqual(text.strip(), "dismiss: 2 changed, 0 unchanged")
        ats = {line["at"] for line in self.log_lines()}
        self.assertEqual(len(ats), 1)
        self.assertEqual(set(self.record()), {FP, FP2})
        again = json.loads(self.ok("dismiss", str(self.repo), FP, "--reason", "bulk", "--json").stdout)
        self.assertEqual(again["changed"], [])
        self.assertEqual(again["unchanged"], [FP])
        self.assertEqual(len(self.log_lines()), 2)

    def test_show_sorts_by_severity_then_newest(self):
        self.plan("old", ["- [P2][CONFIRMED] src/old.py:3 — Old leak."], mtime=1_700_000_000)
        self.plan("new", ["- [P0][CONFIRMED] src/crash.py:1 — Crash on start.",
                          "- [P2][CONFIRMED] src/new.py:4 — New leak."], mtime=1_800_000_000)
        rows = self.show()["findings"]
        self.assertEqual([row["summary"] for row in rows], ["Crash on start.", "New leak.", "Old leak."])
        text = [line.split("  ", 5)[-1] for line in self.ok("show", str(self.repo)).stdout.splitlines()]
        self.assertEqual(text, ["Crash on start.", "New leak.", "Old leak."])

    def test_dup_marks_the_other_finding(self):
        body = json.loads(self.ok("dup", str(self.repo), FP, "--of", FP2, "--json").stdout)
        self.assertEqual(body["op"], "dup")
        self.assertEqual(body["changed"][0]["after"]["status"], "duplicate")
        self.assertEqual(body["changed"][0]["after"]["duplicate_of"], FP2)
        self.assertEqual(self.record()[FP]["duplicate_of"], FP2)

    def test_edit_sets_severity_and_text_and_an_empty_file_clears_the_text(self):
        text = self.tmp / "edit.md"
        text.write_text("rewritten finding")
        self.ok("edit", str(self.repo), FP, "--severity", "P1", "--text-file", str(text))
        self.assertEqual(self.record()[FP]["edit"], {"severity": "P1", "text": "rewritten finding"})
        text.write_text("")
        self.ok("edit", str(self.repo), FP, "--text-file", str(text))
        self.assertEqual(self.record()[FP]["edit"], {"severity": "P1"})

    def test_a_missing_path_is_usage_and_a_corrupt_record_is_an_error(self):
        missing = self.cli("show", str(self.tmp / "missing"), "--json")
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn("not a repository or run dir", missing.stderr)
        self.assertFalse(json.loads(missing.stdout)["ok"])
        path = self.record_path()
        path.parent.mkdir(parents=True)
        path.write_text("{")
        broken = self.cli("show", str(self.repo), "--json")
        self.assertEqual(broken.returncode, 1, broken.stderr)
        self.assertFalse(json.loads(broken.stdout)["ok"])
        action = self.cli("reopen", str(self.repo), FP, "--json")
        self.assertEqual(action.returncode, 1, action.stderr)
        self.assertEqual(path.read_text(), "{")

    def test_the_parser_lists_these_commands_and_not_triage(self):
        spec = importlib.util.spec_from_file_location("forge_findings_cli_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        names = ["show", "fix", "dismiss", "reopen", "to-file", "dup", "tag", "note", "edit"]
        self.assertEqual(list(module.COMMANDS), names)
        parser = module.build_parser()
        choices = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        self.assertEqual(list(choices.choices), names)

    def test_launcher_help_lists_findings_and_memory_add_reaches_the_script(self):
        def forge(*args):
            return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True,
                                  env=os.environ.copy(), timeout=60)

        helped = forge("--help")
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("  findings  every review finding of a repository: show, mark, tag, triage\n", helped.stdout)
        command = forge("findings", "--help")
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertIn("{show,fix,dismiss,reopen,to-file,dup,tag,note,edit}", command.stdout)
        bare = forge("findings")
        self.assertEqual((bare.returncode, bare.stdout), (2, ""))
        self.assertEqual(bare.stderr, command.stdout)
        for name in ("add", "edit"):
            reached = forge("memory", name)
            self.assertEqual(reached.returncode, 2, reached.stderr)
            self.assertNotIn("unknown command", reached.stderr)
            self.assertIn(f"{name}:", reached.stderr)
        nope = forge("memory", "nope")
        self.assertEqual(nope.returncode, 2)
        self.assertIn("unknown command: nope", nope.stderr)
        self.assertIn("add or edit", nope.stderr)


if __name__ == "__main__":
    unittest.main()
