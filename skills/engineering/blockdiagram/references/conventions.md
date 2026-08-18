# Block-diagram conventions (rationale)

These are the rules the engine enforces and the reasons behind them. They come
from review feedback on real spec diagrams.

## Geometry / routing
- **Arrowheads small & proportional.** A common defect is oversized arrowheads
  that dominate the line. Engine uses marker size 8 with a slim triangle.
- **Anchor arrows at computed box edges**, never an eyeballed midpoint. Eyeballing
  caused a real bug where an arrow ended at a box's right edge but only "looked
  right" because the box was drawn over it.
- **Crossings are counted, and mostly avoidable.** `lint()` intersects the routed
  wires and WARNs with the number, so "looks about right" is not the test. Most
  crossings in generated diagrams were never a placement problem at all: they came
  from handing out corridor lanes in edge-declaration order, which makes a fan-out
  cross itself O(n²) times. Lanes are now ordered by travel distance. What is left is
  a design choice — swap two boxes in a column, or split the fan across sides.
- **A wire against the flow goes around, not back through.** A feedback path routed
  back through the corridors it came down has to cross the forward wires it answers.
  Back edges take a reserved channel above or below the boxes; ask for it explicitly
  with matching `src_side`/`dst_side`.
- **Crossings are fine; stacked parallel lines are not.** Two parallel segments
  drawn on top of each other read as one line and hide a connection. The engine
  offsets parallel runs sharing a corridor into separate lanes; `lint()` FAILs on
  stacked (overlapping, collinear) segments.
- **Align straight when boxes overlap on an axis.** Neighbor boxes that share an
  x- or y-range get a straight connector, not a jog.
- **Orthogonal by default, but diagonal is allowed when it reads better.**
  Right-angle routing is the default; use `edge(..., shape="straight")` for a
  direct (possibly non-90°) line where that is clearer. Diagonal edges are an
  explicit author choice and are exempt from the wire-through-box lint, so check
  them by eye. Orthogonal wires that cross a third box still FAIL.
- **Wires must not pass through a non-endpoint box.** Orthogonal mid-segments run
  in the column/row GAP corridors. `lint()` FAILs if a wire crosses an unrelated
  box (set `src_side`/`dst_side`, add a port, or restructure — this is not an
  autorouter).

## Placement
- **Align, but don't always look grid-placed.** The grid is the mechanism, not
  the goal: boxes should line up where it aids reading, while the overall
  arrangement should keep connection (wire) lengths short — related blocks near
  each other, minimal long runs. The engine gives you the grid plus spans;
  achieving short wires is currently a *manual* design choice (place related
  boxes in adjacent cells, use spans), NOT an automatic placer. Deliberately not
  building a wire-length-minimizing solver — that is the ASIC place-and-route
  rabbit hole. Revisit only if diagrams get large enough to need it.
- **Space boxes enough that an edge label clears both boxes.** A bus label sits
  over the connector midpoint; if the gap is too small the label collides with a
  box. The engine widens the horizontal gap to fit the widest edge label
  (`gap_x >= max_label_width + 16`). Keep labels concise.

## Labels
- **Label the bus width and bandwidth** on connection lines (e.g. "TL 64b",
  "AXI4 512b ×4ch"). Bandwidth that depends on a runtime clock is written as
  width × clock with the clock noted.
- **Line weight encodes cardinality — three tiers only.** `edge(..., weight=...)`:
  `"signal"` = one wire (1 px), `"bus"` = a little wider (a bundle), `"fat"` = a
  fat bus (much wider). Keep to these three (don't invent a continuum). Arrowheads
  grow only modestly with weight (`markerUnits=userSpaceOnUse`) so thick lines
  don't get clumsy heads. Use it for the datapath hierarchy at a glance; still
  label the actual bit-width in text — thickness shows rank, the label gives the
  number.
- **Block label must fit inside the box.** Boxes auto-size to text; `lint()` WARNs
  if a line still looks too wide.
- **Small description line(s) under the label** — the house style.
- **Port labels are optional** — add only when they earn their place; don't label
  ports reflexively.

## Text / fonts
- **No missing-glyph boxes.** Stick to Basic Latin plus a curated safe set
  (`× · – — → ← ↔ ↑ ↓ ≈ ≤ ≥ ° ± § … ✓ ✗`), or convert text to paths / embed a
  font. `lint()` WARNs on other non-ASCII glyphs. Verify in the rendered PNG.

## Palette
- `#1F4E79` — emphasis blocks and all strokes/headings.
- `#cfe0f0` — normal blocks.
- grey `#ececec` + dashed `#888` — 3rd-party / black-box IP (Cadence DDR, PCIe,
  Ethernet). Document the boundary, not the internals.
- `#f7f9fc` — notes / legend boxes.

## Process
- Always render to PNG and visually inspect before committing. These diagrams
  come out "almost ok" and need a human look every time, even when lint is clean.
  Lint checks geometry only — it does **not** catch a clipped/overflowing label, a
  title running off the canvas, a legend sitting on a curve, or a stray escape in a
  label (e.g. a literal `\_`). Read the PNG and confirm every label fits.

## Common lint FAILs and how placement fixes them (observed)
Three FAILs cause almost every failed build; the FAIL text now names the boxes/edges
and the fix. Design placement to avoid them (see SKILL.md "Passing lint the first time"):
- **Wire crosses a non-endpoint box** — a same-row/column edge runs through a box between
  its endpoints. Put the endpoints in adjacent cells; or leave via a free side with
  `src_side=`/`dst_side=`; or move the middle box. Place a many-edge "hub" box centrally,
  not at one end. Do **not** paper over it with `shape="straight"` — straight edges are
  exempt from the crossing check and will silently pass through the box.
- **Stacked parallel segments** — two edges share one corridor. Draw a single representative
  wire for a relationship (a fan of identical lanes reads as one bus); or give siblings
  distinct sides; or space the boxes.
- **Box overlap** — a `colspan`/`rowspan` collision; no two boxes may claim a cell.
