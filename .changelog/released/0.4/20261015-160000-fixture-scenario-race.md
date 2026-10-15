---
type: fixed
breaking: false
audience: internal
---
The fake agent CLI in the test suite numbers its calls under a lock, so two tasks sharing a scenario key no longer replay the same call twice. That race made `test_accept_that_cannot_merge_exits_6_and_records_nothing` fail intermittently under load.
