# What makes a block diagram pleasing — and how the engine does it

Auto-generated diagrams look bad for *nameable* reasons. Each aesthetic below is
translated into a concrete mechanism so it can be enforced, not eyeballed.

| Aesthetic principle (the "what") | Mechanism (the "how") | Status |
|----------------------------------|-----------------------|--------|
| **Clear reading direction** — eye flows one way (dataflow L→R) | Layered ranks: `col = longest-path distance from sources`; sinks end up right | autoplace |
| **Few edge crossings** — crossings read as noise | Median-barycenter row ordering, several down/up sweeps per rank | autoplace |
| **Straight primary datapath** — minimise bends | Single edge between aligned boxes is drawn straight (anchor = overlap-centre); ordering pulls connected boxes toward the same row | route + autoplace |
| **Aligned structure** — shared edges/centrelines, uniform sizing | Grid: column width = widest box in column, row height = tallest in row; everything snaps to it | layout |
| **Balance / symmetry** — no lopsided columns | Each column is vertically centred (`offset = (nrow−len)//2`) | autoplace |
| **Fan-out, not stacking** — many wires off one block spread out | Edge-ends sharing a (box, side) are fanned to distinct points, ordered by the far endpoint so they don't cross at the box | route |
| **Compactness / good aspect ratio** — not a long thin chain or a sprawl | Aspect-ratio lint WARNs when `W/H` is outside ~0.31–3.2 (suggests wrap / change flow) | lint |
| **Consistent spacing** — even gutters | Uniform `gap_x`/`gap_y`; auto-widened only to fit edge labels | layout |
| **No collisions** — boxes, wires, labels don't overlap | Lint FAILs: box overlap, wire-through-box, stacked parallel segments | lint |
| **Legible text** — fits, no tofu | Measured metrics size boxes; glyph-coverage check vs the actual font | layout + lint |
| **Visual hierarchy** — important vs background | `kind`: emphasis (solid blue), block, ip (grey dashed black box), note | render |
| **Labelled connections** — buses show width / b/w | `edge(label=...)`; label centred over the wire with room reserved | render + layout |
| **Cardinality at a glance** — 1 wire vs bus vs fat bus | `edge(weight="signal"/"bus"/"fat")` → 1 px / wider / much wider line; arrowhead scales only modestly | render |

## What the engine deliberately does NOT do (avoid the P&R rabbit hole)
- No continuous force-directed/annealing placement. Layered + barycenter is a
  bounded heuristic seed; **hand-tune by setting any box's `col`/`row`**.
- No wrapping of long chains (would break the flow direction). If the
  aspect-ratio WARN fires, restructure manually or pick a different flow.
- No global wire-router with obstacle avoidance. Mid-segments use gap corridors;
  the lint FAILs if a wire still crosses a third box, and you fix it by hand
  (`src_side`/`dst_side`, a port, or a different cell).

## Practical rules of thumb (encoded above, restated for authors)
1. Let dataflow set left→right; keep feedback edges few and short.
2. Put related blocks adjacent — short wires beat clever routing.
3. One straight datapath spine; branch off it, don't weave through it.
4. Spread a block's many connections across its side; never stack two wires.
5. Keep the whole figure near a 4:3–16:9 box; if it goes thin, re-flow.
6. Uniform boxes, uniform gaps, centred columns — consistency reads as "designed".
