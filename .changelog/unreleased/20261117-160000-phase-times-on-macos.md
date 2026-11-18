---
type: fixed
breaking: false
audience: user
commits: 3020e13
---
On macOS with Python 3.9, such as the system `python3`, every phase of a run was recorded as taking about 0 seconds, so `report` and forge-ui showed wrong phase times. Phases are now timed on a clock that all of forge's processes share.
