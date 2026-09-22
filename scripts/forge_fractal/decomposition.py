"""Mandatory, durable planning decisions for opt-in automatic task trees."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from . import artifact, write_json


WORK_PROTOCOL = '''
Forge has already made this node's decomposition decision. Implement the remaining
scoped work, integrate completed child results, and run the requested verification.
Do not spawn children or change control metadata. End your final response with:
FORGE_FRACTAL: {"done":true}
'''

PLAN_PROTOCOL = '''
FORGE_DECOMPOSITION_DECISION
This is a planning-only invocation. Inspect the repository without changing files,
running implementation, or using Fractal control commands. The approved task contract
and ownership are authoritative; decide only how to execute this node within them.
Prefer splitting nontrivial work into useful, independently verifiable children.
Keep genuinely atomic or tightly coupled work together and explain why.
Child goals must contain complete instructions and verification requirements.
Use globally unique child IDs, declared paths within the node's ownership, and sibling
dependencies for ordering. Existing completed work must be preserved, not delegated again.
Your children receive this same planning stage automatically. Do not force needless depth.
Return exactly one final-line JSON marker, with a nonempty reason:
FORGE_FRACTAL: {"decision":"atomic","reason":"why this work should remain together"}
or:
FORGE_FRACTAL: {"decision":"split","reason":"why this split is useful","children":[
{"id":"unique-name","goal":"complete instructions","difficulty":"low","paths":["relative/path"],"deps":[]},
{"id":"another-name","goal":"complete instructions","difficulty":"medium","paths":["another/path"],"deps":[]}]}
The split marker must be on ONE line and contain at least two children. Do not return done.
'''


def parse_decision(text: str) -> dict:
    markers = [line for line in text.splitlines() if line.startswith('FORGE_FRACTAL: ')]
    if len(markers) != 1 or text.strip().splitlines()[-1] != markers[0]:
        raise ValueError('Expected exactly one final FORGE_FRACTAL decision marker')
    value = json.loads(markers[0].split(': ', 1)[1])
    if not isinstance(value, dict) or value.get('decision') not in ('split', 'atomic'):
        raise ValueError('Planner must decide split or atomic; done is not a planning decision')
    if not isinstance(value.get('reason'), str) or not value['reason'].strip():
        raise ValueError('Decomposition decision requires a nonempty reason')
    allowed = {'decision', 'reason', 'children'} if value['decision'] == 'split' else {'decision', 'reason'}
    if set(value) != allowed:
        raise ValueError('Unexpected or missing decomposition decision fields')
    if value['decision'] == 'split':
        children = value['children']
        if not isinstance(children, list) or len(children) < 2:
            raise ValueError('A split requires at least two children')
        for child in children:
            _validate_child_shape(child)
    return value


def _validate_child_shape(child: dict) -> None:
    required = {'id', 'goal', 'difficulty', 'paths'}
    if not isinstance(child, dict) or not required <= child.keys() or child.keys() - required - {'deps'}:
        raise ValueError('Invalid child fields')
    if any(not isinstance(child[key], str) or not child[key].strip() for key in ('id', 'goal', 'difficulty')):
        raise ValueError('Child id, goal and difficulty must be nonempty strings')
    for key in ('paths', 'deps'):
        values = child.get(key, [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError('Child paths and deps must be lists of strings')


def manifests(node: dict) -> list[dict]:
    return [*node.get('decomposition_history', []), node.get('decomposition', {})]


def reserved_nodes(nodes: dict) -> dict:
    """Count accepted but not yet materialized children against lifetime bounds."""
    result = dict(nodes)
    for node in nodes.values():
        for decision in manifests(node):
            for child in decision.get('admission', []):
                result.setdefault(child['name'], dict(id=child['name'], parent=node['id'], status='reserved'))
    return result


def capacity(tree, node: dict) -> dict:
    nodes = reserved_nodes(tree.nodes())
    unsettled = sum(n['parent'] == node['id'] and n['status'] != 'completed' for n in nodes.values())
    return dict(depth=len(node['ancestors']), max_depth=tree.limits['depth'],
                children=tree.limits['children'] - unsettled, nodes=tree.limits['nodes'] - len(nodes))


def bound_reason(bounds: dict) -> str:
    if bounds['depth'] >= bounds['max_depth']:
        return 'depth limit reached'
    if bounds['children'] < 2:
        return 'fewer than two direct child slots available'
    if bounds['nodes'] < 2:
        return 'fewer than two lifetime node slots available'
    return ''


def implementation_prompt(tree, node: dict) -> str:
    from .execution import PROTOCOL
    automatic = tree.config.get('decomposition', {}).get('enabled', False)
    prompt = node['goal'] + (WORK_PROTOCOL if automatic else PROTOCOL)
    prompt += '\nOwned paths: ' + json.dumps(node['paths'])
    if automatic:
        prompt += '\nApproved task contract:\n' + tree.request['prompt']
    children = [n for n in tree.nodes().values() if n['parent'] == node['id']]
    if children:
        prompt += '\nCompleted child results (already imported):\n' + json.dumps(children)
    if node.get('last'):
        prompt += '\nPrevious work is preserved. Prior response:\n' + node['last']
    return prompt


def retry_decision(node: dict, prompt: str) -> None:
    decision = node.get('decomposition')
    changed_goal = node['id'] == 'implementation' and node['goal'] != prompt
    if decision and (changed_goal or decision['phase'] != 'accepted'):
        node.setdefault('decomposition_history', []).append(node.pop('decomposition'))
    node.pop('error', None)


def snapshot_fingerprint(workspace: Path) -> str:
    """Include ignored/product files as well as HEAD and staged content."""
    digest = hashlib.sha256(artifact('forge_fingerprint', workspace).encode())
    for root, dirs, files in os.walk(workspace, followlinks=False):
        if Path(root) == workspace:
            dirs[:] = [name for name in dirs if name != '.git']
        for name in sorted(dirs + files):
            path = Path(root) / name
            digest.update(str(path.relative_to(workspace)).encode())
            digest.update(str(path.lstat().st_mode).encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(65536), b''):
                        digest.update(block)
        dirs.sort()
    return digest.hexdigest()


class DecisionStage:
    def __init__(self, tree) -> None:
        self.tree = tree

    def save(self, node: dict, decision: dict) -> None:
        from .execution import event
        self.tree.update(node, decomposition=decision)
        event(self.tree.task, 'decomposition_decision', node=node['id'],
              phase=decision['phase'], decision=decision.get('decision'), reason=decision.get('reason'))

    def recover_admission(self, node: dict) -> None:
        # A manifest is the durable reservation and fixes routing before any spawn.
        with self.tree.guard:
            for decision in manifests(node):
                for child in decision.get('admission', []):
                    if child['name'] not in self.tree.nodes():
                        self.tree.create_node(parent=node, **child)
            self.tree.update(node)

    def ensure(self, node: dict, workspace: Path) -> None:
        from .execution import Halt
        self.recover_admission(node)
        if node.get('awaiting_children'):
            self.tree.children(node)
            self.tree.update(node, awaiting_children=False)
        decision = node.get('decomposition')
        if decision and decision['phase'] == 'accepted':
            return
        if decision and decision['phase'] == 'failed':
            raise Halt(decision['error'])
        if decision is None:
            decision = dict(phase='pending', attempts=0,
                            goal_sha256=hashlib.sha256(node['goal'].encode()).hexdigest(),
                            planner=self.tree.request['planner'])
            self.save(node, decision)
        while decision['attempts'] < 2:
            self.tree.pause_gate(node)
            bounds = capacity(self.tree, node)
            if reason := bound_reason(bounds):
                self.save(node, dict(decision, phase='accepted', decision='bounded', reason=reason, bounds=bounds))
                return
            decision.update(phase='planning', attempts=decision['attempts'] + 1, bounds=bounds)
            self.save(node, decision)
            try:
                text = self.invoke(node, workspace, decision)
                proposal = parse_decision(text)
                self.accept(node, decision, proposal)
            except Halt as error:
                if str(error) == 'paused':
                    decision.update(phase='pending', attempts=decision['attempts'] - 1)
                    self.save(node, decision)
                    continue
                if str(error) not in ('stopped', 'interrupted', 'timeout'):
                    self.save(node, dict(decision, phase='failed', error=str(error)))
                raise
            except (ValueError, KeyError, TypeError) as error:
                decision.update(phase='pending', feedback=str(error))
                self.save(node, decision)
                continue
            self.recover_admission(node)
            if node.get('awaiting_children'):
                self.tree.children(node)
                self.tree.update(node, awaiting_children=False)
            return
        self.save(node, dict(decision, phase='failed', error='planning_exhausted'))
        raise Halt('planning_exhausted')

    def invoke(self, node: dict, workspace: Path, decision: dict) -> str:
        from .execution import Halt
        from .invocation import run_step
        directory = self.tree.task / 'nodes' / node['id'] / 'planning' / uuid.uuid4().hex
        snapshot = directory / 'product'
        base = artifact('forge_tree', workspace)
        source_before = artifact('forge_fingerprint', workspace)
        artifact('forge_review_snapshot', workspace, base, base, snapshot)
        before = snapshot_fingerprint(snapshot)
        children = [n for n in self.tree.nodes().values() if n['parent'] == node['id']]
        context = dict(node=node['id'], goal=node['goal'], owned_paths=node['paths'],
                       bounds=decision['bounds'], routing=self.tree.config['routing'],
                       children=children, feedback=decision.get('feedback'),
                       existing_ids=list(reserved_nodes(self.tree.nodes())))
        prompt = ('Approved task contract:\n' + self.tree.request['prompt'] + '\n' + PLAN_PROTOCOL
                  + '\nNode planning context:\n' + json.dumps(context))
        write_json(directory / 'request.json', context)
        try:
            rc, text = run_step(self.tree, node, snapshot, prompt, phase='planning')
        finally:
            try:
                unchanged = snapshot_fingerprint(snapshot) == before
            except (OSError, RuntimeError):
                unchanged = False
            if not unchanged or artifact('forge_fingerprint', workspace) != source_before:
                raise Halt('planning_mutation')
        self.tree.update(node, planning_last=text)
        if rc:
            raise Halt('timeout' if rc == 7 else 'planning_failed')
        return text

    def accept(self, node: dict, decision: dict, proposal: dict) -> None:
        with self.tree.guard:
            bounds = capacity(self.tree, node)
            reason = bound_reason(bounds)
            needed = len(proposal.get('children', []))
            offered = decision.get('bounds', bounds)
            if min(bounds['children'], bounds['nodes']) < needed <= min(offered['children'], offered['nodes']):
                reason = 'child capacity consumed by another admission during planning'
            if proposal['decision'] == 'split' and reason:
                proposal = dict(decision='bounded', reason=reason)
            admission = []
            if proposal['decision'] == 'split':
                admission = self.tree.validate_children(node, proposal['children'])
            accepted = dict(decision, **proposal, phase='accepted', bounds=bounds, admission=admission)
            # Decision and awaiting state share one atomic node.json write.
            node['awaiting_children'] = bool(admission)
            self.save(node, accepted)
