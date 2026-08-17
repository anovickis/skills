---
name: blockdiagram
version: 0.2.0
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
