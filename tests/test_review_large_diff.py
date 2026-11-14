"""A task review over a diff larger than a reviewer's harness takes (#145).

The fake codex/claude from test_forge.py: no model is called.
"""
import unittest

import test_forge


class LargeDiffReviewTests(test_forge.ForgeTests):
    def test_a_diff_over_the_limit_is_read_from_the_snapshot_and_a_small_one_stays_inline(self):
        p = self.plan(deps='-')
        # 410 KB, over the 400 KB the whole-run review already inlines at most.
        self.scenario({'a.dwarf.*': {'files': {'big.txt': ('x' * 99 + '\n') * 4200}}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        big = (p / 'tasks' / 'a' / 'qa.input').read_text()
        self.assertNotIn('```diff', big)
        self.assertIn("git diff HEAD~1 HEAD -- . ':(exclude).forge'", big)
        self.assertIn('big.txt', big)                      # the stat names it
        self.assertLess(len(big.encode()), 100000)
        small = (p / 'tasks' / 'b' / 'qa.input').read_text()
        self.assertIn('```diff', small)
        self.assertNotIn('git diff HEAD~1 HEAD', small)
        self.assertFalse((p / 'tasks' / 'a' / '.qa.diff').exists())
        self.assertTrue(all((p / 'tasks' / t / 'merged').exists() for t in 'ab'))

    def test_a_prompt_the_harness_refuses_as_too_large_names_the_cause_and_the_way_out(self):
        p = self.plan(deps='-')
        self.scenario({'a.qa.*': {'stdout': 'Error: turn/start failed: Input exceeds the maximum length of 1048576 '
                                            'characters. {"input_error_code":"input_too_large"}', 'rc': 1}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual((p / 'tasks' / 'a' / 'status').read_text().strip(), 'ERROR')
        said = (p / 'tasks' / 'a' / 'task.out').read_text()
        self.assertIn("a: the reviewer's harness refused the prompt as too large", said)
        self.assertIn('A retry sends the same prompt', said)
        self.assertIn('forge-parallel.sh adopt', said)
        self.assertNotIn('a: qa dispatch failed', said)


if __name__ == '__main__':
    unittest.main()
