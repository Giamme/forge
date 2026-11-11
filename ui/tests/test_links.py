"""GitHub links named in task markdown (forge_ui/links.py). Hand-written expectations."""
import sys
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

    def test_a_fence_inside_a_block_quote_is_not_a_link(self):
        quoted = (
            '> ~~~python\n'
            '> #133 and https://github.com/acme/widgets/issues/133\n'
            '> ~~~\n'
            '> > ~~~\n'
            '> > #9\n'
            '> > ~~~\n'
            '> See #12.\n')
        issue = {'kind': 'issue', 'number': 12, 'url': 'https://github.com/acme/widgets/issues/12',
                 'source': 'prompt.md'}
        self.assertEqual(extract(quoted, 'prompt.md', 'https://github.com/acme/widgets'), [issue])
        # A quoted fence marker is content of a top-level fence, so it does not close it.
        top = '```\n> ```\n#11\n```\nSee #12.\n'
        self.assertEqual(extract(top, 'prompt.md', 'git@github.com:acme/widgets.git'), [issue])

    def test_a_quoted_fence_ends_when_the_quote_ends(self):
        issue = {'kind': 'issue', 'number': 133, 'url': 'https://github.com/acme/widgets/issues/133',
                 'source': 'prompt.md'}
        # The blank line ends the block quote, and the fence ends with it.
        self.assertEqual(extract('> ~~~python\n> example\n\nFix #133.\n', 'prompt.md',
                                 'https://github.com/acme/widgets'), [issue])
        # A fenced block does not continue lazily onto the next unquoted line either.
        self.assertEqual(extract('> ~~~python\n> example\nFix #133.\n', 'prompt.md',
                                 'git@github.com:acme/widgets.git'), [issue])

    def test_a_fence_indented_under_a_list_item_is_not_a_link(self):
        origin = 'https://github.com/acme/widgets'
        after = {'kind': 'issue', 'number': 12, 'url': 'https://github.com/acme/widgets/issues/12',
                 'source': 'prompt.md'}
        buried = {'kind': 'issue', 'number': 133, 'url': 'https://github.com/acme/widgets/issues/133',
                  'source': 'prompt.md'}
        self.assertEqual(extract('- Example:\n\n    ~~~python\n    print("#133")\n    ~~~\n',
                                 'prompt.md', origin), [])
        # A tab stops at column 4, which is inside an item whose content starts at column 2.
        self.assertEqual(extract('- Example:\n\n\t~~~\n\tprint("#133")\n\t~~~\n',
                                 'prompt.md', origin), [])
        self.assertEqual(extract('- See #4.\n\n    ~~~\n    #133\n    ~~~\n\nAlso #12.\n',
                                 'prompt.md', origin), [
            {'kind': 'issue', 'number': 4, 'url': 'https://github.com/acme/widgets/issues/4',
             'source': 'prompt.md'},
            after,
        ])
        # `1.` plus two spaces indents the item by four, so the fence hides #133 and #12 stays.
        self.assertEqual(extract('1.  foo\n\n    ```\n    #133\n    ```\n\n    #12\n',
                                 'prompt.md', origin), [after])
        # Four spaces and no list is an indented code block, not a fence. Only fences, spans and
        # headings are masked, so both references stay.
        self.assertEqual(extract('    ~~~python\n    print("#133")\n    ~~~\nSee #12.\n',
                                 'prompt.md', origin), [buried, after])

    def test_a_closing_fence_may_be_indented_whatever_the_opener_was(self):
        origin = 'https://github.com/acme/widgets'
        fix = {'kind': 'issue', 'number': 133, 'url': 'https://github.com/acme/widgets/issues/133',
               'source': 'prompt.md'}
        self.assertEqual(extract('~~~python\nexample\n ~~~\nFix #133.\n', 'prompt.md', origin), [fix])
        self.assertEqual(extract('```\nexample #4\n   ```\nFix #133.\n', 'prompt.md', origin), [fix])
        # Four spaces is content, so the fence stays open and hides what follows.
        self.assertEqual(extract('~~~\nexample\n    ~~~\nFix #133.\n', 'prompt.md', origin), [])

    def test_a_number_python_will_not_convert_is_not_a_link(self):
        huge = '9' * 5000
        text = (
            f'See #{huge} and https://github.com/acme/widgets/issues/{huge}.\n'
            'Real #4 and https://github.com/acme/widgets/pull/8.\n')
        real = [
            {'kind': 'issue', 'number': 4, 'url': 'https://github.com/acme/widgets/issues/4',
             'source': 'prompt.md'},
            {'kind': 'pr', 'number': 8, 'url': 'https://github.com/acme/widgets/pull/8',
             'source': 'prompt.md'},
        ]
        url = {'kind': 'url', 'number': None, 'url': f'https://github.com/acme/widgets/issues/{huge}',
               'source': 'prompt.md'}
        issue = {'kind': 'issue', 'number': int(huge) if self._converts(huge) else None,
                 'url': f'https://github.com/acme/widgets/issues/{huge}', 'source': 'prompt.md'}

        def expect(refused):
            # Refused: #N is skipped and the URL stays a url. Accepted: the two are one issue.
            return [url if refused else issue] + real

        self.assertEqual(extract(text, 'prompt.md', 'https://github.com/acme/widgets.git'),
                         expect(not self._converts(huge)))
        # Where the interpreter can refuse a long digit string, that path must not raise either.
        setter = getattr(sys, 'set_int_max_str_digits', None)
        getter = getattr(sys, 'get_int_max_str_digits', None)
        if setter is None or getter is None:
            return
        previous = getter()
        try:
            setter(sys.int_info.str_digits_check_threshold)
            self.assertEqual(extract(text, 'prompt.md', 'https://github.com/acme/widgets.git'),
                             expect(True))
        finally:
            setter(previous)

    @staticmethod
    def _converts(text):
        try:
            int(text)
        except ValueError:
            return False
        return True

    def test_the_same_issue_written_twice_is_one_link(self):
        text = (
            'See #4 and https://github.com/acme/widgets/issues/4?utm=1#frag.\n'
            'Again #4.\n'
            'https://github.com/acme/widgets/issues/4.\n')
        self.assertEqual(extract(text, 'prompt.md', 'git@github.com:acme/widgets.git'), [
            {'kind': 'issue', 'number': 4, 'url': 'https://github.com/acme/widgets/issues/4', 'source': 'prompt.md'},
        ])
