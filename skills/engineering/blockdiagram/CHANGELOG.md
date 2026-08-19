# Changelog — `blockdiagram` skill

## 0.7.2 — round the outside, and the room to do it
A wire with nowhere legal to go was drawn straight through whatever stood in the way. Ten
boxes and seventeen wires were enough to produce it: **one clock fan across three columns,
three wires under a box.** On a five-level hierarchy with twenty cross-level wires it was
five FAILs, all from a single clock leg — four boxes crossed and one stacked run.

The router already searched three-segment paths and then five-segment ones, and when none
was legal it kept the simple shape and let the lint report it. What it never tried is the
one space a figure is guaranteed to have: the band above every box and the band below
every box. Those corridors existed all along — `_free_spans` returns them — but the search
takes the ten tracks nearest the wire's own midpoint, so the outer bands always sorted out
of range. They were never tried, not rejected.

- **A detour, as the last resort only.** Out to a vertical track, along the outer band past
  everything, back in at the far side. Reached only when no interior path is legal *and*
  the path that would otherwise be drawn passes under a non-endpoint box, so a wire the
  interior can serve is never sent the long way round.
- **The band is given room when it needs it.** A detour is worthless if the band is full,
  and a flat wide figure has only those two horizontal corridors — five tracks for the
  whole drawing at the default gap, which one fan can take. When violations survive the
  ladder, the band grows (8 lanes, then 16) and the ladder runs again, judged on
  `_quality()`: violations first, crossings only as the tie-break, so a wider band is never
  kept for prettiness.

Measured, worst first. Every case that was FAILing is now clean at full effort:

| case | before | after |
|---|---|---|
| clock fan, 3 columns (10 boxes, 17 wires) | 3 FAILs | **0** |
| the same at 13 boxes, 23 wires (two shapes) | 16 FAILs | **0** |
| 17 boxes, 31 wires | 51 FAILs | **0** |
| five levels + 20 cross-level wires (48 boxes, 67 wires) | 5 FAILs | **0** |
| the eight sample diagrams | 0 FAILs, 77 crossings | 0 FAILs, **76** |
| merged hierarchy (53 boxes, 52 wires) | 0 FAILs, 103 crossings | 0 FAILs, **71** |
| five-level SoC (45 boxes, 82 wires) | 0 FAILs, 199 crossings | 0 FAILs, **186** |

The last two were already legal and got better anyway: a wire sent round the outside is a
wire no longer cutting through the corridors, so the figures that were merely *crowded*
gained too -- 103 crossings to 71 on the 53-box merge.

Every case repaired above needed only the FIRST escalation step, 8 lanes. The 16-lane step
has never yet been the one that helped; it costs nothing when 8 works (the loop stops as
soon as the figure is legal) and is kept as margin rather than as a measured need. The
five-level SoC pays for it in time, though: ~6.5 min to ~14, because it takes a second walk
of the ladder to get there. A figure that is already legal after the first walk pays
nothing.

Three things stated plainly rather than buried.

**Two of the eight figures move.** 03 goes 7 crossings to 12, 04 goes 9 to 3 — net one
better, both still FAIL-free, and one canvas a single pixel narrower. Neither draws an
illegal wire in its *final* arrangement, so nothing was repaired there; the detour also
applies while the ladder is exploring, so it shifts which arrangement the ladder settles
on. Byte-identity was reachable only by making the search and the final draw disagree with
each other, which is how silent bugs get made.

**The gates were each bought with a wrong answer.** Triggering on the router's own broader
notion of illegal — which refuses a path running within a few px of another wire, tighter
than the lint's tolerance — moved clean figures for nothing. Widening the band after the
*first* route rather than after the ladder did the same, because the ladder repairs most
violations by itself. Both were caught by re-measuring the eight, not by reading the code.

**Fast effort is still not clean on the worst case:** the 17-box fan keeps 15 FAILs at
`effort="fast"` against 99 before. The draft setting's shortened ladder cannot reach the
arrangement full effort finds, so on that input `fast` does trade legality, contrary to what
0.7.0 claims for it. Improved, not fixed, and it predates this change.

Selftest 42 checks -> 47; ALL PASS. Corpus of eight full-effort: 43.8s -> 44.9s (the ladder
re-runs only for a figure that is still illegal after it).

## 0.7.1 — a draft that says it is one
The effort setting now travels with the output three ways: the lint summary names it
(`lint: OK (effort=fast)`), a fast draw adds a `NOTE` line saying what was traded, and the
SVG carries `data-effort` so the file itself records how it was drawn.

The hazard of two settings is not the code, it is a quick draw passing for a finished
figure weeks later, when nobody remembers which it was. `NOTE` deliberately is not `WARN`
-- the setting was asked for, so it is provenance rather than a complaint, and it must not
turn a clean draft into "warnings only". That exposed a small bug in the summary line while
adding it: it read "OK" only when the report was entirely empty, so a NOTE alone would have
printed "warnings only". It now looks at failures and warnings, not at emptiness.

Full-effort geometry is unchanged: all eight samples render identically bar the one
provenance line.

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
