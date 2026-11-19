---
type: changed
breaking: false
audience: user
commits: 0d6dc07
highlight: true
---
First step toward native Windows support: file locking, safe file opening and process handling now go through one cross-platform layer that also works on Windows. Nothing changes on macOS or Linux, and Windows isn't usable yet: the Windows launcher, installer and docs come in the next part. Thanks to Kratoswer-byte for the port.
