"""registry.tsv itself: one alias names one model on every harness (#187).

The first row of an alias is its default harness, and `alias:effort:harness` moves "the same
model to a different harness". Statistics keyed by alias (forge_dataset/names.py) count every
row of an alias together, so a row that names another model under the same alias would mix two
models under one name. A harness that only serves another version gets a versioned alias.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'registry.tsv'
HARNESSES = {'codex', 'claude', 'openclaude', 'opencode', 'antigravity', 'grok'}


def rows(text):
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        out.append(line.split('\t'))
    return out


def model_key(model):
    """(family, version) for a model id. The version is None for a CLI's own moving name
    (claude's `opus`), which matches any version.

    A provider prefix (github-copilot/, opencode-go/), a `claude-` vendor prefix and the
    -thinking / -preview suffixes are not part of the model: `github-copilot/claude-opus-5.5`
    is opus 5.5, `claude-opus-4-6-thinking` is opus 4.6, `gemini-3.1-pro-preview` is gemini-pro 3.1.
    """
    name = model.rsplit('/', 1)[-1].lower()
    if name.startswith('claude-'):
        name = name[len('claude-'):]
    name = re.sub(r'-(thinking|preview)$', '', name)
    family, version = [], []
    for part in name.split('-'):
        if re.fullmatch(r'\d+(\.\d+)*', part):
            version.append(part)
        else:
            family.append(part)
    return '-'.join(family), ('.'.join(version) or None)


def mismatches(table):
    """{alias: sorted model ids} for every alias whose rows name different models."""
    by_alias = {}
    for cells in table:
        by_alias.setdefault(cells[0], []).append(cells[2])
    out = {}
    for alias, models in by_alias.items():
        keys = {model_key(m) for m in models}
        families = {f for f, _ in keys}
        versions = {v for _, v in keys if v is not None}
        if len(families) > 1 or len(versions) > 1:
            out[alias] = sorted(models)
    return out


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.text = REGISTRY.read_text(encoding='utf-8')
        self.table = rows(self.text)

    def test_every_row_has_an_alias_a_known_harness_a_model_and_an_effort(self):
        for cells in self.table:
            self.assertIn(len(cells), (4, 5), cells)
            self.assertIn(cells[1], HARNESSES, cells)
            self.assertTrue(cells[0] and cells[2] and cells[3], cells)

    def test_an_alias_has_at_most_one_row_per_harness(self):
        seen = set()
        for cells in self.table:
            self.assertNotIn((cells[0], cells[1]), seen, cells)
            seen.add((cells[0], cells[1]))

    def test_no_alias_names_two_different_models(self):
        # Fix a failure by giving the odd row a versioned alias of its own (opus-4.6, grok-4.6).
        self.assertEqual(mismatches(self.table), {})

    def test_the_header_lists_every_harness_a_row_uses(self):
        header = next(line for line in self.text.splitlines() if 'which CLI runs it' in line)
        for harness in {cells[1] for cells in self.table}:
            self.assertIn(harness, header)

    def test_the_model_key_reads_the_ids_forge_uses(self):
        self.assertEqual(model_key('gpt-6.2-sol'), ('gpt-sol', '6.2'))
        self.assertEqual(model_key('github-copilot/gpt-6.2-sol'), ('gpt-sol', '6.2'))
        self.assertEqual(model_key('gpt-6-astra'), ('gpt-astra', '6'))
        self.assertEqual(model_key('claude-opus-4-6-thinking'), ('opus', '4.6'))
        self.assertEqual(model_key('github-copilot/claude-opus-5.5'), ('opus', '5.5'))
        self.assertEqual(model_key('opus'), ('opus', None))
        self.assertEqual(model_key('github-copilot/gemini-3.1-pro-preview'), ('gemini-pro', '3.1'))
        self.assertEqual(model_key('gemini-3.7-flash'), ('gemini-flash', '3.7'))

    def test_the_check_finds_the_rows_that_used_to_disagree(self):
        # The rows #187 found: gemini was flash on antigravity and pro on opencode, opus was 4.6
        # on antigravity, grok 4.7 on grok and 4.6 on opencode.
        old = rows('gemini\tantigravity\tgemini-3.7-flash\tlow\n'
                   'gemini\topencode\tgithub-copilot/gemini-3.1-pro-preview\t-\n'
                   'opus\tclaude\topus\t<=max\n'
                   'opus\tantigravity\tclaude-opus-4-6-thinking\tnone\n'
                   'opus\topencode\tgithub-copilot/claude-opus-5.5\t-\n'
                   'grok\tgrok\tgrok-4.7\t<=xhigh\n'
                   'grok\topencode\tgithub-copilot/grok-4.6\t-\n'
                   'sol\tcodex\tgpt-6.2-sol\t<=ultra\n'
                   'sol\topencode\tgithub-copilot/gpt-6.2-sol\t-\n')
        self.assertEqual(sorted(mismatches(old)), ['gemini', 'grok', 'opus'])


if __name__ == '__main__':
    unittest.main()
