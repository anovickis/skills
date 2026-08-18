# Changelog — `blockdiagram` skill

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
