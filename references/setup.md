# forge setup

`forge setup` asks, once, what a first run would otherwise ask piece by piece, and saves the
answers as defaults. `forge install` runs it at the end on a terminal (never again once it is done,
and never under `forge update`). Running it again offers the current answers as the defaults.

```
forge setup                  the guided pass, on a terminal
forge setup --defaults       the recommended answers, no prompts
forge setup --from FILE      the answers of `forge setup export` (FILE may be -), no prompts
forge setup show [--json]    every answer, where it is stored, and the command that changes it
forge setup export           the answers as JSON, for another machine
forge setup set KEY VALUE    change one answer
```

Without a terminal it never prompts: plain `forge setup` exits 2 and names `--defaults` and `--from`.
Setup never calls a model. It sends nothing anywhere unless an answer says it will: installing
Ripwire or Fractal downloads them, and setting up a decision model asks for a provider.

## For the orchestrator

Read `forge setup show --json` before a new run. When `done` is false, offer `forge setup` once,
before the first run, instead of asking its questions one by one; the user may decline. When `done`
is true, each answer below is the user's word for what it covers, so do not ask it again:

| answer | what you do |
|---|---|
| `models` | nothing: setup saved it as the default profile, which the runner applies (a default profile is the user's model choice) |
| `ui` | `ask`: ask the forge-ui question as usual; `always`: pass `--ui` without asking; `never`: pass `--no-ui` |
| `fractal` | `no`: pass `--no-fractal` without asking; `install`: ask the Fractal question as usual |
| `file_issues` | the `--file-issues` choice for leftover findings (`ask`, `auto` or `off`) |
| `sweep` | `P0`..`P3`: pass `run --sweep Pn`; `off`: do not add it |
| `reviewer_session` | `on`: pass `plan --reviewer-session`; a share such as `0.25`: `plan --reviewer-session-rate 0.25`; `off`: do not add it |

A flag the user gives for a run wins over a saved answer. Resume and retry keep the run's recorded
choices, as before.

## The answers and where they live

Every answer is stored where forge already reads it, so `forge config show` and the other commands
see the same values. Nothing setup saves is hidden: `forge setup show` lists it all.

| answer | values | stored in |
|---|---|---|
| `models` | a profile name (`balanced` first, when it runs here), `custom`, or `ask` (no default) | `decide.json` `default_profile` |
| `routing` | with `custom`: the implementer and reviewer of each tier, and a planner | the `setup` profile |
| `qa_threshold` | `P0`..`P3`, `none` (default `P2`) | the default profile's `policy` |
| `shadow_qa`, `shadow_rate` | a reviewer spec or `off`; the share of tasks it reviews | the default profile's `policy` |
| `brief`, `brief_model` | `off`, `advisory`, `gated`, `strict`; the model it needs | `decide.json` `features` |
| `summaries`, `summaries_model` | `off`, `on`; the model it needs | `decide.json` `features` |
| `retro` | `off`, `on` | `decide.json` `features` |
| `storage_compact`, `storage_warn_gb` | `auto`, `off`; gigabytes or `off` | `decide.json` `features` |
| `memory` | `commit`, `local`, `off`, per repository | that clone's `git config forge.memory` |
| `ui`, `ui_controls` | `ask`, `always`, `never`; `on` or `off` (read-only) | `setup.json` |
| `file_issues`, `sweep`, `reviewer_session` | as in the table above | `setup.json` |
| `update_check` | `on`, `off` | `setup.json` |
| `ripwire` | `ask` (offered at the first run), `install`, `never` | `setup.json` |
| `fractal` | `no` (runs do not ask about it), `install` | `setup.json` |
| `decide` | `off`, `on` (set one up now) | `setup.json`; the gates themselves with `forge decide` |

`setup.json` is `${XDG_CONFIG_HOME:-~/.config}/forge/setup.json`, next to `decide.json`.

When the chosen profile and the review answers say the same thing, setup selects the profile itself
(`forge config profile use balanced`). When they differ (a stricter threshold, a shadow reviewer, a
custom pick), it writes a user profile named `setup` with the base profile's routing and your policy,
and selects that. `ask` with no policy change clears the default, so each run asks for its models.

What the other answers change:

- `memory local` lists `.forge/` in the clone's `.git/info/exclude` (nothing to commit); `off` makes
  every run in that clone behave as `--no-memory`; `commit` leaves `.forge/` for you to commit
  ([memory](memory.md#committing-ignoring-disabling)). Setup asks it only inside a repository.
- `ui_controls off` starts forge-ui read-only, as `FORGE_UI_READONLY=1` does.
- `update_check off` stops the daily forge and Ripwire update checks, as `FORGE_UPDATE_CHECK=off` does.
- `ripwire never` silences the offer a run makes when Ripwire is missing; `forge ripwire install`
  still installs it.

## Settings a plan copies

Brief, summaries and retro are copied into a plan when it is planned, so changing them reaches
later plans only. Setup and `forge config set` say so when you change one. For a plan that is
already running, `forge control set <plan> summaries on|off` and
`forge control set <plan> summaries-model <spec>` change it now.

## Another machine

`forge setup export > forge-setup.json` writes every answer except the per-repository memory choice;
`forge setup --from forge-setup.json` applies them without a prompt. A file may hold only some
answers: the others keep their current values. Each model is checked against the registry; whether
its agent CLI is installed on that machine is what `forge-dispatch.sh doctor` shows.
