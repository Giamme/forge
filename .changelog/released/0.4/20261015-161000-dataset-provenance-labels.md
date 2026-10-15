---
type: changed
breaking: false
audience: user
---
The run dataset now records which forge produced every event: besides the release, a fingerprint of the exact scripts, references and registry a run used (`code_sha`) and the checkout's commit (`forge_rev`), taken when the execution starts and flagged if the code changed before it ended. Imported runs no longer carry the importer's version. The store upgrades itself on first use. `forge dataset export` uses this to label outcomes more honestly: an attempt that says nothing about the implementer (a reviewer timeout, a harness crash, no verdict) is left out and counted instead of letting a later success count as the first attempt's, reviews gain a severity label (`none` to `P0`) when every finding is graded, routing and retry states gain an explicit task-size block, and each case's metadata says which forge decided and ran it and how the task went (attempts, raw verdict, gate pass, human accept, why it stopped). The orchestrator now records the decompose level it split at even when the user named none.
