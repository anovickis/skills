---
name: blockdiagram
version: 0.7.0
description: >-
  Author technical SVG block diagrams for hardware/spec documents from a small
  Python DSL with explicit grid placement (or connectivity-driven autoplace)
  and computed orthogonal routing.
  Boxes auto-fit their text; arrows anchor at box edges and parallel runs are
  pushed into separate lanes; arrowheads stay small; bus edges carry width/
  bandwidth labels; Cadence/3rd-party IP renders as grey dashed black boxes.
  Every build renders a PNG (inkscape) and runs geometric lint (box overlap,
  text overflow, stacked parallel lines, arrowhead size, non-Arial-safe glyphs).
  Use when asked to make/draw/update a block diagram, architecture diagram, or
  the figures in a design spec; pairs with a markdown-to-docx spec pipeline.
---

# Block diagram generator

Produces clean, consistent SVG block diagrams for specs. The engine encodes the
rules that otherwise make hand-authored SVGs come out "almost ok": edge-anchored
routing, no stacked parallel lines, labels that fit inside boxes, small
arrowheads, and labelled buses. Output SVGs drop straight into a spec's diagrams folder and embed into a
markdown-to-docx pipeline (native SVG + PNG fallback).

## The rules

What a correct diagram obeys. The engine enforces these; the self-test asserts them.

**Wires**
1. A wire never passes under a block.
2. A wire never runs along another wire. Crossing at 90° is fine; travelling together is not.
3. A wire never loops — it must not cross or double back over itself.
4. Minimise crossings, but never by breaking 1–3.
5. Point to point. A wire that merges into another lies about connectivity.
6. Every wire carries a name **and** a width, in Verilog notation: `d_bits_data [128]`.
7. Name a long wire at **both** ends, or the reader must trace it back.
8. A wire enters its arrowhead through the flat back, never through the sloped sides.
9. Arrowheads scale with the wire and sit on its centreline.
10. Colour groups wires by what they carry (data / control / clock / interrupt), with a
    legend whenever more than one kind is present. Unrecognised stays `data` — a wire in
    the wrong colour is worse than one in the default.
11. A **global** signal is a tap, not a wire per block. Clock, reset and scan go to
    everything; drawn as N long wires they cost a crossing per block and tell the reader
    nothing they had not assumed. Use `rail()` — see below.

**Blocks**
12. Inputs arrive on the LEFT, outputs leave on the RIGHT. A feedback path leaves the
    right edge, goes round, and re-enters the left. Bidirectional links may use either
    side, set explicitly.
12. Size a block from the wires entering and leaving it, so there is room to label them.
13. A block's description is inferred from its wires, never hand-written.
14. Wrap a wide rank over several columns — an 11-child fan-out in one column leaves the
    router nowhere to go.

**Labels**
15. Never on a block, never on another label.
16. A label must be nearest **its own** wire, or it belongs to neither.
17. Halo behind the text: dark glyphs on a dark bus are unreadable.
18. If there is nowhere legible, leave the wire unnamed and let the verifier report it.
    Never print a name over a block to imply the wire is labelled.

**To remove a crossing** — cheapest disturbance first
19. Reorder the wires leaving a block.
20. Reorder the wires entering a block.
21. Move the blocks: swap a pair, change a row, change a column.
22. Route that one wire differently.
23. Re-route its neighbours around it.

**Above all**
24. Read the finished diagram back and compare it with the source data — every
    connection, name and width recovered from the drawing itself. If it cannot be
    recovered, the diagram is wrong however clean it looks.
25. Lint says the picture is *sound*. The round-trip says it is *true*. Different
    questions; ask both.


## Use it
```python
import sys; sys.path.insert(0, "<this skill>/scripts")
from blockdiagram import Diagram

d = Diagram("Title", cols=3, rows=4)           # grid; boxes go in (col,row) cells
d.box("cpu", 0, 0, "CPU Cluster", ["4 cores", "private L1"])    # kind="block" (default)
d.box("mem", 2, 0, "MemorySystem", ["DDR controller"], kind="ip")  # grey dashed black box
d.box("hub", 1, 0, "Interconnect", ["Xbar"], rowspan=3, kind="emphasis")  # blue solid
d.note(0, 3, "Bus widths", ["TL: 64-bit beat", "Mem: 512-bit ×4ch"], colspan=3)
d.edge("cpu", "hub", label="TL 64b")           # arrow, optional bus label
d.edge("hub", "mem", label="AXI4")
report = d.save("diagrams/foo.svg")                            # writes svg + png, runs lint, prints report
```

## Rules the engine enforces
- **Grid placement**: `box(id, col, row, label, desc=[], kind=, colspan=, rowspan=)`.
  Columns/rows auto-size to the widest/tallest cell so boxes align.
- **Auto-fit boxes**: a box grows to fit its label + description lines.
- **Edge-anchored routing**: `edge(src, dst, label=, src_side=, dst_side=, shape=)`
  picks box sides from geometry, draws right-angle routes through gap corridors,
  aligns straight when boxes overlap on an axis, and offsets parallel runs into
  lanes. `shape="straight"` draws a direct (diagonal-allowed) line when clearer.
- **Ports** (optional): `box(..., ports=[("D0","R"),("clk","T")])` declares named
  edge points on a side; attach with `edge("a:D0", "b")`. Drawn only when declared.
- **Line weight (3 tiers)**: `edge(..., weight="signal"|"bus"|"fat")` =
  one wire (1 px) / a little wider / a fat bus; arrowheads stay tasteful.
- **Auto label spacing**: horizontal gaps widen so a bus label clears both boxes.
- **Small arrowheads** (size 8), **house palette** (`#1F4E79` emphasis, `#cfe0f0`
  block, grey dashed IP), **bus labels** on edges.
- **Measured text**: boxes size to real font metrics (PIL + Arial-metric font),
  with a heuristic fallback; glyph coverage checked against the actual font.

## Effort: draft while you iterate, full for the figure you ship
```python
d = Diagram("Title", effort="fast")     # default is "full"
```
Fast mode tries fewer *arrangements*: measured over eight samples, **3.5x quicker for 21%
more crossings** (23.2s → 6.6s, 77 → 93); on a 40-box hierarchy, 46s → 14s for 25 → 40.
Use it in the draw-look-adjust loop, then render the real figure at full effort.

What fast mode does **not** trade is legality. Nothing under a box, no wire along another,
no loops — the router's search for a legal path is identical at either setting. Narrowing
*that* was tried for speed and rejected: at a dozen tracks instead of forty the router
sometimes finds no legal path and falls back to a straight one, which put 21 wires under
boxes across the samples. Crossings are an aesthetic cost and fair game for a draft; a
wire hidden under a block is a lie about the design. The self-test asserts this.

Fast is not uniformly worse, either — with a shorter search it sometimes lands better
(one sample went 9 crossings to 4). It is less *thorough*, not systematically worse.

The graph bridge takes `--fast`.

## Cost
The router searches: for each wire it considers up to forty three-segment paths and, when
none is clean, a thousand five-segment ones, and the whole diagram is re-routed for every
candidate arrangement the crossing ladder tries. That is ~600 full re-routes on a 40-box
hierarchy. It is *bounded* work, not unbounded, but it is not free: budget a few seconds
for a dozen boxes and under a minute for forty. If a figure is slow, it is the ladder
earning its keep — on that 40-box tree, module movement alone takes the crossings from 117
to 25.

## Quality gate (always)
`save()` renders a PNG and runs `lint()`:
- `FAIL`: box overlap; orthogonal wire crossing a non-endpoint box; stacked
  (overlapping, non-crossing) parallel segments.
- `WARN`: text overflows a box (measured); glyph missing from the font (avoid
  missing-glyph boxes — stick to covered glyphs or convert text to paths).

Treat any `FAIL` as blocking. After a clean build, **still open the PNG and look**
— the lint catches geometry, not aesthetics (it will not catch a clipped/overflowing
label, a title running off the canvas, or a legend sitting on top of a curve; read the
rendered PNG and confirm every label fits and nothing collides).
Run the engine self-test with `python3 scripts/blockdiagram.py --selftest`.

## Passing lint the first time (learned failure modes)
Three FAILs account for almost every failed build. Design the placement to avoid them —
the FAIL message now names the offending boxes/edges and the fix:
1. **Wire crosses a non-endpoint box.** A straight edge between two boxes in the same row
   (or column) runs *through* any box sitting between them. Fix, in order of preference:
   put the connected boxes in **adjacent** cells; or route out of a free side with
   `src_side=`/`dst_side=` (leave, say, the top and come back down); or move the middle box
   to another row/column. A "hub" box that talks to many others belongs **centrally**
   (adjacent to all of them), not at one end of a row.
2. **Stacked parallel segments.** Two edges sharing one corridor overlap into a single
   thick line. Fix: do **not** draw duplicate/parallel edges for the same relationship —
   draw one representative wire (a fan of identical lanes reads as one bus anyway); give
   sibling edges distinct sides; or space the boxes so each gets its own lane.
3. **Box overlap.** Almost always a `colspan`/`rowspan` collision — no two boxes may claim
   the same cell.

Iterate placement until `lint: OK`; don't reach for `shape="straight"` to silence a
crossing — straight edges are *exempt* from the crossing check, so they will silently run
through a box. Use `straight` only for a short diagonal with nothing between the endpoints.

## Autoplace (positionless start)
The engine can seed the placement from connectivity — use it to get a good first
layout, then hand-tune:
```python
d = Diagram("Title")                    # no cols/rows
d.node("a", "Source", ["desc"])         # node() = box() without (col,row)
d.node("b", "Sink")
d.edge("a", "b")
d.save(...)                             # save() runs autoplace() automatically
```
Autoplace ranks boxes left→right along the dataflow (sources left, sinks right),
orders rows by median-barycenter to reduce crossings, and centers columns for
balance. To hand-tune, call `d.autoplace()` explicitly, then override any box:
`d.boxes["hub"].col = 1; d.boxes["hub"].row = 0`, then `save()` (it will not
re-run autoplace once every box has a position). Mixing positioned `box()` and
positionless `node()` is not supported — autoplace reassigns every box; go all
manual or all auto (+ post-tune).

## Global signals: `rail()`
```python
d.rail("clkgen", ["core0", "core1", "l2", "uart"], label="clk/rst", kind="clock")
```
One **tap** per block instead of one wire per block: a short stub on each target, the
source named once. Clock and reset across five levels of hierarchy measured in the
*hundreds* of crossings drawn as wires, and no router can help — those wires genuinely do
go everywhere. A spec does not draw them either. `O(1)` ink per block, nothing to cross,
and the relationship stays in the source (`src` is carried on every tap, so
`verify_diagram.py` can still account for it). Taps take no part in ranking or placement.

The graph bridge does this automatically: a clock-kind fan of 4 or more from one source
becomes a rail, and it prints which ones it grouped. `--no-rails` draws them as wires.

## Feedback and cycles
Cycles are broken *before* ranking (depth-first back-edge detection), so a loop cannot
reverse the flow — a fetch/decode/exec/wb pipeline with a `redirect` used to come out
with `exec` at column 0, left of `fetch`, because the longest path ran round the loop.
The back edges are then drawn as what they are, obeying rule 11: out of the right edge,
round, and back in on the left. `d._back_edges` holds them after `autoplace()`.

## Depth
Containment depth is close to free: a tree draws with **no crossings at any depth**,
provided each parent's children stay together — which is why the ordering sweeps run one
side at a time (forward by parents, backward by children) rather than averaging both, and
why wrapping a wide rank breaks on **family boundaries** instead of every `cap` boxes. A
parent whose children straddle two columns has to send wires into both, and those wires
cross the other column's.

What costs crossings is *cross-level dataflow* mixed into the same figure: a refill path
from a leaf back to a shared cache, interrupts from the periphery to the cores. Measured
on one 48-box figure: 0 crossings with containment alone, 12 with 4 such wires, 105 with
10. If a figure needs both, use `rail()` for anything global, split the relations into
separate figures, or accept the crossings — the lint counts them for you.

## Placement intent
The grid is a mechanism, not the look. Place related boxes in adjacent cells and
use spans so connection lengths stay short and the result doesn't read as a rigid
grid. Autoplace gives a bounded heuristic seed; there is intentionally **no
wire-length-minimizing solver** (that is ASIC place-and-route) — final arrangement
is a design choice. See `references/aesthetics.md` for the principle→mechanism map.

## Kinds
- `block` — normal sub-block (light blue).
- `emphasis` — highlighted block (solid blue, white text).
- `ip` — 3rd-party / black-box IP (grey, dashed). Document boundary, not internals.
- `note` — legend/notes box (via `d.note(...)`).

## Examples
`python3 scripts/blockdiagram.py --selftest` renders the reference diagrams and checks
them; read those for worked examples of grid placement, spans, ports and bus labels.

See `references/conventions.md` for the full rule rationale.

## Governance
`OWNERS` + `CHANGELOG.md` in this dir; bump `version:` on behavior change. Run
`python3 scripts/blockdiagram.py --selftest` (must print ALL PASS) before shipping
any engine change.

## From a knowledge graph

`scripts/graph_to_blockdiagram.py` draws straight from a
[graphify](https://github.com/anovickis/graphify-rtl) graph, so an RTL hierarchy becomes
a diagram without retyping it:

```sh
graphify update <design_rtl>
python3 scripts/graph_to_blockdiagram.py --root <TopModule> --depth 1 -o top.svg
python3 scripts/graph_to_blockdiagram.py --modules CacheCtrl Xbar CpuTile -o cache.svg
python3 scripts/graph_to_blockdiagram.py --root CacheCtrl --depth 2 --emit-dsl > diagram.py
python3 scripts/graph_to_blockdiagram.py --list          # module names in the graph
python3 scripts/graph_to_blockdiagram.py --root Top --ports -o top.svg
```

`--ports` puts each block's interface in its box (`524 in/198 out`, `widest 128b`),
labels each arrow with what actually crosses it (`x16 128b`), and scales line weight to
match — so a 512-bit path reads as a fat bus and a few control wires read as a signal.

The width on an arrow is the real connection: the instantiation names the child's ports
and the child declares their widths. Where a connection is positional, or its ports are
parameterised (`[WIDTH-1:0]`), the width is absent rather than guessed and the weight
falls back to the block's own widest port — a statement about the block, not the wire.
An invented bit count in a diagram gets believed, which is why none is invented.

Repeat instantiations collapse to one arrow with a count (`x16` for sixteen banks), and
modules the corpus never defines are drawn as IP — so the boundary of what was extracted
shows in the picture rather than being implied. Autoplace is a bounded heuristic seed,
not a router: for a figure going into a document, `--emit-dsl` and hand-tune.

The engine is found via the installed package, then `$BLOCKDIAGRAM_SKILL`, then a
sibling `scripts/`, so the bridge works wherever the skill is installed.
