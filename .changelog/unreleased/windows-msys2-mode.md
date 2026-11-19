---
type: added
breaking: false
audience: user
commits: 677c676
---
On Windows, forge can run inside MSYS2, where process groups, signals, file locks and symlinks behave as on Linux. It is an opt-in mode: set `FORGE_WIN_MODE=msys2` and start forge from Git Bash as usual, or start it from an MSYS2 shell. Setup and limits are in [Install](docs/install.md#windows-msys2-mode).
