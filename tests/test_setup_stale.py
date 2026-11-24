"""Tests for stale setup tracking and --setup-before-verify (#191)."""

from __future__ import annotations

import dataset_isolation  # noqa: F401
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import test_forge as fixtures
except ImportError:
    fixtures = None

ROOT = Path(__file__).resolve().parents[1]
STALE_SCRIPT = ROOT / "scripts" / "forge-setup-stale.py"

GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@t",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@t",
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_NOSYSTEM="1",
)


def _load_stale_module():
    spec = importlib.util.spec_from_file_location("forge_setup_stale", STALE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


class SetupStaleUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.plan = self.root / "plan"
        self.plan.mkdir()
        self._git("init", "-q")
        _write_lf(self.repo / "README.md", "init\n")
        self._git("add", ".")
        self._git("commit", "-qm", "init")
        self.mod = _load_stale_module()

    def _git(self, *args: str) -> str:
        res = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=GIT_ENV,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return res.stdout.decode("utf-8", "surrogateescape").strip()

    def _commit(self, rel: str, content: str, msg: str = "update") -> str:
        _write_lf(self.repo / rel, content)
        self._git("add", rel)
        self._git("commit", "-qm", msg)
        return self._git("rev-parse", "HEAD")

    def _check(self, setup_commit_path: str | None = None) -> tuple[str, str]:
        argv = [sys.executable, str(STALE_SCRIPT), "check", str(self.plan), str(self.repo), str(self.repo)]
        if setup_commit_path:
            argv.extend(["--setup-commit", setup_commit_path])
        res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        out = res.stdout.decode("utf-8", "surrogateescape").rstrip("\r\n")
        status, _, reason = out.partition("\t")
        return status, reason

    def test_record_and_ok_when_head_matches_setup_commit(self):
        marker = self.plan / "tasks" / "_integration" / "setup.commit"
        subprocess.run(
            [sys.executable, str(STALE_SCRIPT), "record-commit", str(self.repo), "--out", str(marker)],
            check=True,
        )
        head = self._git("rev-parse", "HEAD")
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), head)
        self.assertEqual((self.repo / ".git" / "forge-setup-commit").read_text(encoding="utf-8").strip(), head)
        self.assertEqual(self._check(str(marker)), ("OK", ""))

    def test_without_flag_unrelated_is_ok_and_lockfile_is_stale(self):
        subprocess.run(
            [sys.executable, str(STALE_SCRIPT), "record-commit", str(self.repo)],
            check=True,
        )
        self._commit("src/app.py", "print('ok')\n")
        self.assertEqual(self._check(), ("OK", ""))

        self._commit("package-lock.json", '{"lockfileVersion": 3}\n')
        status, reason = self._check()
        self.assertEqual(status, "STALE")
        self.assertIn("package-lock.json", reason)

    def test_with_flag_and_no_setup_inputs_any_commit_triggers_rerun(self):
        subprocess.run(
            [sys.executable, str(STALE_SCRIPT), "record-commit", str(self.repo)],
            check=True,
        )
        _write_lf(self.plan / "setup_before_verify", "on\n")
        self._commit("src/app.py", "print('v2')\n")
        status, reason = self._check()
        self.assertEqual(status, "RERUN")
        self.assertIn("src/app.py", reason)

    def test_with_setup_inputs_filters_rerun_and_warns_when_off(self):
        _write_lf(self.repo / ".forge" / "setup-inputs", "# declared inputs\nschema/*.prisma\ncodegen/\nMakefile\n")
        self._git("add", ".forge/setup-inputs")
        self._git("commit", "-qm", "add setup-inputs")
        subprocess.run(
            [sys.executable, str(STALE_SCRIPT), "record-commit", str(self.repo)],
            check=True,
        )

        # Unrelated change: neither RERUN (when flag on) nor STALE (when flag off).
        self._commit("src/helper.py", "x = 1\n")
        _write_lf(self.plan / "setup_before_verify", "on\n")
        self.assertEqual(self._check(), ("OK", ""))
        (self.plan / "setup_before_verify").unlink()
        self.assertEqual(self._check(), ("OK", ""))

        # Declared glob change triggers RERUN when on, STALE when off.
        self._commit("schema/user.prisma", "model User {}\n")
        _write_lf(self.plan / "setup_before_verify", "on\n")
        status, reason = self._check()
        self.assertEqual(status, "RERUN")
        self.assertIn("schema/user.prisma", reason)

        (self.plan / "setup_before_verify").unlink()
        status, reason = self._check()
        self.assertEqual(status, "STALE")
        self.assertIn("schema/user.prisma", reason)

    def test_integ_marker_fallback_when_git_dir_marker_absent(self):
        marker = self.plan / "tasks" / "_integration" / "setup.commit"
        subprocess.run(
            [sys.executable, str(STALE_SCRIPT), "record-commit", str(self.repo), "--out", str(marker)],
            check=True,
        )
        (self.repo / ".git" / "forge-setup-commit").unlink()
        self._commit("Cargo.lock", "[[package]]\n")
        status, reason = self._check(str(marker))
        self.assertEqual(status, "STALE")
        self.assertIn("Cargo.lock", reason)


@unittest.skipIf(fixtures is None, "requires POSIX test_forge fixtures")
class SetupStaleRunTests(unittest.TestCase):
    if fixtures is not None:
        setUp = fixtures.ForgeTests.setUp
        git = fixtures.ForgeTests.git
        run_script = fixtures.ForgeTests.run_script
        scenario = fixtures.ForgeTests.scenario

    def _init_setup_project(self, setup_inputs: str | None = None):
        _write_lf(self.repo / ".gitignore", "generated/\n")
        _write_lf(self.repo / "package-lock.json", "v1\n")
        _write_lf(self.repo / "app.txt", "base\n")
        setup_script = (
            "#!/bin/sh\n"
            "mkdir -p generated\n"
            "cat package-lock.json > generated/deps.txt\n"
            "echo setup >> generated/count.log\n"
        )
        p_setup = self.repo / ".forge" / "setup"
        _write_lf(p_setup, setup_script)
        p_setup.chmod(0o755)
        if setup_inputs is not None:
            _write_lf(self.repo / ".forge" / "setup-inputs", setup_inputs)
        self.git("add", ".gitignore", "package-lock.json", "app.txt", ".forge")
        self.git("commit", "-qm", "setup fixture")

    def _make_script_task(self, plan: Path, tid: str, deps: str, files: str, body: str, goal: str):
        tdir = plan / "tasks" / tid
        tdir.mkdir(parents=True, exist_ok=True)
        _write_lf(tdir / "prompt.md", f"Implement {goal}\n")
        run_sh = tdir / "run"
        _write_lf(run_sh, "#!/bin/sh\nset -e\n" + body + "\n")
        run_sh.chmod(0o755)
        tsv = plan / "tasks.tsv"
        existing = tsv.read_text(encoding="utf-8") if tsv.exists() else ""
        _write_lf(tsv, existing + f"{tid}\t{deps}\tlow\t{files}\tscript\topus\t{goal}\n")

    def test_stale_setup_warns_then_verify_with_flag_passes(self):
        self._init_setup_project()
        self.scenario({})
        p = self.root / "plan"
        p.mkdir()
        self._make_script_task(
            p,
            "t1",
            "-",
            "package-lock.json",
            "printf 'v2\\n' > package-lock.json",
            "bump lockfile",
        )
        r_plan = self.run_script("forge-parallel.sh", "plan", p, "--repo", self.repo, "--no-memory")
        self.assertEqual(r_plan.returncode, 0, r_plan.stdout)

        verify_cmd = "grep -qx v2 generated/deps.txt"
        r_run = self.run_script("forge-parallel.sh", "run", p, "--verify", verify_cmd, "--verify-retries", "0")
        self.assertNotEqual(r_run.returncode, 0, r_run.stdout)
        self.assertEqual((p / "verification.status").read_text(encoding="utf-8").strip(), "FAIL")
        self.assertTrue((p / "verification.stale_setup").is_file(), r_run.stdout)
        self.assertIn("package-lock.json", (p / "verification.stale_setup").read_text(encoding="utf-8"))
        self.assertIn("stale setup", r_run.stdout)

        # Re-running verify without flag still warns.
        r_v1 = self.run_script("forge-parallel.sh", "verify", p)
        self.assertEqual(r_v1.returncode, 5, r_v1.stdout)
        self.assertIn("stale setup", r_v1.stdout)

        # Re-running verify with --setup-before-verify refreshes setup and passes.
        r_v2 = self.run_script("forge-parallel.sh", "verify", p, "--setup-before-verify")
        self.assertEqual(r_v2.returncode, 0, r_v2.stdout)
        self.assertEqual((p / "verification.status").read_text(encoding="utf-8").strip(), "PASS")
        self.assertFalse((p / "verification.stale_setup").exists())

    def test_setup_inputs_avoids_redundant_rerun_when_only_unrelated_file_merges(self):
        self._init_setup_project(setup_inputs="package-lock.json\n")
        self.scenario({})
        p = self.root / "plan-inputs"
        p.mkdir()
        self._make_script_task(
            p,
            "t1",
            "-",
            "app.txt",
            "printf 'updated\\n' > app.txt",
            "edit unrelated app",
        )
        r_plan = self.run_script(
            "forge-parallel.sh", "plan", p, "--repo", self.repo, "--no-memory", "--setup-before-verify"
        )
        self.assertEqual(r_plan.returncode, 0, r_plan.stdout)
        self.assertTrue((p / "setup_before_verify").is_file())

        r_run = self.run_script(
            "forge-parallel.sh", "run", p, "--verify", "grep -qx v1 generated/deps.txt", "--verify-retries", "0"
        )
        self.assertEqual(r_run.returncode, 0, r_run.stdout)
        int_wt = Path((p / "wt_root").read_text(encoding="utf-8").strip()) / "_integration"
        count_lines = (int_wt / "generated" / "count.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(count_lines), 1, f"expected setup once at creation, got {count_lines}")

    def test_run_with_setup_before_verify_and_lanes_refreshes_before_lane(self):
        self._init_setup_project(setup_inputs="package-lock.json\n")
        lane = self.repo / ".forge" / "verify.d" / "deps"
        _write_lf(lane, "grep -qx v2 generated/deps.txt\n")
        self.git("add", ".forge/verify.d/deps")
        self.git("commit", "-qm", "add verify lane")

        self.scenario({})
        p = self.root / "plan-lane"
        p.mkdir()
        self._make_script_task(
            p,
            "t1",
            "-",
            "package-lock.json",
            "printf 'v2\\n' > package-lock.json",
            "bump lockfile for lane",
        )
        r_plan = self.run_script("forge-parallel.sh", "plan", p, "--repo", self.repo, "--no-memory")
        self.assertEqual(r_plan.returncode, 0, r_plan.stdout)

        r_run = self.run_script(
            "forge-parallel.sh", "run", p, "--setup-before-verify", "--verify-retries", "0"
        )
        self.assertEqual(r_run.returncode, 0, r_run.stdout)
        self.assertEqual((p / "verification.status").read_text(encoding="utf-8").strip(), "PASS")
        int_wt = Path((p / "wt_root").read_text(encoding="utf-8").strip()) / "_integration"
        count_lines = (int_wt / "generated" / "count.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(count_lines), 2, f"expected initial setup + pre-verify setup, got {count_lines}")


if __name__ == "__main__":
    unittest.main()
