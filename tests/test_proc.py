"""Cross-platform process/signal tests for forge_proc and forge-guard.py."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_proc  # noqa: E402


class ForgeProcTests(unittest.TestCase):
    def test_bash_executable_resolves_real_bash(self) -> None:
        bash = forge_proc.bash_executable()
        done = subprocess.run([bash, '-c', 'echo ok'], capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), 'ok')

    def test_pid_alive_and_kill_tree(self) -> None:
        self.assertTrue(forge_proc.pid_alive(os.getpid()))
        self.assertFalse(forge_proc.pid_alive(0))
        self.assertFalse(forge_proc.pid_alive(-1))
        self.assertFalse(forge_proc.pid_alive(1, min_pid=2))
        self.assertFalse(forge_proc.pid_alive('not-a-pid'))  # type: ignore[arg-type]

        proc = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **forge_proc.popen_group_kwargs(),
        )
        try:
            self.assertTrue(forge_proc.pid_alive(proc.pid))
            forge_proc.kill_tree(proc.pid, force=True)
            proc.wait(timeout=5)
            self.assertFalse(forge_proc.pid_alive(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_win_descendants_ignores_stale_ppid_before_parent_start(self) -> None:
        table = {
            100: (1, '2000', 'parent.exe'),
            200: (100, '2500', 'real-child.exe'),
            300: (100, '1000', 'reused-ppid-older.exe'),
            400: (300, '1500', 'older-grandchild.exe'),
        }
        orig = forge_proc.IS_WINDOWS
        try:
            forge_proc.IS_WINDOWS = True
            self.assertEqual(forge_proc.win_descendants(table, {100}), {200})
        finally:
            forge_proc.IS_WINDOWS = orig

    def test_mask_sigterm_and_install_signal(self) -> None:
        with forge_proc.mask_sigterm():
            called = True
        self.assertTrue(called)
        prev = signal.getsignal(forge_proc.SIGUSR1) if forge_proc.SIGUSR1 is not None else None
        try:
            forge_proc.install_signal(forge_proc.SIGUSR1, lambda *_: None)
        finally:
            if forge_proc.SIGUSR1 is not None and prev is not None:
                signal.signal(forge_proc.SIGUSR1, prev)

    def test_guard_ping_identity_session_and_stop_tree(self) -> None:
        guard = SCRIPTS / 'forge-guard.py'
        ping = subprocess.run([sys.executable, str(guard), 'ping'], capture_output=True, text=True, timeout=10)
        self.assertEqual(ping.returncode, 0, ping.stderr)
        self.assertEqual(ping.stdout.strip(), 'ok')

        sess = subprocess.run(
            [sys.executable, str(guard), 'session', '--', sys.executable, '-c', 'import sys; print("hi"); sys.exit(7)'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(sess.returncode, 7)
        self.assertEqual(sess.stdout.strip(), 'hi')

        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            parent = subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    (
                        'import os, pathlib, subprocess, sys, time\n'
                        'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                        'p = pathlib.Path(sys.argv[1])\n'
                        'tmp = p.with_suffix(".tmp")\n'
                        'tmp.write_text(str(c.pid))\n'
                        'os.replace(tmp, p)\n'
                        'time.sleep(30)\n'
                    ),
                    str(pidfile),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **forge_proc.popen_group_kwargs(),
            )
            try:
                deadline = time.monotonic() + 5.0
                child_text = ''
                while time.monotonic() < deadline:
                    if pidfile.exists():
                        child_text = pidfile.read_text().strip()
                        if child_text.isdigit():
                            break
                    time.sleep(0.05)
                self.assertTrue(child_text.isdigit(), f'child.pid not written cleanly: {child_text!r}')
                child_pid = int(child_text)
                self.assertTrue(forge_proc.pid_alive(child_pid))

                stop = subprocess.run(
                    [sys.executable, str(guard), 'stop-tree', str(parent.pid), '--wait', '0.3'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(stop.returncode, 0, stop.stderr)
                self.assertGreaterEqual(int(stop.stdout.strip()), 2)
                parent.wait(timeout=5)
                deadline = time.monotonic() + 3.0
                while forge_proc.pid_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(forge_proc.pid_alive(child_pid))
            finally:
                if parent.poll() is None:
                    forge_proc.kill_tree(parent.pid, force=True)
                    parent.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
