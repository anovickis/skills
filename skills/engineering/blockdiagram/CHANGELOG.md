# Changelog — `blockdiagram` skill

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
