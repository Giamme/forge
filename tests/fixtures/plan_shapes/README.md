The 16 decomposed plans (`tasks.tsv`) of the #86 SWE-bench Pro benchmark runs, named `<set>-<repo>-<setup>.tsv`. Their shapes, by forge's greedy waves (deps in earlier waves, no file overlap within a wave):

- one task: easy-ansible-decomp, easy-element-decomp, easy-element-orch
- chain (widest wave 1): easy-ansible-orch, easy-openlibrary-decomp, easy-openlibrary-orch, hard-ecr-lite, hard-seekbar-decomp, hard-seekbar-lite
- branches (some wave has 2+ tasks): easy-flipt-decomp, easy-flipt-orch, easy-vuls-decomp, easy-vuls-orch, hard-ecr-decomp, hard-metrics-decomp, hard-metrics-lite
