---
type: added
breaking: false
audience: user
commits: 
---
At run start forge compacts the repository's idle runs in the background and warns when its forge files passed a size you set. `forge config set storage.compact auto|off` (default `auto`) and `storage.warn-gb N|off` (default 20) control it; a run never waits for it (#139).
