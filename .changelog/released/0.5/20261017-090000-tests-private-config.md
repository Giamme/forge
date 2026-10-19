---
type: fixed
breaking: false
audience: internal
commits: 
---
The test suite now runs every test process against a throwaway forge config home, so no test, including one still being written, can read your real `jev.json` (and its TypeSafe key) or add its temp dirs to your runs registry.
