---
type: added
breaking: false
audience: user
---
`forge dataset outcomes` records what integrated work became, from your repo's history alone: for every task an `integrate` brought in, how many of the lines it added are still there 14 days later (`--horizon-days` to change it) and whether a later commit reverted it. It only reads git objects, never your working tree or index, writes nothing for a horizon not yet reached, and adds nothing when run again. The export turns it into the first label no reviewer decides: whether a merged diff survived. Verification results now also name the commit they verified.
