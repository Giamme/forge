"""Tests for scripts/forge_flock.py across Windows and POSIX."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_flock  # noqa: E402


class ForgeFlockTests(unittest.TestCase):
    """Advisory file locking works across file objects, fds, and subprocesses."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.dir.name) / 'test.lock'

    def tearDown(self):
        self.dir.cleanup()

    def test_lock_and_unlock_file_object_and_fd(self):
        with open(self.lock_path, 'a+', encoding='utf-8') as handle:
            forge_flock.flock(handle, forge_flock.LOCK_EX | forge_flock.LOCK_NB)
            forge_flock.flock(handle, forge_flock.LOCK_UN)
            # Repeat unlock on an unlocked handle is a harmless no-op.
            forge_flock.flock(handle, forge_flock.LOCK_UN)

        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            forge_flock.flock(fd, forge_flock.LOCK_EX | forge_flock.LOCK_NB)
            forge_flock.flock(fd, forge_flock.LOCK_UN)
            forge_flock.flock(fd, forge_flock.LOCK_UN)
        finally:
            os.close(fd)

    def test_ordinary_reader_is_not_blocked_while_lock_is_held(self):
        self.lock_path.write_text('payload\n', encoding='utf-8', newline='\n')
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            forge_flock.flock(fd, forge_flock.LOCK_EX | forge_flock.LOCK_NB)
            reader = subprocess.run(
                [sys.executable, '-c', 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text().strip())',
                 str(self.lock_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(reader.returncode, 0, reader.stderr)
            self.assertEqual(reader.stdout.strip(), 'payload')
            self.assertEqual(os.path.getsize(self.lock_path), len('payload\n'))
        finally:
            forge_flock.flock(fd, forge_flock.LOCK_UN)
            os.close(fd)

    def test_nonblocking_contention_across_processes(self):
        child_code = (
            'import errno, os, sys; '
            f'sys.path.insert(0, {str(ROOT / "scripts")!r}); '
            'import forge_flock; '
            'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644); '
            '\ntry:\n'
            '    forge_flock.flock(fd, forge_flock.LOCK_EX | forge_flock.LOCK_NB)\n'
            'except OSError as exc:\n'
            '    sys.exit(42 if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES) else 1)\n'
            'finally:\n'
            '    os.close(fd)\n'
            'sys.exit(0)\n'
        )
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            forge_flock.flock(fd, forge_flock.LOCK_EX | forge_flock.LOCK_NB)
            held = subprocess.run([sys.executable, '-c', child_code, str(self.lock_path)],
                                  capture_output=True, text=True)
            self.assertEqual(held.returncode, 42, held.stderr)

            forge_flock.flock(fd, forge_flock.LOCK_UN)
            free = subprocess.run([sys.executable, '-c', child_code, str(self.lock_path)],
                                  capture_output=True, text=True)
            self.assertEqual(free.returncode, 0, free.stderr)
        finally:
            os.close(fd)

    def test_blocking_lock_waits_for_release(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        holder = None
        try:
            holder_code = (
                'import os, sys, time; '
                f'sys.path.insert(0, {str(ROOT / "scripts")!r}); '
                'import forge_flock; '
                'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644); '
                'forge_flock.flock(fd, forge_flock.LOCK_EX); '
                'print("locked", flush=True); '
                'time.sleep(0.3); '
                'forge_flock.flock(fd, forge_flock.LOCK_UN); '
                'os.close(fd)'
            )
            holder = subprocess.Popen(
                [sys.executable, '-c', holder_code, str(self.lock_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(holder.stdout.readline().strip(), 'locked')
            with self.assertRaises(OSError) as ctx:
                forge_flock.flock(fd, forge_flock.LOCK_EX | forge_flock.LOCK_NB)
            self.assertIn(ctx.exception.errno, (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES))
            forge_flock.flock(fd, forge_flock.LOCK_EX)
            forge_flock.flock(fd, forge_flock.LOCK_UN)
            self.assertEqual(holder.wait(timeout=5), 0)
        finally:
            if holder is not None and holder.poll() is None:
                holder.kill()
                holder.wait()
            if holder is not None:
                holder.stdout.close()
                holder.stderr.close()
            os.close(fd)


if __name__ == '__main__':
    unittest.main()
