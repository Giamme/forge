## Planning

By default **you** plan: before dispatching, decide the approach and put it in the prompt,
so the dwarf builds the agreed thing rather than inventing a shape nobody has seen. In a
decomposed run that matters more, because the capsule prevents two dwarves from touching
one *file* but nothing stops them inventing incompatible *interfaces* at a seam they share.

`--planner <spec>` hands that stage to a named model instead — one dispatch for the whole
run, never one per task. A single planner is the point: one mind designs both sides of every
seam, where N independent planners would recreate the problem. Effort defaults to `xhigh`,
since a bad plan is executed at full price by every dwarf downstream of it.

```bash
bash <skill_dir>/scripts/forge-dispatch.sh planner <spec> \
  --repo "$REPO" --run-dir "$FORGE_RUN" --prompt-file "$FORGE_RUN/planner.prompt"
```

The planner is instructed to read only and uses QA permission flags. These flags alone do not enforce filesystem immutability.
Ask it for the approach only: in a decomposed run, `tasks.tsv` rows plus a few lines of
approach per task; in a single-task run, just the approach. Write each task's approach to
`tasks/<id>/approach.md`, where the runner picks it up for both the dwarf and qa.

Show the approach before dispatching and let the user change it. That gate is the last
moment the plan is free; after it, changing the plan costs a whole run.

