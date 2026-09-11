"""Pinned Fractal agents.py backend. Every invocation uses a fresh Forge dispatch."""
from __future__ import annotations

import json
import sys
import uuid

from fractal.core.agent import Agent, Invocation, StreamEvent, StreamParser

from . import SCRIPTS


class ForgeParser(StreamParser):
    def feed(self, line: str) -> list[StreamEvent]:
        try:
            value = json.loads(line)
        except ValueError:
            return []
        if not isinstance(value, dict) or value.get('forge_event') != 'result':
            return []
        self.model = value['model']
        self.cost = value.get('cost')
        self.final = True
        if value['exit_code']:
            self.errors.append(value['text'])
        return [StreamEvent(kind='result', text=value['text'], model=self.model,
                            failed=bool(value['exit_code']), final=True, cost=self.cost)]


class ForgeAgent(Agent):
    name = 'forge-bridge'
    mints_session = True
    enforces_budget = False
    __parser__ = ForgeParser

    def _invocation(self, prompt, *, mode, session, model, effort, budget) -> Invocation:
        # The coordinator gives each work step its own immutable request directory.
        step = self.forge_step
        prompt_path = step / ('input-' + uuid.uuid4().hex + '.md')
        prompt_path.write_text(prompt)
        argv = (sys.executable, str(SCRIPTS / 'forge-fractal.py'), '_bridge',
                str(step), str(prompt_path), model)
        return Invocation(agent=self.name, argv=argv, cwd=self.forge_workspace)


__all__ = ['ForgeAgent']
