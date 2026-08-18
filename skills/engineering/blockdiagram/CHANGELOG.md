# Changelog — `blockdiagram` skill

## 0.7.0 — a draft setting
`Diagram(..., effort="fast")`, and `--fast` on the graph bridge. Over the eight samples:
**3.5x quicker for 21% more crossings** (23.2s → 6.6s, 77 → 93). On a 40-box hierarchy,
46s → 14.2s for 25 → 40 crossings. Default is unchanged and byte-identical.

Fast mode shortens the ladder -- two ordering sweeps instead of eight, one polish round
instead of three, no adjacent-swap sweep, one round of module moves, no rip-up. The
adjacent-swap sweep is where the time actually went on a fan-heavy diagram: one full
re-route per swap, per side, per round, so twenty wires off one box cost twenty re-routes.
Keeping the whole-side reversal (a fan attached in the wrong sense is one move from right)
and dropping the swap sweep is most of the saving.

What it does NOT trade is legality. The router's search for a legal path is identical at
either setting, and the self-test asserts that fast mode produces no wire under a box, none
along another and none looping. Narrowing that search was tried first -- a dozen tracks
instead of forty, five instead of ten -- and rejected: it was 7x quicker and put **21**
wires under boxes or along other wires across the samples. Crossings are an aesthetic cost
and fair game for a draft; a wire hidden under a block is a lie about the design.

Two honest notes. Capping the ladder ALONE, without touching the polish sweep, bought only
1.2x -- the ladder's rounds already stop early when a round finds nothing, so the knob that
looked obvious did almost nothing. And fast is not uniformly worse: one sample came out at
4 crossings against full effort's 9. It is less thorough, not systematically worse.

## 0.6.1 — five times faster, same drawings
A 40-box hierarchy took **229 s**; it now takes **45.8 s**, and the self-test went from
over two minutes to **5.6 s**. Every one of the eight sample diagrams renders to a
**byte-identical** SVG, which is how each step was checked: same winner, same picture,
less work to prove it.

The router's inner clearance test was 95% of the runtime (2.9 million calls, 281 million
`abs()` on a 21-box diagram). Four changes, none of which alters what gets drawn:

- **Cheap facts before expensive ones, and pruning.** Length, label room, the arrowhead
  approach and the no-loop rule need nothing but the path itself. Once a clean path is in
  hand, a longer candidate cannot win whatever it crosses -- so it never reaches the
  obstacle scan, and with the length known arithmetically it never even reaches the
  evaluator.
- **Per-segment memo.** The five-segment candidates are a *product* of tracks, so the
  same first run recurs a hundred times and the last run ten. Scanning each distinct
  segment once takes a wire from ~5000 segment scans to ~1200. Sound because a wire is
  remembered only after its path is chosen, so obstacles do not move mid-call.
- **Branch pruning on shared segments.** A candidate sharing an illegal segment with a
  rejected one is itself illegal, so the way out of the source, the way into the target
  and the drop to the middle band are each tested once and whole branches of the product
  are dropped.
- **A box test before the exact ones** in the no-loop check, which was running two exact
  geometric tests 14 million times a diagram; and obstacles indexed by coordinate band
  rather than scanned in full.

What was NOT done, with the numbers, since it is a quality decision rather than a
performance one: the remaining time is ~600 full re-routes by the crossing ladder.
Capping module movement to one round takes the 40-box tree to 28.5 s for 5 more crossings
(25 → 30); dropping it entirely gives 23.1 s and 117 crossings. It is left at full
strength.

## 0.6.0 — cycles, families, and rails
Ported from a parallel line of work on the same engine, each change measured against the
same eight-diagram sample set (77 crossings before, 77 after — see below for what moved
and what did not).

- **Cycles are broken before ranking.** Ranking straight through a feedback loop reversed
  the flow: a fetch/decode/exec/wb pipeline with a `redirect` came out with `exec` at
  column 0, LEFT of `fetch`, because the longest path ran round the loop. Back edges are
  found depth-first, ranking uses the DAG that is left, and the loop is then drawn as the
  flow convention already says it should be — out the right, round, back in the left.
  Pipeline columns: `[1, 2, 0, 1]` → `[0, 1, 2, 3]`.
- **`rail(src, dsts, ...)` — a global signal is a tap per block, not a wire per block.**
  Clock and reset across five levels of hierarchy measured in the *hundreds* of crossings
  as wires, and no router can help: those wires genuinely do go everywhere. A spec draws a
  tap at each block and names the source once. Taps take no part in ranking or placement.
  A tap's far end sits off the block deliberately, so **the stub carries `data-tap` with
  its source** and `verify_diagram.py` attributes it — a 5-tap rail round-trips with zero
  problems, where otherwise every tap would have read as a wire ending in space. The graph
  bridge groups a clock-kind fan of 4+ from one source into a rail automatically, printing
  what it grouped; `--no-rails` opts out.
- **Ordering sweeps run one side at a time**, alternating (forward by parents, backward by
  children), instead of taking the median over both at once. Averaging both does not
  converge: a box is pulled towards its own children while its siblings go elsewhere, and
  in a containment tree that scattered every parent's children across their rank.
- **Wrapping breaks on family boundaries.** Slicing a wide rank every `cap` boxes cut
  through the middle of a parent's children, and a parent whose children straddle two
  columns must send wires into both. On a 40-box tree: 3 of 13 families were cut, now 0,
  and no parent's children are out of order in their column.

What that bought, honestly: the tree went 27 → 25 crossings and the sample set did not
move (77 → 77, 62.3s → 61.3s). The sample diagrams are single-parent fan-outs, where one
family is larger than a column and family-aware wrapping has nothing to group. The
invariant is still worth having — a reader can see which children belong to which parent —
but the remaining crossings on a wrapped tree are the wrapping itself: a parent ends up
several columns from its children's chunk. Fixing that needs parent-adjacent columns
(nesting), which is a placement strategy, not an ordering rule.

## 0.5.1
- **The router now gives every label somewhere to sit.** `gap_x` was sized so a name
  fits the whole gap between two columns, but a 3-segment route splits that gap into two
  half-runs, and a name that fits the gap fits neither half — which is why wires came out
  unnamed. Among paths that cross equally little, the router now prefers one whose
  longest horizontal run can actually hold the label. 72 → 81 of 85 connections carry a
  readable name and width, and four of the eight samples round-trip completely.
- **`lint()` checks the rules on the finished geometry**, not only where they are
  generated: self-crossing wires, arrowheads entered from the side, the left/right flow
  convention, and any wire whose name could not be placed (naming the wire and saying
  why). A rule enforced only at the point of generation regresses silently — the router
  just stops producing the good case and nothing says so.

## 0.5.0
- **The crossing ladder is complete**: reorder exits, reorder entrances, *move the
  modules* (swap a pair, change a row, change a column), route the wire differently,
  re-route its neighbours. Module movement was the missing rung and the decisive one —
  the busiest fan-out sample went 25 → 6 crossings where reordering alone reached 22.
- **Wires may not loop.** A path that crosses or doubles back over itself reads as two
  wires; non-adjacent segments of one path must stay clear of each other.
- **Flow convention**: outputs leave on the right, inputs arrive on the left, always.
  A feedback path goes round rather than attaching to the nearest side.
- **Wire colour by kind** (data / control / clock / interrupt), classified from the
  signal name, with a legend when more than one kind is present.
- Scoring puts hard-rule violations above crossings, so a module move can never buy
  two fewer crossings at the price of a wire hidden under a block.
- Removed a dead `_crossings()` that counted crossings from row order and was silently
  shadowed by the geometric one.
- SKILL.md now states the rules explicitly.

## 0.4.0
- **Wires enter the arrowhead from behind.** `orient="auto"` already aimed each head
  along its final segment, so the axis was right — but where that segment was shorter
  than the head is long, the corner before it fell inside the triangle and the wire
  visibly joined the point from a slanted side. 53 of 85 arrowheads in the sample set
  were like that. The router now requires a final approach at least as long as the
  arrowhead; all 85 enter through the flat back.
- **`_untangle()` reshuffles which wire attaches where**, to reduce crossings: a
  barycentre sweep followed by whole-side reversal and adjacent-swap improvement,
  keeping the best arrangement seen. 38% fewer crossings over the sample set
  (144 → 89). It can never return a worse arrangement than not running it.
- The router's inner loop keeps placed wires split by orientation, which made
  untangling (dozens of re-routes per diagram) affordable: 18.1s → 2.1s on a 16-wire
  case.
- `lint()` re-routes through `_untangle()`, so it reports on the geometry that is
  actually drawn rather than a different routing of the same diagram.

## 0.3.0
Wires obey three rules, and the drawing is checked against its own source data.

- **The router treats placed wires as obstacles.** It previously tested a candidate path
  against BOXES only, so wires were invisible to each other: every wire that wanted a
  gutter got it, and buses were drawn one on top of another. Corridors are now divided
  into tracks, so a gutter carries several wires side by side, and among the legal paths
  the router takes the one with fewest crossings, then the shortest.
- **Level ends no longer skip the check.** Two boxes level with each other took a
  straight line drawn without asking what stood between them. In a 12-box fan-out a
  single such wire caused four of the overlaps and three of the wires under blocks.
- **A wire can no longer cross its own source or target block.** The router excluded a
  wire's own two boxes from its obstacle list; all that bought was permission to draw a
  stub straight back through the block it had just left.
- **Labels are placed where they can be read**: never on a block, never on another
  label, and always nearer their own wire than any wire with a different name. Positions
  are rounded before checking, because the rounded value is what gets drawn.
- **Widths use Verilog notation** — `d_bits_data [128]`, not `128b`.
- `verify_diagram.py` recovers connections, names and widths from the finished SVG and
  compares them with the data the diagram was built from. `lint(reroute=False)` checks
  geometry as it stands, so the detectors stay testable now that the router will not
  produce those violations by itself.

Measured over eight generated samples (3 to 21 connections): wires under blocks,
wires running along other wires, and labels on blocks are all **zero**. 72 of 85
connections carry a readable name and width; the shortfall is crowded fan-outs where
several wires share one name and their labels would collide, and the verifier reports
each one rather than the diagram implying it is named.

## 0.2.0
- SKILL.md documents **autoplace** (already in the engine, selftest-covered, previously
  undocumented): `Diagram("Title")` with no cols/rows + `d.node(id, label, ...)` seeds placement
  from connectivity (layered ranks, barycenter ordering, centered columns); hand-tune by calling
  `d.autoplace()` then overriding `d.boxes[id].col/row` before `save()`. Manual and positionless
  boxes do not mix — all manual or all auto (+ post-tune).
- Governance section + this CHANGELOG + OWNERS; selftest is the pre-ship gate for engine changes.

## 0.1.0
- Initial skill: Python DSL engine (grid placement, edge-anchored orthogonal routing, lane
  offsets, auto-fit boxes, ports, 3-tier line weights, house palette), PNG render + geometric
  lint on every save, references (conventions, aesthetics).
