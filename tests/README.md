# Offline validation

Run `bash tests/check.sh`. Python's standard-library unittest suite uses temporary Git
repositories and fake model CLIs. PATH is restricted to those fakes and an explicit utility
allowlist, so installed model clients cannot be used accidentally. No provider account is needed.
CI runs the same checks on macOS (system Bash 3.2) and Linux.

`skill-cases.json` contains invocation and approval scenarios with expected observable behavior.
Use these for a manual or independent skill evaluation: provide each request and SKILL.md,
allow only fake dispatch, and compare actions with the rubric. Mechanical checks validate
reference links and scenario structure; they do not prove a model follows the instructions.
Live model routing, authentication and provider behavior are separate, explicitly requested checks.
