"""What an agent CLI said and did in one execution: its log read as one list of events.

forge sends each dispatch's output to `<role>.log` (forge-dispatch.sh). What that holds depends on the
harness and how forge called it:
- codex `--json`: one JSON event per line (thread, items, turns);
- grok `--output-format streaming-messages-json`, claude and openclaude `--output-format stream-json`: one
  message per line in Claude's shape (system, assistant, user, result);
- opencode `--format json`: one event per line (text, reasoning, tool_use, step_finish, error);
- claude `--output-format json`: only the final result, with the session id; the conversation itself is
  then read from Claude's session file, `<config dir>/projects/<project>/<session id>.jsonl`;
- anything else (agy, plain text runs): the log as it is.
The format is told from the log itself; the harness only says where a session file would be.

An event is a dict with `kind` and the keys that kind uses:
  prompt     text                               the task as the agent got it (a session file's first message)
  message    text                               what the agent wrote
  reasoning  text                               its thinking, when the harness shows it
  command    command, output, exit, error       a shell command and what it printed
  edit       files, tool, error                 files the agent wrote or changed
  tool       tool, target, input, output, error any other tool call (target: the file, pattern or url it is about)
  result     text, tokens, error                the final answer (and the run's totals)
  error      text                               an error the harness reported
  text       text, stream                       lines that are not structured (a plain log, stderr)
  gap        count                              events left out of the middle of a very long transcript
Every event may carry `t` (epoch seconds), `id` (the harness's own id for a tool call), `error` (true for a call
that failed, false for one answered without an error) and, while the execution runs, `running` (a call not
answered yet). Texts are capped.
Nothing here opens any path but the log given and, for a Claude result, that one session file.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat

TEXT_CAP = 20000            # characters kept of one message, reasoning or result
OUTPUT_CAP = 8000           # of a command's or tool's output: the head and the tail
LOG_CAP = 32 << 20          # bytes of a log read; a longer one is read from its end
EVENTS_CAP = 5000           # events answered, and characters in them: past either, the middle is left out
CHARS_CAP = 4_000_000
_SESSION_ID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
_SHELL = re.compile(r"(?:/\S+/)?(?:ba|z)?sh -l?c '((?:[^']|'\\'')*)'", re.S)   # codex's wrapper around a command
_TARGETS = ('file_path', 'filePath', 'target_file', 'notebook_path', 'path', 'target_directory', 'pattern', 'query', 'url')
_SHELL_TOOLS = frozenset(('bash', 'shell', 'run_terminal_command', 'run_shell_command', 'exec_command', 'terminal'))
_EDIT_TOOLS = frozenset(('edit', 'write', 'multiedit', 'notebookedit', 'search_replace', 'write_file', 'edit_file',
                         'create_file', 'str_replace_editor', 'apply_patch', 'patch', 'replace', 'write_to_file'))
_CACHE: dict = {}           # (path, size, mtime, ...) → (answer, session file it read); a live log is read again when it grew


def cap(text, limit):
    """`text` as a string of at most `limit` characters: its head and tail around a marker, and how much was cut."""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, indent=1) if text is not None else ''
    if len(s) <= limit:
        return s, 0
    head = limit * 2 // 3
    tail = limit - head
    return s[:head] + f'\n… {len(s) - limit} characters …\n' + s[-tail:], len(s) - limit


def _put(event, key, value, limit):
    if value is None or value == '':
        return
    text, cut = cap(value, limit)
    event[key] = text
    if cut:
        event[key + '_cut'] = cut


def _event(kind, **fields):
    out = {'kind': kind}
    for key, value in fields.items():
        if key in ('text', 'output', 'input', 'command') and value is not None:
            _put(out, key, value, OUTPUT_CAP if key in ('output', 'input') else TEXT_CAP)
        elif value is not None:
            out[key] = value
    return out


def _unwrap(command):
    """A command without the `bash -lc '…'` codex runs it in."""
    m = _SHELL.fullmatch(command) if isinstance(command, str) else None
    return m.group(1).replace("'\\''", "'") if m else command


def _tool_event(name, args, ident=None, t=None):
    """A tool call as the event it is: a shell command, an edit, or any other tool."""
    low = (name or '').lower()
    args = args if isinstance(args, dict) else {'input': args} if args is not None else {}
    if low in _SHELL_TOOLS:
        return _event('command', command=args.get('command') or args.get('cmd') or args.get('input'), tool=name, id=ident, t=t)
    if low in _EDIT_TOOLS:
        files = [args.get(k) for k in ('file_path', 'filePath', 'path', 'target_file', 'notebook_path') if isinstance(args.get(k), str)]
        if not files and isinstance(args.get('edits'), list):
            files = [e.get('file_path') or e.get('path') for e in args['edits'] if isinstance(e, dict)]
        patch = _patch(args)
        ev = _event('edit', tool=name, files=[f for f in files if f] or None, input=None if patch else args, id=ident, t=t)
        _put(ev, 'patch', patch, OUTPUT_CAP)
        return ev
    # what the call is about (a file, a pattern, a url) heads it; the other arguments follow
    target = next((k for k in _TARGETS if isinstance(args.get(k), str)), None)
    rest = {k: v for k, v in args.items() if k != target}
    return _event('tool', tool=name or 'tool', target=args[target] if target else None, input=rest or None, id=ident, t=t)


def _patch(args):
    """An edit's arguments as -/+ lines: old and new strings (each edit of a multi-edit), or a written file's content."""
    def pair(a):
        old, new = a.get('old_string', a.get('oldString')), a.get('new_string', a.get('newString'))
        if not isinstance(old, str) and not isinstance(new, str):
            return None
        return '\n'.join(['-' + line for line in (old or '').splitlines()] + ['+' + line for line in (new or '').splitlines()])
    parts = [pair(args)] + [pair(e) for e in args.get('edits') or [] if isinstance(e, dict)]
    parts = [part for part in parts if part]
    if parts:
        return '\n…\n'.join(parts)
    content = args.get('content')
    return '\n'.join('+' + line for line in content.splitlines()) if isinstance(content, str) else None


def _when(value):
    """Epoch seconds from an ISO timestamp or epoch milliseconds/seconds, else None."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def _tokens(total, usage):
    """Adds a usage block (Claude, codex, opencode or grok naming) to the running totals."""
    if not isinstance(usage, dict):
        return
    def num(*keys):
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                return v
        return 0
    cache = usage.get('cache') if isinstance(usage.get('cache'), dict) else {}
    total['input'] = total.get('input', 0) + num('input_tokens', 'input') + num('cache_creation_input_tokens') + num('cache_read_input_tokens') + (cache.get('read') or 0) + (cache.get('write') or 0)
    total['cached'] = total.get('cached', 0) + num('cached_input_tokens', 'cache_read_input_tokens') + (cache.get('read') or 0)
    total['output'] = total.get('output', 0) + num('output_tokens', 'output') + num('reasoning')


# ---------- the formats ----------

def _lines(text):
    """(json or None, raw line) for each non-empty line."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        value = None
        if line[0] == '{':
            try:
                value = json.loads(line)
            except ValueError:
                value = None
        yield (value if isinstance(value, dict) else None), raw


def detect(text):
    """The log's format: codex, claude (Claude's message shape), claude-result, opencode, json or text."""
    seen = 0
    for value, _ in _lines(text[:200000]):
        if value is None:
            continue
        seen += 1
        kind = value.get('type')
        if 'item' in value or kind in ('thread.started', 'turn.started', 'turn.completed', 'turn.failed'):
            return 'codex'
        if kind in ('step_start', 'step_finish', 'tool_use', 'text', 'reasoning') and ('part' in value or 'sessionID' in value):
            return 'opencode'
        if kind in ('system', 'assistant', 'user') and ('message' in value or 'subtype' in value or 'session_id' in value):
            return 'claude'
        if kind == 'result' and seen == 1 and 'session_id' in value:
            rest = [v for v, _ in _lines(text) if v is not None]
            return 'claude-result' if len(rest) == 1 else 'claude'
        if seen >= 50:
            break
    if not seen and (_whole(text) or {}).get('type') == 'result':
        return 'claude-result'
    return 'json' if seen else 'text'


def _whole(text):
    """The log as one JSON object (a result printed over several lines), or None."""
    body = text.strip()
    if body[:1] != '{':
        return None
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _stderr(events, pending):
    if pending:
        events.append(_event('text', text=_ANSI.sub('', '\n'.join(pending)), stream='stderr'))
        pending.clear()


def parse_codex(text):
    events, by_id, tokens, meta, pending = [], {}, {}, {}, []
    for value, raw in _lines(text):
        if value is None:
            pending.append(raw)
            continue
        _stderr(events, pending)
        kind = value.get('type')
        if kind == 'thread.started':
            meta['session'] = value.get('thread_id')
        elif kind in ('item.started', 'item.updated', 'item.completed'):
            item = value.get('item') or {}
            ev = _codex_item(item)
            if ev is None:
                continue
            if kind != 'item.completed':
                ev['running'] = True
            ident = item.get('id')
            if ident in by_id:                       # started, then completed: the same place in the list
                events[by_id[ident]] = ev
            else:
                if ident:
                    by_id[ident] = len(events)
                events.append(ev)
        elif kind == 'turn.completed':
            _tokens(tokens, value.get('usage'))
        elif kind == 'turn.failed':
            events.append(_event('error', text=(value.get('error') or {}).get('message') or 'the turn failed'))
        elif kind == 'error':
            events.append(_event('error', text=value.get('message') or 'error'))
    _stderr(events, pending)
    return events, tokens, meta


def _codex_item(item):
    kind = item.get('type') or item.get('item_type')
    if kind in ('agent_message', 'assistant_message'):
        return _event('message', text=item.get('text'))
    if kind == 'reasoning':
        return _event('reasoning', text=item.get('text') or item.get('summary'))
    if kind == 'command_execution':
        ev = _event('command', command=_unwrap(item.get('command')), output=item.get('aggregated_output'), exit=item.get('exit_code'))
        if item.get('status') == 'failed' or (isinstance(item.get('exit_code'), int) and item['exit_code'] != 0):
            ev['error'] = True
        return ev
    if kind == 'file_change':
        changes = item.get('changes') or []
        return _event('edit', files=[c.get('path') for c in changes if isinstance(c, dict) and c.get('path')] or None,
                      change=[c.get('kind') for c in changes if isinstance(c, dict)] or None, error=item.get('status') == 'failed' or None)
    if kind == 'mcp_tool_call':
        ev = _event('tool', tool=f"{item.get('server') or 'mcp'}.{item.get('tool') or 'tool'}", input=item.get('arguments'),
                    output=item.get('result') if item.get('result') is not None else item.get('error'))
        if item.get('error') or item.get('status') == 'failed':
            ev['error'] = True
        return ev
    if kind == 'web_search':
        return _event('tool', tool='web_search', input=item.get('query'))
    if kind == 'todo_list':
        return _event('tool', tool='todo', input=item.get('items'))
    if kind == 'error':
        return _event('error', text=item.get('message'))
    if kind:
        return _event('tool', tool=kind, input={k: v for k, v in item.items() if k not in ('id', 'type')} or None)
    return None


def parse_claude(text, session_file=False):
    """Claude's message shape: grok's and claude's streams, and Claude's session files (one block per line there)."""
    events, by_id, tokens, meta, pending, counted = [], {}, {}, {}, [], set()
    for value, raw in _lines(text):
        if value is None:
            if not session_file:
                pending.append(raw)
            continue
        _stderr(events, pending)
        kind, t = value.get('type'), _when(value.get('timestamp'))
        if value.get('isSidechain') or value.get('isMeta'):
            continue
        if kind == 'system':
            if value.get('subtype') == 'init':
                meta.update(session=value.get('session_id'), model=value.get('model'))
            continue
        message = value.get('message') if isinstance(value.get('message'), dict) else None
        if kind == 'assistant' and message:
            mid = message.get('id')
            if mid is None or mid not in counted:   # a message's usage is repeated on each of its blocks' lines
                counted.add(mid)
                _tokens(tokens, message.get('usage'))
            for block in _blocks(message.get('content')):
                bt = block.get('type')
                if bt == 'text' and block.get('text', '').strip():
                    events.append(_event('message', text=block['text'], t=t))
                elif bt in ('thinking', 'redacted_thinking') and block.get('thinking'):
                    events.append(_event('reasoning', text=block['thinking'], t=t))
                elif bt in ('tool_use', 'server_tool_use'):
                    ev = _tool_event(block.get('name'), block.get('input'), block.get('id'), t)
                    if block.get('id'):
                        by_id[block['id']] = ev
                        ev['running'] = True                # until its result comes
                    events.append(ev)
                elif bt.endswith('_tool_result') and block.get('tool_use_id') in by_id:   # a server tool's result
                    by_id[block['tool_use_id']].pop('running', None)
                    _put(by_id[block['tool_use_id']], 'output', _payload(block.get('content'))[0], OUTPUT_CAP)
        elif kind == 'user' and message:
            for block in _blocks(message.get('content')):
                bt = block.get('type')
                if bt == 'tool_result':
                    ev = by_id.get(block.get('tool_use_id'))
                    out, code = _payload(block.get('content'))
                    if ev is None:
                        events.append(_event('tool', tool='result', output=out, t=t))
                        continue
                    ev.pop('running', None)
                    _put(ev, 'output', out, OUTPUT_CAP)
                    if code is not None and ev['kind'] == 'command':
                        ev['exit'] = code
                    ev['error'] = bool(block.get('is_error') or (ev['kind'] == 'command' and code not in (None, 0)))
                elif bt == 'text' and not events:
                    # a session file starts with the task as the agent got it
                    events.append(_event('prompt', text=block.get('text'), t=t))
        elif kind == 'result':
            usage = value.get('usage')
            if isinstance(usage, dict):
                tokens.clear()                          # the result holds the run's totals
                _tokens(tokens, usage)
            meta['session'] = meta.get('session') or value.get('session_id')
            if value.get('total_cost_usd') is not None:
                meta['cost'] = value.get('total_cost_usd')
            res = value.get('result')
            error = value.get('is_error') or value.get('subtype') not in (None, 'success')
            if error and not res:
                res = '; '.join(str(e) for e in value.get('errors') or []) or value.get('subtype') or 'error'
            _close(events, _event('result', text=res, error=True if error else None, tokens=dict(tokens) or None))
    _stderr(events, pending)
    return events, tokens, meta


def _close(events, result):
    """Adds the final result; its text is left out when the agent's last message already said it."""
    last = next((ev for ev in reversed(events) if ev['kind'] == 'message'), None)
    if last is not None and result.get('text') and last.get('text', '').strip() == result['text'].strip():
        result.pop('text')
        result['same'] = True
    events.append(result)


def _blocks(content):
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _payload(content):
    """(text, exit code or None) of a tool result's content: a string, Claude's list of text blocks, or grok's
    JSON string ({type, output: [bytes], exit_code, ...}, or {type, FileContent: {content, ...}})."""
    if isinstance(content, list):
        content = '\n'.join(b.get('text', '') if isinstance(b, dict) else str(b) for b in content)
    if not (isinstance(content, str) and content[:1] == '{'):
        return content, None
    try:
        data = json.loads(content)
    except ValueError:
        return content, None
    if not isinstance(data, dict):
        return content, None
    inner = [v for k, v in data.items() if k != 'type']
    if len(inner) == 1 and isinstance(inner[0], dict):
        data = inner[0]
    code = data.get('exit_code') if isinstance(data.get('exit_code'), int) else None
    parts = []
    for key in ('output', 'stdout', 'content', 'tool_output_for_prompt', 'message', 'result', 'text', 'summary', 'stderr'):
        value = data.get(key)
        if isinstance(value, list) and all(isinstance(x, int) for x in value):
            value = bytes(x & 0xff for x in value).decode('utf-8', 'replace')
        if isinstance(value, str) and value.strip():
            parts.append(value)
            if key not in ('output', 'stdout'):     # a command's output and its stderr; else the first text
                break
    return ('\n'.join(parts) if parts else json.dumps(data, ensure_ascii=False, indent=1)), code


def parse_opencode(text):
    events, tokens, meta, pending = [], {}, {}, []
    for value, raw in _lines(text):
        if value is None:
            pending.append(raw)
            continue
        _stderr(events, pending)
        kind, part, t = value.get('type'), value.get('part') if isinstance(value.get('part'), dict) else {}, _when(value.get('timestamp'))
        meta['session'] = meta.get('session') or value.get('sessionID')
        if kind == 'text':
            events.append(_event('message', text=part.get('text'), t=t))
        elif kind == 'reasoning':
            events.append(_event('reasoning', text=part.get('text'), t=t))
        elif kind == 'tool_use':
            state = part.get('state') if isinstance(part.get('state'), dict) else {}
            ev = _tool_event(part.get('tool'), state.get('input'), part.get('callID'), t)
            _put(ev, 'output', state.get('output') if state.get('status') != 'error' else state.get('error'), OUTPUT_CAP)
            meta_ = state.get('metadata') if isinstance(state.get('metadata'), dict) else {}
            if ev['kind'] == 'command' and isinstance(meta_.get('exit'), int):
                ev['exit'] = meta_['exit']
            if state.get('status') in ('completed', 'error'):
                ev['error'] = state.get('status') == 'error' or ev.get('exit') not in (None, 0)
            events.append(ev)
        elif kind == 'step_finish':
            _tokens(tokens, part.get('tokens'))
            if part.get('cost') is not None:
                meta['cost'] = (meta.get('cost') or 0) + part['cost']
        elif kind == 'error':
            err = value.get('error') if isinstance(value.get('error'), dict) else {}
            data = err.get('data') if isinstance(err.get('data'), dict) else {}
            events.append(_event('error', text=data.get('message') or err.get('name') or 'error', t=t))
    _stderr(events, pending)
    return events, tokens, meta


def parse_json(text):
    """JSON lines of a shape not known here (agy's stream, say): each shown as it is, text kept readable."""
    events = []
    for value, raw in _lines(text):
        if value is None:
            events.append(_event('text', text=_ANSI.sub('', raw)))
            continue
        kind = str(value.get('type') or value.get('event') or 'event')
        res = value.get('result')
        if kind == 'result' and isinstance(res, dict):           # agy --output-format stream-json's last event
            usage = res.get('usage') if isinstance(res.get('usage'), dict) else {}
            error = res.get('status') == 'ERROR' or bool(res.get('error'))
            events.append(_event('result', text=res.get('response') or res.get('error'), error=True if error else None,
                                 tokens={'input': usage.get('input_tokens') or 0, 'cached': usage.get('cache_read_tokens') or 0,
                                         'output': (usage.get('output_tokens') or 0) + (usage.get('thinking_tokens') or 0)} if usage else None))
            continue
        body = value.get('text') or value.get('content') or value.get('message') or value.get('result')
        if isinstance(body, str) and body.strip():
            events.append(_event('error' if 'error' in kind else 'message', text=body, source=kind))
        else:
            events.append(_event('tool', tool=kind, input=value))
    return _merge_text(events), {}, {}


def _merge_text(events):
    out = []
    for ev in events:
        if ev['kind'] == 'text' and out and out[-1]['kind'] == 'text':
            joined, cut = cap(out[-1]['text'] + '\n' + ev['text'], TEXT_CAP)
            out[-1]['text'] = joined
            if cut:
                out[-1]['text_cut'] = out[-1].get('text_cut', 0) + cut
        else:
            out.append(ev)
    return out


def parse_text(text):
    return ([_event('text', text=_ANSI.sub('', text).strip())] if text.strip() else []), {}, {}


PARSERS = {'codex': parse_codex, 'claude': parse_claude, 'opencode': parse_opencode, 'json': parse_json, 'text': parse_text}


# ---------- a Claude session file ----------

def session_file(session_id, home=None, environ=None):
    """The session file claude left for `session_id`, or None. Only `<config dir>/projects/<one dir>/<session
    id>.jsonl` is looked at, the config dir being $CLAUDE_CONFIG_DIR, then ~/.claude."""
    if not session_id or not _SESSION_ID.fullmatch(str(session_id)):
        return None
    env = os.environ if environ is None else environ
    home = Path(home or Path.home())
    roots = ([Path(env['CLAUDE_CONFIG_DIR'])] if env.get('CLAUDE_CONFIG_DIR') else []) + [home / '.claude']
    for root in roots:
        projects = root / 'projects'
        try:
            dirs = [d for d in os.scandir(projects) if d.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for d in dirs:
            path = Path(d.path) / f'{session_id}.jsonl'
            try:
                if stat.S_ISREG(path.stat().st_mode):
                    return path
            except OSError:
                continue
    return None


# ---------- one execution's transcript ----------

def _read(path):
    """The log's text (its end when it is over LOG_CAP) and whether it was cut."""
    try:
        size = path.stat().st_size
        with open(path, 'rb') as fh:
            if size > LOG_CAP:
                fh.seek(size - LOG_CAP)
                data = fh.read()
                data = data[data.find(b'\n') + 1:]
            else:
                data = fh.read()
    except OSError:
        return None, False
    return data.decode('utf-8', 'replace'), size > LOG_CAP


def transcript(log, harness=None, running=False, home=None, environ=None):
    """The transcript of the execution whose output is the file `log` (a Path, or None when there is none).

    {format, source: log|session|none, session, model, events, tokens, cost, truncated, warnings}
    """
    out = dict(format=None, source='none', session=None, model=None, events=[], tokens=None, cost=None, truncated=False, warnings=[])
    if log is None:
        out['warnings'].append('this execution has no log')
        return out
    try:
        st = log.stat()
    except OSError:
        st = None
    if st is None or not stat.S_ISREG(st.st_mode):
        out['warnings'].append(f'{log.name} is not readable')
        return out
    key = (str(log), st.st_size, st.st_mtime_ns, harness, running)
    if key in _CACHE:
        answer, sid, seen = _CACHE[key]
        if sid is None or _signature(session_file(sid, home, environ)) == seen:
            return json.loads(answer)
    text, cut = _read(log)
    if text is None:
        out['warnings'].append(f'{log.name} is not readable')
        return out
    if running:
        # a line still being written is not an event yet
        end = text.rfind('\n')
        text = text[:end + 1] if end >= 0 else ''
    fmt = detect(text)
    out.update(format=fmt, source='log', truncated=cut)
    if cut:
        out['warnings'].append(f'{log.name} is over {LOG_CAP >> 20} MB: only its end is shown')
    if fmt == 'claude-result':
        result = next((v for v, _ in _lines(text) if v is not None), None) or _whole(text)
        sid = result.get('session_id')
        path = session_file(sid, home, environ)
        tail = parse_claude(json.dumps(result))[0]
        events, tokens, meta = tail, {}, {}
        if path is not None:
            stext, scut = _read(path)
            if stext is not None:
                events, tokens, meta = parse_claude(stext, session_file=True)
                _close(events, tail[-1])                     # the run's own result closes it
                out.update(format='claude', source='session', truncated=cut or scut)
        if out['source'] != 'session':
            out['warnings'].append(f"{harness or 'claude'} logged only its final answer, and its session file "
                                   f"({sid or 'no session id'}) is not on this machine any more")
        if isinstance(result.get('usage'), dict):
            tokens = {}
            _tokens(tokens, result['usage'])
        meta.setdefault('session', sid)
        if result.get('total_cost_usd') is not None:
            meta['cost'] = result['total_cost_usd']
    else:
        events, tokens, meta = PARSERS[fmt](text)
    if not running:
        for ev in events:                                    # a call cut off when the agent stopped
            ev.pop('running', None)
    events, left_out = _fit(events)
    if left_out:
        out['warnings'].append(f'{left_out} events in the middle are left out')
        out['truncated'] = True
    out.update(events=events, tokens=tokens or None, session=meta.get('session'), model=meta.get('model'), cost=meta.get('cost'))
    if len(_CACHE) > 16:                                    # answers can be megabytes each
        _CACHE.clear()
    sid = result.get('session_id') if fmt == 'claude-result' else None
    _CACHE[key] = (json.dumps(out), sid, _signature(path) if sid else None)
    return out


def _signature(path):
    try:
        st = path.stat() if path is not None else None
    except OSError:
        return None
    return st and (str(path), st.st_size, st.st_mtime_ns)


def _fit(events):
    """(events, how many were left out): within EVENTS_CAP and CHARS_CAP, keeping the start and more of the end."""
    sizes = [sum(len(v) for v in ev.values() if isinstance(v, str)) for ev in events]
    if len(events) <= EVENTS_CAP and sum(sizes) <= CHARS_CAP:
        return events, 0
    head = used = 0
    while head < len(events) and head < EVENTS_CAP // 5 and used + sizes[head] <= CHARS_CAP // 5:
        used += sizes[head]
        head += 1
    tail = 0
    while tail < len(events) - head and tail < EVENTS_CAP - EVENTS_CAP // 5 and used + sizes[-1 - tail] <= CHARS_CAP:
        used += sizes[-1 - tail]
        tail += 1
    gap = len(events) - head - tail
    return events[:head] + [_event('gap', count=gap)] + events[len(events) - tail:], gap
