#!/usr/bin/env python3
"""Assemble context from explicit sources; never summarize or trim requirements."""
from __future__ import annotations

import argparse
from pathlib import Path


def context(
    requirements: str, approach: str | None = None, capsule: str | None = None,
    retry: str | None = None, goal: str | None = None, memories: tuple[str, ...] = (),
) -> str:
    chunks = []
    for title, path in [('Ownership and baseline', capsule), ('Overall goal', goal),
                        ('Complete implementation requirements', requirements),
                        ('Intended approach', approach),
                        ('Your previous attempt was reviewed and rejected', retry)]:
        if path and Path(path).is_file():
            with Path(path).open(newline="") as source:
                data = source.read()
            if data:
                chunks.append('## ' + title + '\n' + data)
    # Memory injection emits headings and prose around bullet facts. Union only
    # exact fact lines, preserving distinct facts even when they share a key.
    seen = set()
    facts = []
    for path in memories:
        if Path(path).is_file():
            for line in Path(path).read_text().splitlines():
                if line.startswith('- ') and line not in seen:
                    seen.add(line)
                    facts.append(line)
    if facts:
        chunks.append('## Project memory (context from earlier runs)\n' + '\n'.join(facts))
    return '\n\n'.join(chunks) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for key in ('requirements', 'approach', 'capsule', 'retry', 'goal'):
        parser.add_argument('--' + key, required=key == 'requirements')
    parser.add_argument('--memory', action='append', default=[])
    args = vars(parser.parse_args())
    args['memories'] = args.pop('memory')
    print(context(**args), end='')
