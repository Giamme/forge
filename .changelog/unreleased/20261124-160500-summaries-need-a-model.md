---
type: fixed
breaking: false
audience: user
commits: 
---
Switching task summaries on for a plan that has no summaries model is now refused, with the command that sets one (`forge control set <plan> summaries-model <spec>`), instead of being accepted and then skipping every summary silently. `summaries-model default` is refused while it would leave a planned plan with no model. A new `forge control set <plan> brief-model <spec>` changes the brief model of a planned plan, and `forge config set brief.model` names it. forge-ui shows the run's summaries model next to the Summaries switch, with a field that sets it (#163).
