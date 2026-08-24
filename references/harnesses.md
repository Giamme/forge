# Harness reference

Per-CLI invocation details behind `scripts/forge-dispatch.sh`. Read this when a dispatch
fails, when adding a backend, or when you need to know what a role can actually do.

Everything below was verified by running the CLIs on this machine, not read from docs —
several of these behaviours are undocumented and a few contradict what the `--help` text
implies.

## Contents

- [At a glance](#at-a-glance)
- [codex](#codex)
- [claude / openclaude](#claude--openclaude)
- [opencode](#opencode)
- [antigravity (agy)](#antigravity-agy)
- [Adding a harness](#adding-a-harness)

## At a glance

| Harness | Non-interactive form | Model flag | Effort flag | Full bypass |
|---------|----------------------|------------|-------------|-------------|
| `codex` | `codex exec` | `-m` | `-c model_reasoning_effort=<e>` | `--dangerously-bypass-approvals-and-sandbox` |
| `claude` | `claude -p` | `--model` | `--effort` | `--dangerously-skip-permissions` |
| `openclaude` | `openclaude -p` | `--model` | `--effort` | `--dangerously-skip-permissions` |
| `opencode` | `opencode run` | `-m provider/model` | `--variant` | `--auto` |
| `antigravity` | `agy --print=...` | `--model` | `--effort` | `--dangerously-skip-permissions` |

Effort ladders, low to high:

- codex — `low medium high xhigh max ultra`
- claude — `low medium high xhigh max`
- openclaude — `low medium high xhigh max ultracode`
- antigravity — `low medium high`, and **per-model**: see below
- opencode — no fixed ladder; `--variant` is provider-specific and is forwarded verbatim

## codex

```
codex exec -m <model> -C <repo> --skip-git-repo-check -o <last-message-file> \
  -c model_reasoning_effort=<effort> (--approve-for-me | --dangerously-bypass-...) "<prompt>"
```

**It does not validate the effort.** `-c model_reasoning_effort=bogus` starts a run and
prints `reasoning effort: bogus` in its own header without erroring. A typo therefore costs
a full run at some unintended level, with nothing in the output saying so. This is the main
reason forge validates efforts itself before dispatch.

**It blocks on stdin.** `codex exec` prints `Reading additional input from stdin...` and
waits, because a piped stdin is appended to the prompt as a `<stdin>` block. Headless that
looks like a hang rather than an error, so the prompt goes on argv and stdin is redirected
from `/dev/null`.

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
<harness> -p --model <model> --add-dir <run-dir> --effort <effort> \
  (--permission-mode auto | --dangerously-skip-permissions) [--disallowed-tools ...]
```
with the prompt delivered on **stdin**.

**The prompt must not go on argv when a variadic flag precedes it.** `--disallowed-tools`,
`--allowed-tools` and friends are variadic, so a following positional is consumed as
another value. In practice the prompt is parsed word by word into tool names — you get a
pile of `Permission deny rule "the" matches no known tool` warnings and then
`Error: Input must be provided either through stdin or as a prompt argument`. Passing it on
stdin sidesteps this entirely and also lifts the argv length limit, which matters once a
diff is inlined into a review prompt.

**Permission modes are not interchangeable.** Measured on this machine with a prompt that
asks the agent to run `python3 -c "print(6*7)"`:

| Mode | Can edit files | Can run shell |
|------|----------------|---------------|
| `acceptEdits` | yes | **no** |
| `dontAsk` | — | **no** |
| `auto` | yes | yes |
| `bypassPermissions` | yes | yes |

Forge uses `auto` for non-yolo roles. Under `acceptEdits` a dwarf can write code but cannot
run the tests that would tell it whether the code works — one such run reported
"executable verification was delegated because headless mode blocked local Bash approval",
i.e. it shipped unverified. QA additionally gets `--disallowed-tools "Edit,Write,NotebookEdit"`
so it can read and reproduce but cannot quietly repair the diff it is supposed to be judging.

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
opencode run -m <provider>/<model> --dir <repo> --variant <effort> --auto [--agent plan] "<prompt>"
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
