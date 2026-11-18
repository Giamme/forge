---
type: fixed
breaking: false
audience: user
commits: e67ab72, 3e2b06e
---
The planner refuses a reply nested more than 64 levels deep or holding a number over 4,300 digits the same way on every Python version, and forge-ui refuses a profile nested that deep. Before, Python 3.12 and later accepted the deep profile, and older Python 3.9 builds spent a long time parsing the huge number.
