"""plan accepts a dwarf cell of `script`: tasks/<id>/run, the table, the forecast (#142).

`plan` keeps the cell, requires an executable run file, and never routes it. The forecast and
the slow-spec notes skip it. preflight_plan skips the dwarf doctor. Running the file is a
later task; nothing here dispatches a model.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_forge as fixtures

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from forge_dataset.efforts import plan_notes, slow_note  # noqa: E402
from forge_dataset.forecast import plan_tasks  # noqa: E402

# forge-parallel.sh up to its main section, then preflight_plan. `run --dry-run` returns
# before that function, so the doctor is reached the way a real run reaches it.
PREFLIGHT = r'''
source_text="$(sed '/^# --- main/,$d' "$SKILL/scripts/forge-parallel.sh" | sed "s|^SELF=.*|SELF=$SKILL/scripts/forge-parallel.sh|; s|^SKILL_DIR=.*|SKILL_DIR=$SKILL|")"
SKILL_DIR="$SKILL"
eval "$source_text"
preflight_plan "$1"
'''

SCRIPT_LINE = 'scripted tasks: 1 (gen) run tasks/<id>/run; no implementer model'
MISSING_RUN = "task '%s' is a script task but tasks/%s/run is missing or not executable"
POOL_MSG = "pool names 'script', which is reserved for a task that runs tasks/<id>/run"


class ScriptPlanTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.count = 0

    def new_plan(self, rows):
        """rows: (id, deps, difficulty, files, dwarf, qa)."""
        self.count += 1
        p = self.root / ('plan%d' % self.count)
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join(
            '%s\t%s\t%s\t%s\t%s\t%s\tT%s\n' % (*row, row[0]) for row in rows))
        for row in rows:
            task = p / 'tasks' / row[0]
            task.mkdir(parents=True)
            (task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + row[0])
        return p

    def executable(self, plan, task):
        run = plan / 'tasks' / task / 'run'
        run.write_text('#!/bin/sh\nexit 0\n')
        run.chmod(0o755)
        return run

    def plan_cmd(self, plan, *flags):
        return self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory', *flags)

    def rows(self, plan):
        out = {}
        for line in (plan / 'tasks.tsv').read_text().splitlines():
            if not line or line.startswith('#'):
                continue
            fields = line.split('\t')
            self.assertEqual(len(fields), 7, line)
            out[fields[0]] = fields
        return out

    def test_a_script_row_is_kept_and_the_table_counts_no_implementer(self):
        p = self.new_plan([
            ('gen', '-', 'medium', 'a.txt', 'script', '-'),
            ('impl', '-', 'low', 'b.txt', '-', '-'),
        ])
        self.executable(p, 'gen')
        r = self.plan_cmd(p, '--dwarf-low', 'sol')
        self.assertEqual(r.returncode, 0, r.stdout)
        rows = self.rows(p)
        self.assertEqual(rows['gen'][2:6], ['medium', 'a.txt', 'script', 'opus'])
        self.assertEqual(rows['impl'][4], 'sol')
        self.assertIn('tasks: 2   waves: 1   estimated dispatches: 3 (1 dwarf + 2 qa)', r.stdout)
        self.assertIn(SCRIPT_LINE, r.stdout)
        self.assertRegex(r.stdout, r'gen\s+medium\s+script\s+opus')
        # The blank row was routed. The script row was not explored and has no running tier.
        self.assertTrue((p / 'tasks' / 'impl' / 'running.tier').is_file())
        self.assertFalse((p / 'tasks' / 'gen' / 'running.tier').exists())
        self.assertFalse((p / 'tasks' / 'gen' / 'route.jsonl').exists())
        self.assertFalse((p / 'tasks' / 'gen' / 'route.explored').exists())
        forecast, merged = plan_tasks(p)
        self.assertEqual([row['task'] for row in forecast], ['impl'])
        self.assertEqual(forecast[0]['spec'], 'sol')
        self.assertEqual(merged, [])

    def test_a_script_row_needs_an_executable_run_file(self):
        # One mutation from a row plan would accept: the run file is missing, or not executable.
        for case, mode in (('missing', 'absent'), ('plain', 'file')):
            with self.subTest(case=case):
                p = self.new_plan([
                    ('gen', '-', 'low', 'a.txt', 'script', 'opus'),
                    ('impl', '-', 'low', 'b.txt', 'sol', 'opus'),
                ])
                before = (p / 'tasks.tsv').read_text()
                if mode == 'file':
                    run = p / 'tasks' / 'gen' / 'run'
                    run.write_text('#!/bin/sh\nexit 0\n')
                    run.chmod(0o644)
                r = self.plan_cmd(p)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(MISSING_RUN % ('gen', 'gen'), r.stdout)
                self.assertEqual((p / 'tasks.tsv').read_text(), before)

    def test_a_dwarf_pool_cannot_name_script(self):
        pools = (
            ('--dwarf-low', 'script'),
            ('--dwarf', 'script'),
            ('--dwarf-high', 'opus,script'),
            ('--dwarf-medium', 'script,sol'),
        )
        for flag, value in pools:
            with self.subTest(flag=flag, value=value):
                p = self.new_plan([('impl', '-', 'low', 'a.txt', '-', 'opus')])
                before = (p / 'tasks.tsv').read_text()
                r = self.plan_cmd(p, flag, value)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(POOL_MSG, r.stdout)
                self.assertIn(flag, r.stdout)
                self.assertEqual((p / 'tasks.tsv').read_text(), before)

    def test_a_profile_pool_cannot_name_script_when_the_alias_exists(self):
        # Profile validation accepts a registered alias. `script` is still reserved after that.
        registry = self.root / 'registry.tsv'
        registry.write_text(
            'script\tcodex\tscript-model\t<=ultra\n'
            'sol\tcodex\tgpt-6.2-sol\t<=ultra\n'
            'opus\tclaude\topus\t<=max\n')
        self.env['FORGE_REGISTRY'] = str(registry)
        self.env['XDG_CONFIG_HOME'] = str(self.root / 'cfg')
        cfg = self.root / 'cfg' / 'forge'
        cfg.mkdir(parents=True)
        (cfg / 'decide.json').write_text(json.dumps({
            'version': 1,
            'profiles': {'P': {'routing': {'dwarf': {'low': ['script']}}, 'policy': {'explore': 0}}},
        }))
        p = self.new_plan([('impl', '-', 'low', 'a.txt', '-', 'opus')])
        before = (p / 'tasks.tsv').read_text()
        r = self.plan_cmd(p, '--profile', 'P')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(POOL_MSG, r.stdout)
        self.assertIn('--dwarf-low', r.stdout)
        self.assertEqual((p / 'tasks.tsv').read_text(), before)

    def test_a_replan_with_a_low_pool_leaves_the_script_cell(self):
        p = self.new_plan([
            ('gen', '-', 'low', 'a.txt', 'script', '-'),
            ('impl', '-', 'low', 'b.txt', '-', '-'),
        ])
        self.executable(p, 'gen')
        first = self.plan_cmd(p, '--dwarf-low', 'sol')
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(self.rows(p)['gen'][4], 'script')
        self.assertEqual(self.rows(p)['impl'][4], 'sol')
        again = self.plan_cmd(p, '--dwarf-low', 'haiku')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(self.rows(p)['gen'][4], 'script')
        self.assertEqual(self.rows(p)['impl'][4], 'sol')

    def test_a_profile_replan_does_not_record_or_replace_script(self):
        self.env['XDG_CONFIG_HOME'] = str(self.root / 'cfg')
        cfg = self.root / 'cfg' / 'forge'
        cfg.mkdir(parents=True)
        (cfg / 'decide.json').write_text(json.dumps({
            'version': 1,
            'profiles': {
                'P': {'routing': {'dwarf': {'low': ['sol']}, 'qa': {'any': ['opus']}},
                      'policy': {'explore': 0}},
                'Q': {'routing': {'dwarf': {'low': ['haiku']}, 'qa': {'any': ['sonnet']}},
                      'policy': {'explore': 0}},
            },
        }))
        p = self.new_plan([
            ('gen', '-', 'low', 'a.txt', 'script', '-'),
            ('impl', '-', 'low', 'b.txt', '-', '-'),
        ])
        self.executable(p, 'gen')
        first = self.plan_cmd(p, '--profile', 'P')
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(self.rows(p)['gen'][4:6], ['script', 'opus'])
        routed = json.loads((p / 'routed.json').read_text())
        self.assertNotIn('gen', routed['dwarf'])
        self.assertEqual(routed['dwarf'].get('impl'), 'sol')
        again = self.plan_cmd(p, '--profile', 'Q')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(self.rows(p)['gen'][4], 'script')
        self.assertEqual(self.rows(p)['impl'][4], 'haiku')
        routed = json.loads((p / 'routed.json').read_text())
        self.assertNotIn('gen', routed['dwarf'])
        self.assertEqual(routed['dwarf'].get('impl'), 'haiku')

    def write_tasks(self, name, lines, merged=()):
        p = self.root / name
        p.mkdir()
        (p / 'tasks.tsv').write_text(lines)
        for task in merged:
            d = p / 'tasks' / task
            d.mkdir(parents=True)
            (d / 'merged').write_text('x\n')
        return p

    def test_plan_tasks_skips_script_rows_and_does_not_call_them_merged(self):
        header = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
        mixed = self.write_tasks('mixed', header + (
            'gen\t-\tlow\ta.txt\tscript\topus\tGen\n'
            'impl\t-\tmedium\tb.txt\tsol\topus\tImpl\n'
            'done\t-\thigh\tc.txt\thaiku\topus\tDone\n'), merged=('gen', 'done'))
        forecast, merged = plan_tasks(mixed)
        self.assertEqual(forecast, [{'task': 'impl', 'tier': 'medium', 'spec': 'sol'}])
        self.assertEqual(merged, ['done'])
        only = self.write_tasks('only', header + (
            'gen\t-\tlow\ta.txt\tscript\topus\tGen\n'
            'mov\t-\tlow\tb.txt\tscript\topus\tMov\n'), merged=('gen',))
        self.assertEqual(plan_tasks(only), ([], []))

    def test_plan_notes_skips_a_script_dwarf_and_keeps_its_qa(self):
        p = self.write_tasks('notes', (
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            'gen\t-\tlow\ta.txt\tscript\thaiku:max\tGen\n'
            'impl\t-\tlow\tb.txt\tsol\topus\tImpl\n'))
        seen = []

        def observe(spec, **kwargs):
            seen.append(spec)
            return slow_note(spec, **kwargs)

        with patch('forge_dataset.efforts.slow_note', side_effect=observe):
            notes = plan_notes(p)
        self.assertNotIn('script', seen)
        self.assertIn('haiku:max', seen)
        self.assertIn('sol', seen)
        self.assertTrue(any(line.startswith('note: ') and 'haiku:max' in line for line in notes))
        self.assertFalse(any('script' in line for line in notes))

    def test_preflight_skips_the_dwarf_doctor_for_script(self):
        p = self.new_plan([
            ('gen', '-', 'low', 'a.txt', 'script', 'sonnet'),
            ('impl', '-', 'low', 'b.txt', 'sol', 'opus'),
        ])
        self.executable(p, 'gen')
        r = subprocess.run(['bash', '-c', PREFLIGHT, 'preflight', str(p)], cwd=self.repo,
                           env=dict(self.env, SKILL=str(ROOT)), text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.count('role=dwarf'), 1, r.stdout)
        self.assertEqual(r.stdout.count('role=qa'), 2, r.stdout)
        self.assertIn('alias=sol', r.stdout)
        self.assertIn('alias=sonnet', r.stdout)
        self.assertIn('alias=opus', r.stdout)
        self.assertNotIn('alias=script', r.stdout)
        self.assertNotIn('not a known alias', r.stdout)
        self.assertFalse((self.root / 'calls').exists())
