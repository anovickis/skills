# Changelog — `blockdiagram` skill

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
