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


def recover(svg):
    """Connections as a reader would follow them: tail -> head of each wire."""
    boxes = _boxes(svg)
    edges, problems = [], []
    for m in re.finditer(r'<polyline points="([^"]+)"', svg):
        pts = [tuple(map(float, p.split(","))) for p in m.group(1).split()]
        if len(pts) < 2:
            continue
        src, e1 = _attribute(pts[0], boxes)
        dst, e2 = _attribute(pts[-1], boxes)
        if src and dst:
            edges.append((src, dst))
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
        for s, d in edges:
            print(f"     {s} -> {d}")
        return 1 if problems else 0

    want = {(s, d) for s, d in json.load(open(a.expect))}
    got = set(edges)
    missing = want - got
    spurious = got - want
    for s, d in sorted(missing):
        print(f"  MISSING     {s} -> {d} is in the data but not readable from the diagram")
    for s, d in sorted(spurious):
        print(f"  INVENTED    {s} -> {d} is in the diagram but not in the data")
    ok = not (problems or missing or spurious)
    print(f"  {'ROUND TRIP OK' if ok else 'ROUND TRIP FAILED'}: "
          f"{len(want & got)}/{len(want)} connections survive the drawing")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
