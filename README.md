# Agent skills

Claude Code skills I use for hardware and systems work — RTL, verification, power
modelling, and the documents that come out of them.

Two are mine and are the reason this repo exists. The rest are Matt Pocock's excellent
[mattpocock/skills](https://github.com/mattpocock/skills), which this repo started as a
copy of; they are MIT-licensed and kept as they are, because they are good and I use them.
See [LICENSE](./LICENSE) — the copyright on that work is his.

## My skills

### [blockdiagram](./skills/engineering/blockdiagram/SKILL.md)

Technical SVG block diagrams from a small Python DSL, for hardware and spec documents.
Explicit grid placement or connectivity-driven autoplace; orthogonal routing that is
*computed*, not hand-nudged. Boxes auto-fit their text, arrows anchor at box edges,
parallel runs get their own lanes, bus edges carry width and bandwidth labels, and
third-party IP renders as grey dashed boxes.

What makes it worth having over a general diagramming tool is that **the drawing is
checked**. Every build renders a PNG and runs geometric lint: box overlap, text overflow,
a wire drawn under a box, two wires sharing a corridor, an arrowhead entered from the
side, glyphs Arial cannot render. It refuses to quietly hand you a picture that lies
about the design.

- Wires that cannot get through the interior go **round the outside**, along the band
  above or below every box, and the band is widened when it has no room left.
- A **containment hierarchy** is wrapped by subtree rather than by rank, so a parent
  always sits one column left of its children. Wrapping ranks instead cost 65 crossings
  on a 46-box tree; banding draws the same tree with 6.
- `effort="fast"` trades crossings for time in the draw-look-adjust loop — **3.5× quicker
  for ~20% more crossings** — and never trades legality, which the self-test pins.
- 52 self-tests, and every claim in [the changelog](./skills/engineering/blockdiagram/CHANGELOG.md)
  is a measurement rather than an intention.

Pairs with a markdown-to-docx spec pipeline, and reads a knowledge graph directly via
`scripts/graph_to_blockdiagram.py` so an RTL hierarchy can be drawn instead of retyped.

### [git-untangle](./skills/engineering/git-untangle/SKILL.md)

Explains what state a git repository is *actually* in, in plain language, and what to do
about it. Most git confusion is not a hard problem — it is an unreadable status. `git
status` will call a submodule "modified" when nobody edited anything, will not mention
that the branch you track was deleted on the remote, and will not tell you which of three
remotes a push would reach. Read-only diagnosis first, then the specific recovery.

Use it when a push is rejected, an upstream vanishes, files nobody touched show as
modified, HEAD is detached, branches have diverged, or you just want to know whether it is
safe to push.

## Install

```bash
npx skills@latest add anovickis/skills
```

Pick the skills you want and the agents to install them on. Or just copy a skill
directory — they are self-contained, and `blockdiagram` needs only Python plus `inkscape`
for the PNG render.

The upstream engineering skills below expect a one-time per-repo setup
([`setup-anovickis-skills`](./skills/engineering/setup-anovickis-skills/SKILL.md)),
which asks which issue tracker you use, your triage labels, and where docs should go. Run
it before `to-issues`, `to-prd`, `triage`, `diagnose`, `tdd`,
`improve-codebase-architecture`, or `zoom-out`. `blockdiagram` and `git-untangle` need no
setup.

## Upstream skills

From [mattpocock/skills](https://github.com/mattpocock/skills), unchanged. His
[write-up](https://github.com/mattpocock/skills#why-these-skills-exist) explains the
thinking behind them far better than a summary here would; the short version is that they
target the four ways agent-written code goes wrong — misalignment, verbosity, no feedback
loop, and architectural drift.

### Engineering

- **[diagnose](./skills/engineering/diagnose/SKILL.md)** — Disciplined diagnosis loop for hard bugs and performance regressions: reproduce → minimise → hypothesise → instrument → fix → regression-test.
- **[grill-with-docs](./skills/engineering/grill-with-docs/SKILL.md)** — Grilling session that challenges your plan against the existing domain model, sharpens terminology, and updates `CONTEXT.md` and ADRs inline.
- **[triage](./skills/engineering/triage/SKILL.md)** — Triage issues through a state machine of triage roles.
- **[improve-codebase-architecture](./skills/engineering/improve-codebase-architecture/SKILL.md)** — Find deepening opportunities in a codebase, informed by the domain language in `CONTEXT.md` and the decisions in `docs/adr/`.
- **[setup-anovickis-skills](./skills/engineering/setup-anovickis-skills/SKILL.md)** — Scaffold the per-repo config (issue tracker, triage label vocabulary, domain doc layout) that the other engineering skills consume.
- **[tdd](./skills/engineering/tdd/SKILL.md)** — Test-driven development with a red-green-refactor loop. Builds features or fixes bugs one vertical slice at a time.
- **[to-issues](./skills/engineering/to-issues/SKILL.md)** — Break any plan, spec, or PRD into independently-grabbable GitHub issues using vertical slices.
- **[to-prd](./skills/engineering/to-prd/SKILL.md)** — Turn the current conversation context into a PRD and submit it as a GitHub issue.
- **[zoom-out](./skills/engineering/zoom-out/SKILL.md)** — Tell the agent to zoom out and give broader context on an unfamiliar section of code.
- **[prototype](./skills/engineering/prototype/SKILL.md)** — Build a throwaway prototype to flesh out a design — a runnable terminal app for logic questions, or several UI variations toggleable from one route.

### Productivity

- **[caveman](./skills/productivity/caveman/SKILL.md)** — Ultra-compressed communication mode. Cuts token usage ~75% by dropping filler while keeping full technical accuracy.
- **[grill-me](./skills/productivity/grill-me/SKILL.md)** — Get relentlessly interviewed about a plan or design until every branch of the decision tree is resolved.
- **[handoff](./skills/productivity/handoff/SKILL.md)** — Compact the current conversation into a handoff document so another agent can continue the work.
- **[write-a-skill](./skills/productivity/write-a-skill/SKILL.md)** — Create new skills with proper structure, progressive disclosure, and bundled resources.

### Misc

- **[git-guardrails-claude-code](./skills/misc/git-guardrails-claude-code/SKILL.md)** — Claude Code hooks that block dangerous git commands before they execute.
- **[migrate-to-shoehorn](./skills/misc/migrate-to-shoehorn/SKILL.md)** — Migrate test files from `as` type assertions to @total-typescript/shoehorn.
- **[scaffold-exercises](./skills/misc/scaffold-exercises/SKILL.md)** — Create exercise directory structures with sections, problems, solutions, and explainers.
- **[setup-pre-commit](./skills/misc/setup-pre-commit/SKILL.md)** — Husky pre-commit hooks with lint-staged, Prettier, type checking, and tests.

## Layout

Skills live under `skills/` in buckets: `engineering/` for daily code work,
`productivity/` for non-code workflow, `misc/` for the rarely used. `personal/`,
`in-progress/` and `deprecated/` exist too and are deliberately *not* listed here — see
[CLAUDE.md](./CLAUDE.md) for the rule, which is that anything in the first three buckets
must appear both in this file and in `.claude-plugin/plugin.json`.
