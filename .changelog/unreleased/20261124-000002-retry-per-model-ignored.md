---
type: fixed
breaking: false
audience: user
commits: 
---
A `retry_per_model` file that does not hold a number from 1 to 99 (edited by hand) is now named as ignored in the plan table and at run start, as the scheduler already treated it, instead of being shown as the setting with a worst case computed as if it applied (#158).
