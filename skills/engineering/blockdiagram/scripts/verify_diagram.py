#!/usr/bin/env python3
"""
verify_diagram.py -- read the finished picture back, and check it says what it should.

THE IDEA. Lint checks geometry: do boxes overlap, does a wire pass under one. It cannot
tell you whether the diagram still MEANS what the data meant. So read the SVG the way a
person does -- find the blocks, follow each wire from where it starts to the arrowhead --
recover the connections, and compare them with the ones the diagram was built from.

If a connection cannot be recovered from the picture, the picture is wrong, however
clean it lints. That catches the failures geometry cannot:

  * a wire whose endpoint cannot be attributed to any block (it ends in space)
  * an arrow that lands on a different block than intended
  * an edge that silently vanished
  * a connection the drawing invents

Usage:
    verify_diagram.py diagram.svg --expect edges.json     # [["A","B"], ["B","C"]]
    verify_diagram.py diagram.svg                         # just report what it recovers
"""
import argparse
import json
import re
import sys

TOL = 6.0                      # how near a wire end must be to a box edge to count


def _boxes(svg):
    """Blocks: a rounded rect plus the bold label drawn inside it."""
    out = []
    for m in re.finditer(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" '
                         r'height="([\d.]+)"[^>]*rx="5"', svg):
        x, y, w, h = (float(m.group(i)) for i in range(1, 5))
        out.append({"x": x, "y": y, "w": w, "h": h, "label": None})
    for m in re.finditer(r'<text x="([\d.]+)" y="([\d.]+)"[^>]*font-weight="bold"[^>]*>'
                         r'([^<]*)</text>', svg):
        tx, ty, txt = float(m.group(1)), float(m.group(2)), m.group(3)
        for b in out:
            if b["x"] - 2 <= tx <= b["x"] + b["w"] + 2 and b["y"] - 2 <= ty <= b["y"] + b["h"] + 2:
                if b["label"] is None:
                    b["label"] = txt.strip()
                break
    return [b for b in out if b["label"]]


def _on_box(pt, b):
    x, y = pt
    near_v = abs(x - b["x"]) <= TOL or abs(x - (b["x"] + b["w"])) <= TOL
    near_h = abs(y - b["y"]) <= TOL or abs(y - (b["y"] + b["h"])) <= TOL
    inside_y = b["y"] - TOL <= y <= b["y"] + b["h"] + TOL
    inside_x = b["x"] - TOL <= x <= b["x"] + b["w"] + TOL
    return (near_v and inside_y) or (near_h and inside_x)


def _attribute(pt, boxes):
    hits = [b["label"] for b in boxes if _on_box(pt, b)]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, "ends in space"
    return None, f"ambiguous between {sorted(hits)}"


def _labels(svg):
    """Every non-bold text with its position: the wire labels."""
    out = []
    for m in re.finditer(r'<text x="([\d.-]+)" y="([\d.-]+)"(?![^>]*font-weight="bold")'
                         r'[^>]*>([^<]*)</text>', svg):
        out.append((float(m.group(1)), float(m.group(2)), m.group(3).strip()))
    return out


def _dist_to_wire(pt, pts):
    """Shortest distance from a label to any point on the wire.

    Do NOT assume where along the wire the renderer chose to put the label. It slides
    the text along a run and may drop it below the line to keep clear of a box, so an
    anchor fixed at "centre of the longest run" stopped matching and this checker
    reported names as LOST that were plainly drawn in the SVG. Measuring to the wire
    itself is independent of that policy -- the checker tests the drawing, not the
    drawing's current habits.
    """
    px, py = pt
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if abs(x2 - x1) < 1e-9 and abs(y2 - y1) < 1e-9:
            continue
        t = ((px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)) / ((x2 - x1) ** 2 + (y2 - y1) ** 2)
        t = max(0.0, min(1.0, t))
        best = min(best, abs(px - (x1 + t * (x2 - x1))) + abs(py - (y1 + t * (y2 - y1))))
    return best


def _match_labels(wires, labels):
    """Assign labels to wires globally, nearest pair first.

    Matching greedily per wire let an early wire take a label that sat closer to a later
    one, so the later wire came back unlabelled and the check reported a fault in the
    DIAGRAM that was really a fault in this checker -- which is worth more than it cost,
    because a verifier that cries wolf gets ignored exactly when it is right.
    """
    pairs = []
    for wi, pts in enumerate(wires):
        if len(pts) < 2:
            continue
        for li, (lx, ly, txt) in enumerate(labels):
            if not txt:
                continue
            d = _dist_to_wire((lx, ly), pts)
            if d < 40:
                pairs.append((d, wi, li))
    pairs.sort()
    out, tw, tl = {}, set(), set()
    for d, wi, li in pairs:
        if wi in tw or li in tl:
            continue
        tw.add(wi); tl.add(li)
        out[wi] = labels[li][2]
    return out


def _parse_label(txt):
    """`16x name [128]` -> (name, bits). Either part may be absent.

    Widths are written in Verilog notation, so that is what is parsed. The older `128b`
    form is still read: diagrams already drawn should not stop verifying because the
    notation improved.
    """
    if not txt:
        return None, None
    t = re.sub(r"^\d+x\s+", "", txt)
    m = re.search(r"\[(\d+)\]$", t) or re.search(r"(\d+)b$", t)
    bits = int(m.group(1)) if m else None
    name = t[:m.start()].strip() if m else t.strip()
    return (name or None), bits


def recover(svg):
    """Connections as a reader would follow them: tail -> head, with name and size."""
    boxes = _boxes(svg)
    labels = _labels(svg)
    wires = [[tuple(map(float, p.split(","))) for p in m.group(1).split()]
             for m in re.finditer(r'<polyline points="([^"]+)"', svg)]
    matched = _match_labels(wires, labels)
    edges, problems = [], []
    for wi, pts in enumerate(wires):
        if len(pts) < 2:
            continue
        src, e1 = _attribute(pts[0], boxes)
        dst, e2 = _attribute(pts[-1], boxes)
        name, bits = _parse_label(matched.get(wi))
        if src and dst:
            edges.append((src, dst, name, bits))
        else:
            problems.append(f"wire {pts[0]}->{pts[-1]}: "
                            f"start {e1 or 'ok'}, end {e2 or 'ok'}")
    return [b["label"] for b in boxes], edges, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("svg")
    ap.add_argument("--expect", help="JSON list of [source, target] label pairs")
    a = ap.parse_args()

    svg = open(a.svg).read()
    blocks, edges, problems = recover(svg)
    print(f"  recovered {len(blocks)} blocks and {len(edges)} connections from the picture")
    for p in problems:
        print(f"  UNREADABLE  {p}")

    if not a.expect:
        for s, d, n, b in edges:
            print(f"     {s} -> {d}   {n or '(unnamed)'} {str(b)+'b' if b else '(no size)'}")
        return 1 if problems else 0

    want = {tuple(r) for r in json.load(open(a.expect))}
    got = set(edges)
    pairs_want = {(s, d) for s, d, *_ in want}
    pairs_got = {(s, d) for s, d, *_ in got}
    for s, d in sorted(pairs_want - pairs_got):
        print(f"  MISSING     {s} -> {d} is in the data but not readable from the diagram")
    for s, d in sorted(pairs_got - pairs_want):
        print(f"  INVENTED    {s} -> {d} is in the diagram but not in the data")

    # A connection can be present and still not be readable: no name, no size, or the
    # wrong one. That is a defect in the diagram, not a quibble -- an unnamed wire
    # cannot be checked against the RTL by anyone looking at the picture.
    wl = {(s, d): (n, b) for s, d, n, b in want}
    bad = 0
    for s, d, n, b in sorted(got):
        if (s, d) not in wl:
            continue
        en, eb = wl[(s, d)]
        if en and n != en:
            print(f"  NAME LOST   {s} -> {d}: data says {en!r}, diagram says {n!r}")
            bad += 1
        elif eb and b != eb:
            print(f"  SIZE LOST   {s} -> {d}: data says {eb}b, diagram says {b}b")
            bad += 1
    ok = not (problems or (pairs_want - pairs_got) or (pairs_got - pairs_want) or bad)
    print(f"  {'ROUND TRIP OK' if ok else 'ROUND TRIP FAILED'}: "
          f"{len(pairs_want & pairs_got)}/{len(pairs_want)} connections, "
          f"{len(pairs_want & pairs_got) - bad}/{len(pairs_want)} with name and size intact")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
