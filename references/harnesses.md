# Harness reference

Per-CLI invocation details behind `scripts/forge-dispatch.sh`. Read this when a dispatch
fails, when adding a backend, or when you need to know what a role can actually do.

Everything below was verified by running the CLIs on this machine, not read from docs —
several of these behaviours are undocumented and a few contradict what the `--help` text
implies.

Fractal uses this same dispatcher for all six harnesses through its `agents.py` bridge.
It is an optional execution backend, not a seventh model harness or a replacement for
`alias:effort:harness`. The same effort and permission rules apply to each fresh nested
work step. Use `./forge fractal doctor --spec SPEC` for runtime and advertised capability
checks; authentication and live provider availability still require an explicitly requested
provider test. See [Fractal execution](fractal.md).

## Contents

- [At a glance](#at-a-glance)
- [codex](#codex)
- [claude / openclaude](#claude--openclaude)
- [opencode](#opencode)
- [antigravity (agy)](#antigravity-agy)
- [grok](#grok)
- [Continuing a reviewer session](#continuing-a-reviewer-session) (and an implementer's)
- [Adding a harness](#adding-a-harness)

## At a glance

| Harness | Non-interactive form | Model flag | Effort flag | Full bypass |
|---------|----------------------|------------|-------------|-------------|
| `codex` | `codex exec` | `-m` | `-c model_reasoning_effort=<e>` | `--dangerously-bypass-approvals-and-sandbox` |
| `claude` | `claude -p` | `--model` | `--effort` | `--dangerously-skip-permissions` |
| `openclaude` | `openclaude -p` | `--model` | `--effort` | `--dangerously-skip-permissions` |
| `opencode` | `opencode run` | `-m provider/model` | `--variant` | `--auto` |
| `antigravity` | `agy --print=...` | `--model` | `--effort` | `--dangerously-skip-permissions` |
| `grok` | `grok --prompt-file` | `--model` | `--reasoning-effort` | `--always-approve` |

**Only antigravity bounds its own runtime.** The other five will sit on a hung request
indefinitely, and stock macOS ships no `timeout` binary to wrap them in — so a hung dwarf
used to stall its whole wave under `forge-parallel.sh`'s `xargs -P` with no output and no
way to distinguish stuck from slow. `forge-dispatch.sh` therefore runs every backend in the
background and kills it after its timeout: `--timeout`, else `FORGE_TIMEOUT` (an explicit `0`
disables), else the optional `timeout` column of the model's registry row, else 2700 s — scaled
×4/3 for `xhigh` and ×2 for `max`/`ultra` effort when not explicit (`<role>.resolved` records
`timeout=` and `timeout_source=`). A killed dispatch exits `7`.

**Infrastructure failures exit `8`.** After the backend exits, `forge-runtime.py classify` reads
the dispatch log (never `<role>.last`, which is only created afterwards): claude/openclaude/grok
`result` events with `is_error`, codex `turn.failed`/`error` and opencode `error` events that no
answer followed, and for the raw-log harness (antigravity) the last 2 KB when the exit was nonzero —
looking for strong phrases (usage/session limit, quota, insufficient balance/credit, rate limit,
429/529, unauthorized/401/403, invalid API key, not logged in, ECONNRESET/ENOTFOUND/…). An
`empty` answer (exit 0, nothing said) is the fifth class. codex's non-fatal reconnect notices
followed by a normal reply never classify. `<role>.infra` holds `class=`, `retry_after=` (parsed
from "resets 3pm", "try again in 2h", `|<epoch>`, capped at 24 h), `detail=`, `rc=`, `role=`,
`harness=`, `model=` (the scheduler pauses by them: a rate limit pauses that model on that
harness, a quota or auth stop that harness).
A dwarf that exits 0 with nothing said writes the file but still exits 0; callers consult it
only when the diff is empty.

Implementation notes worth keeping, because they were arrived at the hard way: the backend is
started with `exec` inside its subshell, so the pid forge holds is the backend itself rather than
a wrapper it would be useless to kill. macOS has no `setsid` binary, so the harness is launched
through `forge-guard.py session` (python's `os.setsid()` then `exec`, same pid, pgid == pid)
unless `FORGE_ORPHAN_GUARD=off` or the run is Fractal. That gives a process group to signal as a
unit: the watchdog (TERM, then KILL for a CLI that traps TERM) and the exit trap signal the whole
group in addition to `pkill -P`, and after a normal exit `forge-guard.py reap` lists and stops
anything still alive in it (`<role>.orphans`). This is proven offline only; verify a new harness
with one live dispatch before relying on it, and use the kill switch if a harness deliberately
leaves a helper running.

On an interrupt the harness leads its own session, so the TERM a decomposed run's scheduler
sends to a task's process group reaches the dispatch, whose exit trap stops the harness. The
scheduler waits up to `FORGE_INTERRUPT_GRACE` seconds (default 10) for each task's group to
empty before KILLing it. As a backstop, a guarded dispatch records its harness right after
launch in `<role>.harness` (`<pid><TAB><start time>`, from `forge-guard.py identity`; removed when
the dispatch ends). For a task whose dispatch could not stop its harness, and at the next
scheduler start for tasks a killed scheduler left `RUNNING`, `forge-guard.py stop <record>`
signals the recorded group (TERM, about 2 s, KILL) only when the pid still has the recorded
start time, so a reused pid is left alone.

Effort ladders, low to high:

- codex — `low medium high xhigh max ultra`
- claude — `low medium high xhigh max`
- openclaude — `low medium high xhigh max ultracode`
- antigravity — `low medium high`, and **per-model**: see below
- opencode — no fixed ladder; `--variant` is provider-specific and is forwarded verbatim
- grok — `low medium high xhigh max`; each model offers a subset (grok-4.7: up to `xhigh`)

## codex

```
codex exec -m <model> -C <repo> --skip-git-repo-check -o <last-message-file> \
  -c model_reasoning_effort=<effort> (--approve-for-me | --dangerously-bypass-...) "<prompt>"
```

**It does not validate the effort.** `-c model_reasoning_effort=bogus` starts a run and
prints `reasoning effort: bogus` in its own header without erroring. A typo therefore costs
a full run at some unintended level, with nothing in the output saying so. This is the main
reason forge validates efforts itself before dispatch.

**Prompt delivery uses stdin.** Forge copies the input once (including FIFOs), checks it
with a streaming whitespace character class, and runs `codex exec ... - < role.prompt`.
Native review has no custom prompt and uses `/dev/null`. Codex JSONL events, Claude and openclaude
`--output-format stream-json` and opencode `--format json` provide native usage; final responses
remain in `<role>.last`.
See the official [Codex non-interactive guide](https://learn.chatgpt.com/docs/non-interactive-mode)
and [Claude headless guide](https://code.claude.com/docs/en/headless).

`--approve-for-me` and `--dangerously-bypass-approvals-and-sandbox` are mutually exclusive,
and `--approve-for-me` additionally conflicts with an explicit `-s/--sandbox`.

**Native review** (`codex exec review`) has two constraints worth knowing:

- Its scope flags (`--uncommitted`, `--base`, `--commit`) are **mutually exclusive with a
  custom PROMPT** — `error: the argument '--uncommitted' cannot be used with '[PROMPT]'`.
  So native review cannot be told what the dwarf was asked to do. It judges whether the
  code is correct, never whether it is the right change.
- It has **no `-C/--cd`**, unlike `codex exec`. It must be launched from inside the repo.

## claude / openclaude

```
<harness> -p --model <model> --add-dir <run-dir> --output-format stream-json --verbose --effort <effort> \
  (--permission-mode acceptEdits --allowedTools Bash | --dangerously-skip-permissions)
  [--disallowed-tools "Edit,Write,NotebookEdit"]
```
with the prompt delivered on **stdin**.

**The log is a stream.** `--output-format stream-json` (which `-p` accepts only with `--verbose`)
writes one JSON message per line: the session, every assistant message and tool call, every tool
result, and a closing `result` line with the final answer and the token counts, which is what
forge reads into `<role>.last` and the metrics. The rest is the run's conversation, which forge-ui
shows. Hook and rate-limit events in the stream are ignored.

**The prompt must not go on argv when a variadic flag precedes it.** `--disallowed-tools`,
`--allowed-tools` and friends are variadic, so a following positional is consumed as
another value. In practice the prompt is parsed word by word into tool names — you get a
pile of `Permission deny rule "the" matches no known tool` warnings and then
`Error: Input must be provided either through stdin or as a prompt argument`. Passing it on
stdin sidesteps this entirely and also lifts the argv length limit, which matters once a
diff is inlined into a review prompt.

**Permission modes are not interchangeable, and no single mode is enough.** Re-measured on
this machine with a prompt that asks the agent to edit a file *and* run a real command:

| Mode | Can edit files | Can run shell |
|------|----------------|---------------|
| `acceptEdits` | yes | trivial commands only — prompts for the rest |
| `dontAsk` | — | **no** |
| `auto` | **no** — prompts for Edit | yes |
| `bypassPermissions` | yes | yes |

Both single-mode choices fail, and both fail *silently*:

- under `auto`, a dwarf writes nothing and ends with "I need your permission to edit f.py".
  The harness still exits 0, so forge sees a successful run that produced an empty diff;
- under `acceptEdits` alone, a dwarf writes the code and then stalls on "awaiting approval
  to run pytest", shipping unverified work while reporting success.

Beware that `acceptEdits` auto-approves *some* shell — `echo` goes through — so a smoke test
using a trivial command will wrongly suggest shell access works.

Forge therefore uses **`--permission-mode acceptEdits` plus `--allowedTools "Bash"`** for
non-yolo roles, which gives both. QA additionally gets
`--disallowed-tools "Edit,Write,NotebookEdit"`, which still overrides the mode's edit
permission for those tools. Bash can still write files. The solo and parallel runners use
disposable review repositories and fingerprint checks to invalidate changed artifacts.

`--add-dir <run-dir>` is required because the run directory deliberately sits outside the
repo; without it the agent refuses to open the artifacts forge just wrote for it
("outside the allowed working directory").

openclaude is a Claude Code fork and takes the same flags, plus `--provider` and an extra
`ultracode` effort tier. It routes to whatever provider it is authenticated against —
check with `openclaude auth status`, which on this machine reports
`{"authMethod": "third_party", "apiProvider": "codex"}`, meaning GPT models run through a
Claude-Code-shaped harness. That is the point of `sol:xhigh:openclaude`.

## opencode

```
opencode run -m <provider>/<model> --dir <repo> --variant <effort> --auto [--agent plan] --format json "<prompt>"
```

Models are always `provider/model`; `opencode models` lists what is configured.

`opencode run` exposes **no sandbox tier** — there is only `--auto`. Without it the run
blocks on a permission prompt that nothing can answer headlessly, so forge always passes
it. This means opencode's non-yolo mode is weaker than the other three harnesses': the
containment for a QA role comes from `--agent plan`, its built-in read-only agent, rather
than from a permission boundary.

**Credentials are the usual failure.** Being installed proves nothing. On this machine all
three configured routes currently fail, each differently:

- `github-copilot/*` → `Unauthorized: unauthorized: AuthenticateToken authentication failed`
- `opencode-go/*` → `Insufficient balance.`
- `opencode/*-free` → `UnknownError ... Unexpected server error`

`forge-dispatch.sh doctor` reports binaries on PATH; it cannot detect any of these, which
surface only mid-dispatch as an exit 4. When an opencode dispatch fails, show the user the
actual error — it is almost always auth or billing, and swapping to another backend would
hide a problem they need to fix.

## antigravity (agy)

```
agy --model <model> --print-timeout <t> [--effort <e>] --add-dir <repo> --add-dir <run-dir> \
  (--mode accept-edits | --mode plan | --dangerously-skip-permissions) --print='<prompt>'
```

The harness is named `antigravity` in forge; the binary is `agy`. This is how Gemini is
reached.

**Its log stays plain text.** agy has `--output-format stream-json`, but only its closing event
has been seen (`{"event": "result", "result": {"status", "response", "error", "usage", ...}}`, on a
lapsed login), not the stream of a working run, and the final answer would come from it. Until
then forge keeps agy's printed output, which is both its log and its answer.

**`--print` must use the attached `=` form.** It is a Go flag with an optional value, so
written detached it consumes whatever token follows as its prompt. `agy --print
--print-timeout 120s --model X "real prompt"` fails with *"--print took --print-timeout as
its prompt, so the intended prompt was left as an argument and ignored"*. Forge puts
`--print=...` last so nothing can be captured after it.

**`--add-dir <repo>` is mandatory.** Antigravity is workspace-scoped, not cwd-scoped.
Without it the agent searches the wrong tree and the run dies on *"Find command timed out.
Use a more targeted search directory or pattern.: context deadline exceeded"* — which reads
like a performance problem and is actually a missing workspace.

**It cannot run shell commands without yolo.** Measured: `--mode accept-edits` and
`--sandbox` both deny every command, and the denial is *fatal* — the run aborts with
`permission check failed for command ... user denied permission`, leaving an empty diff
after consuming the quota. Only `--dangerously-skip-permissions` allows execution. Because
the standard dwarf prompt asks the model to run the project's tests, forge appends an
explicit "do not attempt commands" note to the prompt whenever antigravity runs without
yolo. This is the one harness where the non-yolo dwarf genuinely cannot verify its own work.

**Effort is validated, and varies per model** — unlike codex, which accepts anything:

| Model | Accepts |
|-------|---------|
| `gemini-3.7-flash`, `-3.6-flash`, `-3.5-flash` | `low`, `medium`, `high` |
| `gemini-3.1-pro` | `low`, `high` — **no medium** |
| `gpt-oss-120b` | `medium` only |
| `claude-sonnet-4-6`, `claude-opus-4-6-thinking` | none; passing `--effort` errors |

Two traps here. `agy models` lists effort-suffixed ids (`gemini-3.7-flash-low`), but a
suffixed id **conflicts** with `--effort`: *"--model gemini-3.7-flash-low conflicts with
--effort=high"*. Forge therefore registers the unsuffixed ids and sets effort via the flag,
which is what makes `gemini:high` mean the same thing here as on every other harness. And
`gemini-3.1-pro` has a hole in the middle rather than a low ceiling, which is why the
registry's effort column supports explicit sets and not just a `<=ceiling`.

QA runs under `--mode plan`, antigravity's read-only mode.

**Installing forge *into* antigravity works differently from the others.** agy ignores a
loose skill directory under `~/.gemini/skills` — it cannot see skills placed there at all,
including ones that were already sitting in that folder. Skills are only loaded as part of
a plugin: a directory containing `plugin.json` **at its root** (not in `.claude-plugin/`,
which is why `agy plugin validate` rejects a Claude plugin directly) plus a `skills/`
subdirectory. `forge-install.sh` generates that wrapper at `~/.gemini/forge-plugin` and runs
`agy plugin install`.

The catch: `agy plugin install` **copies** the files into `~/.gemini/config/plugins/forge/`
rather than referencing them. Every other harness tracks this directory live through a
symlink or a shim, so editing `SKILL.md` updates them all at once — antigravity is the
exception and keeps running whatever it copied. Re-run `forge-install.sh` after changing
the skill, or agy will silently run a stale version.

`--print-timeout` defaults to 5m in agy, which is short for real implementation work; forge
passes 30m and exposes `--agy-timeout` to change it.

## grok

```
grok --model <model> --prompt-file <prompt> --output-format streaming-messages-json \
  --reasoning-effort <effort> --background-wait-timeout <limit> \
  (--permission-mode acceptEdits --allow Bash [--deny Edit --deny Write] | --always-approve)
```

The xAI Grok Build CLI, signed in with `grok login` (or `XAI_API_KEY`); `grok models` lists
what the account can use and each model's efforts live in `~/.grok/models_cache.json`. The
`grok` alias runs `grok-4.7` here and keeps its opencode route (`grok:high:opencode`, 4.6 via
GitHub Copilot) as a second row.

- **A headless run cannot ask, so it refuses and exits 0.** In the default mode an edit that
  needs approval is skipped with `permission required: Edit calc.py was not run` on stderr:
  the same silent empty diff claude gives. The dwarf therefore runs in `acceptEdits` with
  `--allow Bash` (shell for tests). A reviewer gets the same plus `--deny Edit --deny Write`;
  deny beats the mode and every allow, and Bash can still write files, as with claude.
- **Output is the claude stream.** `streaming-messages-json` ends in a `result` event with
  `result`, `is_error` and the three disjoint token buckets, so `forge-runtime.py` reads the
  final message, the tokens and an `is_error` failure exactly as it does for claude. An error
  subtype can leave `result` empty and put the reason in `errors[]`, which is read instead.
- **A tool call that still needs approval cancels the whole session.** `--allow Bash` does not
  cover every command. Seen in a real run: a `mkdir -p /tmp/… && cp …` into a directory outside
  the worktree, and a multi-line `cd …/ui && \` command. Each printed `permission required: …
  was not run`, and the session then ended with an `error_during_execution` result (`stop_reason`
  `cancelled`), still exiting 0.
  - **What forge records:** class `cancelled` in `<role>.infra`, with the refused command, and
    a note in the run output.
  - **It is not an infrastructure stop:** the dwarf's diff so far still goes to review, and a
    reviewer cut off before its verdict is UNKNOWN, which re-runs only the review.
  - **Untested:** whether `--permission-mode dontAsk` (deny without cancelling) together with
    explicit Edit/Write/Bash allows would avoid the cancel. Nothing changes until that is
    proven live.
- **Background work waits as long as forge's limit allows.** After its last turn a headless run
  waits for the background jobs it started (a test suite run in the background, say) and wakes
  the model with each result. By default it waits only 600 s, then stops the work and exits 1:
  a real dwarf whose full suite ran longer ended as an error. forge passes the role's own limit
  as `--background-wait-timeout`, or a day when the limit is off (`--timeout 0`; grok refuses
  0). grok counts from the last turn and forge from the start, so forge's watchdog always fires
  first and the run ends as a timeout. The flag is missing from `grok --help`; preflight checks
  it with `grok --background-wait-timeout 1 --help` instead, because grok rejects an unknown flag
  that comes before `--help` (exit 2).
- The prompt goes by `--prompt-file`, never argv, and stdin is `/dev/null`.

Verified live on 2026-10-19 with a `FORGE_OK` dispatch: exit 0, `permissionMode` acceptEdits,
the answer extracted from the `result` line, tokens recorded, nothing left running. The same
day, grok-4.7 dwarfs implemented and passed review on real tasks (the forge-ui integration run).
A grok reviewer has not been proven live yet.

## Continuing a reviewer session

`forge-dispatch.sh qa <spec> ... --continue-session <id>` continues an earlier reviewer session instead of
starting a fresh one. `<id>` is the session or thread id a dispatch left in `<run-dir>/<role>.session`
(8 to 128 characters of `[A-Za-z0-9-]`, not starting with `-`). It is accepted for the `qa` role on claude,
openclaude and codex in custom-prompt mode only, and for the `dwarf` role on claude and openclaude
(`plan --implementer-session`); any other role or harness, `--native-review` or a malformed id is a usage
error (exit 2). Nothing in forge asks for it by default.

An implementer continues with the fresh dwarf argv plus `--resume <id>`, so it keeps `acceptEdits` and its
Bash allowance. codex is reviewer-only: `exec resume` has no `-s` and no `--approve-for-me` (checked against
codex-cli 0.164.0), so its write access would rest on a config override that nothing verifies, and a
sandbox it silently lacked would end in an empty diff. Its preamble says the work has been reviewed since,
is in `<repo>` as the implementer left it and need not be re-read, and that this attempt's instructions win.

Probed 2026-10-22 (claude 2.1.307, codex 0.162.0):

| Harness | Argv | Notes |
|---|---|---|
| claude, openclaude | the fresh QA argv plus `--resume <id>` | Continues from a different working directory and keeps the same session id. Permission flags and the read-only restriction stay; the prompt is still on stdin. |
| codex | `codex exec resume <id> --json -m MODEL --skip-git-repo-check -o LAST [-c model_reasoning_effort=…] -c 'sandbox_mode="read-only"' -` | Continues from a different working directory. `exec resume` has **no `-C/--cd` and no `-s/--sandbox`**, so the dispatch's own `cd "$REPO"` sets the directory and the sandbox is passed as a config override (the quotes are TOML). The first turn's sandbox was inherited when probed (read-only); the override makes it explicit. Under `--yolo` the bypass flag replaces the override. |

The prompt is preceded by a short fixed preamble (this continues your earlier review, the work has changed,
the working directory is now `<repo>`, re-read files, this round's instructions win). `<role>.prompt` includes
it. `<role>.continue` records the attempt as `requested=<id>`, then `mode=continued` or `mode=failed` with
`reason=<one line>`, and is archived with the attempt.

**Exit 9** means the session was not continued and the caller may dispatch fresh. Dispatch never retries fresh
itself, so each dispatch keeps its own honest record. It happens when:

- the harness does not advertise the flags continuing needs (`codex exec resume --help`, or `--resume` in
  claude's help) but would pass preflight for a fresh QA dispatch: `reason=unsupported`, before any model call;
  a harness that fails the fresh preflight too keeps exit 3;
- the harness ran and exited non-zero: `reason=` the log's last error line.

A timeout stays exit 7 and an infrastructure stop stays exit 8; both still write `mode=failed`. `doctor --spec`
without `--continue-session` is unchanged.

## Installing forge into a harness

Each harness discovers skills differently, and the differences are not guessable — verify
with the listed check rather than trusting file placement.

| Harness | Mechanism | Verify with |
|---------|-----------|-------------|
| claude | `~/.claude/skills/forge/` (the source) | — |
| openclaude | symlink in `~/.openclaude/skills/` | `openclaude skills list --json` |
| codex | symlink in **`~/.codex/skills/`** | `codex exec "do you have a skill named forge?"` |
| opencode | **nothing** — it scans `~/.claude/` itself | `opencode debug skill` |
| antigravity | `agy plugin install` of a wrapper | `agy --print='/forge'` |
| grok | **nothing** — it lists `~/.claude/skills` itself | `skills` in a headless `init` line |

Three traps here:

- **Codex reads `~/.codex/skills`, not `~/.codex/prompts`.** A file in `prompts/` gives you
  a slash command that expands as text; it does not register a skill.
- **opencode needs no install.** It scans `~/.claude/` and `~/.agents/` for skills unless
  `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` is set, so it finds this directory on its own.
  Adding a `~/.config/opencode/command/forge.md` on top produces a duplicate entry.
- **Antigravity ignores `~/.gemini/skills` entirely** — it cannot see skills placed there,
  including any already in that folder. Skills load only inside a plugin: a directory with
  `plugin.json` **at its root** (not in `.claude-plugin/`, which is why `agy plugin validate`
  rejects a Claude plugin as-is) plus a `skills/` subdirectory. `forge-install.sh` generates
  that wrapper at `~/.gemini/forge-plugin`.

And one ongoing caveat: `agy plugin install` **copies** into `~/.gemini/config/plugins/forge/`
rather than referencing the source. The other four track this directory live, so an edit to
`SKILL.md` reaches them immediately — antigravity keeps running whatever it copied. Re-run
`forge-install.sh` after any change, or agy silently runs a stale version.

### Frontmatter must be strict YAML

Codex parses skill frontmatter strictly and **refuses to load the skill** on any error,
logging `failed to load skill ...: invalid YAML` and then behaving as if it does not exist.
Claude Code and OpenClaude are lenient and load the same file fine, so a frontmatter bug can
sit unnoticed in four harnesses and break the fifth.

The one that bit this skill: a value that *starts* with a quoted scalar and then continues
unquoted is invalid YAML —

```yaml
argument-hint: "<goal>" --dwarf <alias>      # invalid: text after the closing quote
argument-hint: '"<goal>" --dwarf <alias>'    # valid: whole value quoted
```

Note codex reports line numbers relative to the frontmatter body, i.e. excluding the opening
`---`, so its "line 3" is the file's line 4.

## Adding a harness

1. Add rows to `registry.tsv` mapping aliases to that harness's model ids.
2. Add its effort ladder to `ladder_for()` in `scripts/forge-dispatch.sh`, or leave it
   empty for verbatim forwarding.
3. Add a `case` branch building its argv, covering: non-interactive flag, model, effort,
   working directory, the non-yolo containment for each role, the yolo bypass, and how the
   prompt is delivered.
4. Add it to `known_harness()` and to `doctor()`.
5. Prove it end to end before trusting it — a dwarf that really edits a scratch repo and a
   QA that really finds a planted bug. Every trap documented above was found this way and
   none of them were visible from `--help`.
