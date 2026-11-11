---
type: added
breaking: false
audience: user
commits: 
---
Reviewers now end each finding with three facts: its reach (main path, realistic or unusual), its origin (new in this change or already there) and their evidence (ran it, traced it or suspected). The facts are recorded in shadow. No verdict changes, and `forge issues`, the ledger and forge-ui read a finding with them exactly as one without. forge-ui shows them beside each finding in the diff. The new `forge dataset facts` command reports how often two reviewers of one attempt agree on reach compared with severity. It also replays candidate rules on past reviews, such as "unusual reach does not block unless P0" and "existing problems do not block", and lists the findings each rule would have let through (#119).
