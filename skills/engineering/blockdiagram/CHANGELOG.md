# Changelog — `blockdiagram` skill

## 0.4.0 — wire classes, and one lane per wire
- **Wire classes are back**: `edge(..., cls="data"|"control"|"clock"|"interrupt")` draws
  in `#1F4E79` / `#B8860B` / `#6A5ACD` / `#A03030` with the arrowhead matched to its own
  wire, and a **legend** top-right for the classes present — dropped to its own line if
  it would run into the title, treated as an obstacle by the label placer, and checked
  by the lint. Two or more classes to earn a legend; an unknown class falls back to
  `data` instead of inventing a colour. The bridge classifies from the signal name
  (interrupt / clock+reset / control / data), so an uncoloured diagram means "names not
  extracted", the same reading as an unlabelled arrow.
- **One lane, one wire.** Lane positions were shared between wires whose stretches did
  not overlap. It measures as zero crossings and still looks wrong: two collinear runs
  with a gap between them read as one long wire that everything else crosses — which is
  exactly what a reader reported on a 9-wire fan that the metric called clean. A lane is
  now claimed outright; the corridor is sized for the whole bundle and the comb is
  anchored one step off the box instead of at the corridor centre, so it has the room.

## 0.3.0 — crossings
Measured on a corpus of eight real RTL-hierarchy diagrams (4–21 boxes, 98 wires):
**180 wire crossings and 20 lint FAILs before, 0 and 0 after**, plus 14 label
collisions found and cleared. Seven harder synthetic cases (chains, mid-graph hubs,
feedback loops, 16-way fans, labels too wide to fit) draw clean; the eighth is a
hand-placed bypass down a single column, which stays the author's call and is named
as such by the lint.

- **Crossings are counted geometrically** from the routed polylines. The old
  combinatorial estimate reported **zero** for every one of those eight diagrams,
  which is how 180 crossings shipped past the gate.
- **Lane order by travel distance**, not declaration order — the single biggest fix:
  a fan-out is now a comb. Lanes are packed per corridor position, so opposite-side
  wires cannot share a line; spacing steps down instead of overshooting a corridor,
  and a corridor is sized for the bundle that has to turn in it.
- **Arriving ends are ordered by reach**: of two wires arriving from the same
  direction, the one that travelled farther takes the outer anchor.
- **Fan-aware box sizing**: a side that hosts *n* wires is grown to keep them
  `FAN_MIN` apart, by claiming empty neighbouring cells (span) rather than inflating
  the whole row and stretching unrelated boxes with it.
- **Column-skipping wires get a cleared row** (Sugiyama dummy-node effect), run
  after span growth so a grown box cannot re-block the band it reserved.
- **Cycles are broken before ranking**, so a pipeline with feedback still reads
  left→right; **back edges are routed around the outside** (top/bottom channel,
  canvas space reserved, banks assigned so interleaved spans don't share one, and
  channels ordered by reach). Same-side routes (`T`/`T`, `B`/`B`, …) now run outside
  both boxes instead of back between them.
- **Labels are placed, not guessed**: on a straight run of their own wire, preferring
  the arriving run, anchored against the box edge, haloed, collision-checked
  widest-first — and the layout **spreads** if they don't fit, backing off when
  spreading isn't the problem. Label collisions are now lint WARNs.
- **Two measured passes in autoplace**: alternative rankings (a bypassed hub tried to
  the right of the boxes it feeds) and adjacent-swap row shuffling, each accepted
  only if the measured (faults, crossings) improves. A hand-placed diagram is never
  re-placed — lint reports instead.
- Lint FAIL messages name the escape hatch that fits the shape (same column vs same
  row). Selftest grew 15 checks covering all of the above.

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
