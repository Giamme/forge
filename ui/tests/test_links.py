"""GitHub links named in task markdown (forge_ui/links.py). Hand-written expectations."""
import unittest

from forge_ui.links import extract


class LinkTests(unittest.TestCase):
    def test_url_kinds(self):
        text = (
            'See https://github.com/acme/widgets/issues/4 and '
            'https://github.com/acme/widgets/pull/9.\n'
            'Also https://github.com/acme/widgets/blob/main/README.md?plain=1 '
            'and https://example.com/not-github.\n'
            'Trail https://github.com/acme/widgets.\n')
        self.assertEqual(extract(text, 'prompt.md'), [
            {'kind': 'issue', 'number': 4, 'url': 'https://github.com/acme/widgets/issues/4', 'source': 'prompt.md'},
            {'kind': 'pr', 'number': 9, 'url': 'https://github.com/acme/widgets/pull/9', 'source': 'prompt.md'},
            {'kind': 'url', 'number': None, 'url': 'https://github.com/acme/widgets/blob/main/README.md?plain=1',
             'source': 'prompt.md'},
            {'kind': 'url', 'number': None, 'url': 'https://github.com/acme/widgets', 'source': 'prompt.md'},
        ])

    def test_hash_with_ssh_https_and_no_origin(self):
        text = 'Fixes #12.\n'
        issue = {'kind': 'issue', 'number': 12, 'url': 'https://github.com/acme/widgets/issues/12', 'source': 'approach.md'}
        self.assertEqual(extract(text, 'approach.md', 'git@github.com:acme/widgets.git'), [issue])
        self.assertEqual(extract(text, 'approach.md', 'https://github.com/acme/widgets.git'), [issue])
        self.assertEqual(extract(text, 'approach.md', 'ssh://git@github.com/acme/widgets.git'), [issue])
        self.assertEqual(extract(text, 'approach.md', 'git@github.com:Acme/Widgets.git'), [
            {'kind': 'issue', 'number': 12, 'url': 'https://github.com/Acme/Widgets/issues/12', 'source': 'approach.md'}])
        bare = {'kind': 'issue', 'number': 12, 'url': None, 'source': 'request.md'}
        self.assertEqual(extract(text, 'request.md', None), [bare])
        self.assertEqual(extract(text, 'request.md', 'https://gitlab.com/acme/widgets.git'), [bare])
        self.assertEqual(extract(text, 'request.md', 'https://github.com/acme/widgets/extra.git'), [bare])

    def test_code_spans_fences_and_headings_are_not_links(self):
        text = (
            '# Title #1\n'
            '## Fix #7\n'
            'A real #8 ref.\n'
            'Not `#9` or `` see `#10` here ``.\n'
            '```\n'
            '#11 and https://github.com/acme/widgets/issues/11\n'
            '```\n'
            'After #12.\n')
        self.assertEqual(extract(text, 'prompt.md', 'https://github.com/acme/widgets'), [
            {'kind': 'issue', 'number': 8, 'url': 'https://github.com/acme/widgets/issues/8', 'source': 'prompt.md'},
            {'kind': 'issue', 'number': 12, 'url': 'https://github.com/acme/widgets/issues/12', 'source': 'prompt.md'},
        ])

    def test_a_hash_without_a_space_is_a_reference_and_a_heading_is_not(self):
        self.assertEqual(extract('#123\n# 123\n', 'prompt.md'), [
            {'kind': 'issue', 'number': 123, 'url': None, 'source': 'prompt.md'},
        ])

    def test_the_same_issue_written_twice_is_one_link(self):
        text = (
            'See #4 and https://github.com/acme/widgets/issues/4?utm=1#frag.\n'
            'Again #4.\n'
            'https://github.com/acme/widgets/issues/4.\n')
        self.assertEqual(extract(text, 'prompt.md', 'git@github.com:acme/widgets.git'), [
            {'kind': 'issue', 'number': 4, 'url': 'https://github.com/acme/widgets/issues/4', 'source': 'prompt.md'},
        ])
