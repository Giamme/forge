---
type: fixed
breaking: false
audience: user
commits: 6be6d4d
---
forge-ui: a live refresh no longer cancels a click, clears a text selection, replaces a focused `<select>` or closes a disclosure you opened. A redraw waits until you finish and then shows the latest data, and a long wait tells you newer data is there (#115).
