---
name: blockdiagram
version: 0.6.0
description: >-
  Author technical SVG block diagrams for hardware/spec documents from a small
  Python DSL with explicit grid placement (or connectivity-driven autoplace)
  and computed orthogonal routing.
  Boxes auto-fit their text; arrows anchor at box edges and parallel runs are
  pushed into separate lanes; arrowheads stay small; bus edges carry width/
  bandwidth labels; wires are coloured by class (data/control/clock/interrupt) with a
  legend; Cadence/3rd-party IP renders as grey dashed black boxes.
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
d.edge("cpu", "hub", label="irq[3:0]", cls="interrupt")   # coloured + legend entry
d.rail("clkgen", ["cpu", "mem", "hub"], label="clk/rst", cls="clock")  # tap per block
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
- **Wire classes**: `edge(..., cls="data"|"control"|"clock"|"interrupt")` →
  `#1F4E79` / `#B8860B` / `#6A5ACD` / `#A03030`, arrowhead matched to the wire, and a
  legend drawn top-right for the classes actually used (two or more; one class needs no
  key). An unknown `cls` falls back to `data` rather than inventing a colour. Interrupt
  and clock/reset wiring is not datapath — colouring it means a reader can dismiss it
  without tracing it. Four classes is the whole vocabulary on purpose.
- **Measured text**: boxes size to real font metrics (PIL + Arial-metric font),
  with a heuristic fallback; glyph coverage checked against the actual font.

## Quality gate (always)
`save()` renders a PNG and runs `lint()`:
- `FAIL`: box overlap; orthogonal wire crossing a non-endpoint box; stacked
  (overlapping, non-crossing) parallel segments.
- `WARN`: **wire crossings** (counted geometrically, from the routed polylines —
  see below); a label overlapping another label or sitting on a box; text
  overflows a box (measured); glyph missing from the font (avoid missing-glyph
  boxes — stick to covered glyphs or convert text to paths); a legend sitting on a box
  or running into the title; thin aspect ratio.

**The crossing count is measured, not estimated.** It intersects the routed wires,
so it reports what a reader sees. (It used to count pairs of edges between the same
two columns whose row order inverts, which scored a twenty-wire fan-out carrying
eighty crossings as *zero* — a fan out of one box shares a source column, so no pair
ever "inverts". If you have an old diagram that lints clean, re-run it.)

Treat any `FAIL` as blocking. After a clean build, **still open the PNG and look**
— the lint catches geometry, not aesthetics (it will not catch a clipped/overflowing
label, a title running off the canvas, or a legend sitting on top of a curve; read the
rendered PNG and confirm every label fits and nothing collides).
Run the engine self-test with `python3 scripts/blockdiagram.py --selftest`.

## What the engine now gets right on its own
These were all defects; they are now engine behaviour, so don't hand-fix them:
- **Lane order in a bundle.** Wires turning in one corridor are ordered by how far
  they travel — farthest destination innermost — so a fan-out draws as a comb
  instead of self-crossing. Where two boxes in a column share the corridor, the one
  further from the destinations takes the outer lanes, since its wires have to pass
  the other.
- **One lane, one wire.** No two wires share a turn line, even where the stretches
  they run along don't overlap: collinear runs with a gap between them read as a
  single long wire with the other wires' turns crossing it. The corridor is sized for
  the whole bundle and the comb starts one step off the box.
- **A side is sized to its fan.** A box that must host *n* wires on one side grows
  so the ends stay `FAN_MIN` apart, claiming the empty cells beside it (rowspan)
  rather than inflating its whole grid row.
- **A column-skipping wire gets a clear row.** Autoplace moves the destination to a
  row that is empty in the columns the wire skips, adding one row if need be —
  what a Sugiyama dummy node buys.
- **Feedback wires read as feedback.** Cycles are broken before ranking (so a
  pipeline with a `redirect` still reads left→right), and a back edge is taken
  around the outside, over the top or under the bottom, with the canvas reserving
  that channel. Two wires whose spans interleave go to opposite banks.
- **Labels sit on their own wire.** Each label is placed on a straight run of the
  wire it names (preferring the run arriving at the destination), backed against a
  box edge so it stays in the corridor, with a white halo so crossing a wire does
  not cut it in half. If a label cannot be placed clear, the layout **spreads** and
  retries — and backs off again if spreading is not what was wrong.
- **Same-side routes go around.** `src_side="T", dst_side="T"` (or both `"B"`,
  `"L"`, `"R"`) runs the wire outside both boxes. This is the escape hatch the
  wire-through-box FAIL names, and it is how you route a bypass down a stack. When
  the band beside those two boxes is occupied, the route falls back to the channel
  outside *everything*.
- **A wire that cannot be threaded goes round the outside.** `shape="around"` leaves
  into the side margin, runs along the reserved top/bottom band past everything, and
  comes back in at the far side — margins and bands are the only box-free space in the
  figure. The engine applies it itself, measured and reluctantly: only to a wire
  involved in 2+ crossings, only if it removes at least 2, only if it adds no fault,
  and at most 4 wires per figure. On a 53-box merged hierarchy one such wire accounted
  for 26 of 29 crossings.

Two passes are *measured* rather than assumed: autoplace tries a couple of
alternative rankings (a hub whose children are already fed from the left is tried on
the right of them) and keeps whichever draws better, then swaps neighbouring boxes
in a column while that keeps improving the measured (faults, crossings).

**Hand-placed diagrams are never re-placed.** All of the above placement repair
applies to autoplace only: if you set `col`/`row` yourself, the engine keeps your
cells and the lint tells you what is wrong and how to fix it.

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

A fourth, rarer one: a **bypass wire down a stack** (same column, skipping boxes)
cannot be routed inside the column — take it around with `src_side="R", dst_side="R"`.

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

## Placement intent
The grid is a mechanism, not the look. Place related boxes in adjacent cells and
use spans so connection lengths stay short and the result doesn't read as a rigid
grid. Autoplace gives a bounded heuristic seed; there is intentionally **no
wire-length-minimizing solver** (that is ASIC place-and-route) — final arrangement
is a design choice. See `references/aesthetics.md` for the principle→mechanism map.

## Global signals: `rail()`, not one wire per block
```python
d.rail("clkgen", ["core0", "core1", "l2", "uart"], label="clk/rst", cls="clock")
```
Clock, reset and scan go everywhere, and drawing them as one wire per block is what
wrecks a deep diagram — eleven of them across five levels measured **hundreds** of
crossings, and no router can help, because those wires genuinely do go everywhere. A
spec does not draw them either: it draws a **tap** at each block and names the source
once. `rail()` does that — a short stub on each target, `O(1)` ink per block, nothing
to cross. The relationship stays in the diagram source (`src` is recorded on every tap),
it is simply not traced across the figure. Taps take no part in placement.

## Depth
Containment depth is free: a 5-level tree of 46 boxes draws with **0 crossings**. What
costs crossings is *cross-level dataflow* mixed into the same figure — a refill path
from a leaf back to a shared cache, interrupts from the periphery to the cores. Measured
on one 48-box figure: 0 crossings with containment alone, 12 with 4 such wires, 105 with
10, 116 with 20. If a figure needs both, the options in order of preference are: draw
`rail()` for anything global; split the relations into separate figures; or accept the
crossings, which the lint counts for you. A flat layered grid cannot express containment
and dataflow at once — there is no box-in-box nesting.

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
```

Repeat instantiations collapse to one arrow with a count (`x16` for sixteen banks), and
modules the corpus never defines are drawn as IP — so the boundary of what was extracted
shows in the picture rather than being implied. Autoplace is a bounded heuristic seed,
not a router: for a figure going into a document, `--emit-dsl` and hand-tune.

The engine is found via the installed package, then `$BLOCKDIAGRAM_SKILL`, then a
sibling `scripts/`, so the bridge works wherever the skill is installed.
