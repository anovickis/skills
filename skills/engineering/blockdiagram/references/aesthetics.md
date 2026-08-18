# What makes a block diagram pleasing — and how the engine does it

Auto-generated diagrams look bad for *nameable* reasons. Each aesthetic below is
translated into a concrete mechanism so it can be enforced, not eyeballed.

| Aesthetic principle (the "what") | Mechanism (the "how") | Status |
|----------------------------------|-----------------------|--------|
| **Clear reading direction** — eye flows one way (dataflow L→R) | Layered ranks: `col = longest-path distance from sources`; sinks end up right | autoplace |
| **Few edge crossings** — crossings read as noise | Median-barycenter row ordering, then two *measured* passes: alternative rankings and adjacent-swap shuffling, each kept only if the counted crossings drop | autoplace |
| **A bundle reads as a comb** — a fan-out must not cross itself | Lanes in a corridor ordered by travel distance (farthest destination innermost), packed per corridor position, spacing stepped down to fit | route |
| **Feedback looks like feedback** — a return path shouldn't weave through the forward path | Cycles broken before ranking; back edges taken around the outside in a reserved top/bottom channel, banks split when spans interleave | autoplace + route + layout |
| **Straight primary datapath** — minimise bends | Single edge between aligned boxes is drawn straight (anchor = overlap-centre); ordering pulls connected boxes toward the same row | route + autoplace |
| **Aligned structure** — shared edges/centrelines, uniform sizing | Grid: column width = widest box in column, row height = tallest in row; everything snaps to it | layout |
| **Balance / symmetry** — no lopsided columns | Each column is vertically centred (`offset = (nrow−len)//2`) | autoplace |
| **Fan-out, not stacking** — many wires off one block spread out | Edge-ends sharing a (box, side) are fanned to distinct points, ordered by the far endpoint (arriving ends by reach), and the side is *sized* to keep them `FAN_MIN` apart by claiming empty cells | route + layout |
| **Compactness / good aspect ratio** — not a long thin chain or a sprawl | Aspect-ratio lint WARNs when `W/H` is outside ~0.31–3.2 (suggests wrap / change flow) | lint |
| **Consistent spacing** — even gutters | Uniform `gap_x`/`gap_y`; auto-widened only to fit edge labels | layout |
| **No collisions** — boxes, wires, labels don't overlap | Lint FAILs: box overlap, wire-through-box, stacked parallel segments. Lint WARNs: measured wire crossings, label-on-label, label-on-box | lint |
| **Legible text** — fits, no tofu | Measured metrics size boxes; glyph-coverage check vs the actual font | layout + lint |
| **Visual hierarchy** — important vs background | `kind`: emphasis (solid blue), block, ip (grey dashed black box), note | render |
| **Labelled connections** — buses show width / b/w | `edge(label=...)`; placed on a straight run of its own wire (preferring the arriving run), anchored to the box edge, haloed, collision-checked; the layout spreads if labels don't fit | render + layout |
| **Cardinality at a glance** — 1 wire vs bus vs fat bus | `edge(weight="signal"/"bus"/"fat")` → 1 px / wider / much wider line; arrowhead scales only modestly | render |

## What the engine deliberately does NOT do (avoid the P&R rabbit hole)
- No continuous force-directed/annealing placement. Layered + barycenter is a
  bounded heuristic seed; **hand-tune by setting any box's `col`/`row`**.
- No wrapping of long chains (would break the flow direction). If the
  aspect-ratio WARN fires, restructure manually or pick a different flow.
- No global wire-router with obstacle avoidance. Mid-segments use gap corridors; a
  wire that skips columns gets a cleared row and a back edge gets an outside channel,
  but there is no channel router. The lint FAILs if a wire still crosses a third box,
  and you fix it by hand (`src_side`/`dst_side`, a port, or a different cell).
- No re-placement of a hand-placed diagram. Every placement repair above is part of
  autoplace; if you set `col`/`row`, those cells are kept and the lint reports.

## Practical rules of thumb (encoded above, restated for authors)
1. Let dataflow set left→right; keep feedback edges few and short.
2. Put related blocks adjacent — short wires beat clever routing.
3. One straight datapath spine; branch off it, don't weave through it.
4. Spread a block's many connections across its side; never stack two wires.
5. Keep the whole figure near a 4:3–16:9 box; if it goes thin, re-flow.
6. Uniform boxes, uniform gaps, centred columns — consistency reads as "designed".
