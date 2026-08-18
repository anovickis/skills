#!/usr/bin/env python3
"""
graph_to_blockdiagram.py -- pick modules out of a graphify graph, draw a block diagram.

THE GAP THIS CLOSES.  graphify answers "what connects to what" but only draws node-link
graph views (graph.html, tree, --svg, --graphml).  The `blockdiagram` skill draws proper
hardware block diagrams -- boxes, orthogonal routing, bus labels, grey dashed 3rd-party IP
-- but wants you to author the DSL by hand.  Nothing joined the two, so "show me the data
flow around this block" meant reading the graph and retyping it.  This does that step.

    graphify update <design_rtl>          # build/refresh the graph
    graph_to_blockdiagram.py --root <TopModule> --depth 1 -o top.svg

WHAT IT SELECTS.  A root module plus everything it instantiates to --depth, or an explicit
--modules list.  Edges between selected modules are kept; everything else is dropped.
Modules the corpus never defines (vendor IP, technology cells) come through as `external`
and are drawn with the skill's IP style -- grey dashed -- so the boundary of your corpus is
visible in the picture rather than implied.

WHAT IT DOES NOT DO.  It does not choose the layout: `save()` runs the skill's autoplace,
which ranks boxes left-to-right along the dataflow.  That is a bounded heuristic seed, and
the skill is explicit that final arrangement is a design choice -- so for anything going
into a document, emit the DSL with --emit-dsl and hand-tune from there.

A NOTE ON EDGE LABELS.  An `instantiates` edge carries no bus width or protocol, because
the RTL parse does not know one.  Diagrams therefore come out unlabelled unless you pass
--label-edges.  Do not read an unlabelled arrow as "no bus" -- it means "not extracted".

Usage:
    graph_to_blockdiagram.py --root <TopModule> --depth 1 -o out.svg
    graph_to_blockdiagram.py --modules CacheCtrl Xbar CpuTile -o cache.svg
    graph_to_blockdiagram.py --root CacheCtrl --depth 2 --emit-dsl > diagram.py
    graph_to_blockdiagram.py --list                    # what modules exist
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Where the blockdiagram engine lives.  Resolved rather than hardcoded so this travels
# with the skill instead of being welded to one repository's layout: $BLOCKDIAGRAM_SKILL
# wins, then a sibling scripts/ (skill-local install), then this repo's .claude/skills.
def _skill_dir():
    env = os.environ.get("BLOCKDIAGRAM_SKILL")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "scripts"),
                 os.path.join(os.path.dirname(here), "scripts"),
                 os.path.join(REPO, ".claude", "skills", "blockdiagram", "scripts")):
        if os.path.exists(os.path.join(cand, "blockdiagram.py")):
            return cand
    return os.path.join(REPO, ".claude", "skills", "blockdiagram", "scripts")


SKILL = _skill_dir()

DEFAULT_GRAPH = "graphify-out/graph.json"
# A block diagram stops being readable well before this; the skill's aesthetics notes say
# the same. Kept low on purpose -- a wide fan-out lays out as a long thin column and the
# lint will say so. Truncation is always reported, never silent.
DEFAULT_MAX = 14


def load(path):
    with open(path) as fh:
        g = json.load(fh)
    nodes = {n["id"]: n for n in g.get("nodes", [])}
    edges = g.get("links", g.get("edges", []))
    return g, nodes, edges


def modules(nodes):
    """Module-definition nodes, keyed by module name.

    `verilog_module` is set by the extractor on a module declaration. Graphs built before
    cross-file instantiation resolution existed do not carry it; fall back to the label so
    this still does something useful on an old graph (and warn -- see check_hierarchy).
    """
    out = {}
    for n in nodes.values():
        name = n.get("verilog_module")
        if name:
            out.setdefault(name, n["id"])
    if not out:
        for n in nodes.values():
            lbl = n.get("label", "")
            if lbl and not lbl.endswith((".v", ".sv", ".svh")):
                out.setdefault(lbl, n["id"])
    return out


def check_hierarchy(nodes, edges):
    """Warn when the graph predates cross-file instantiation resolution.

    Without it every `instantiates` edge points at a file-local stub, so a diagram comes
    out as disconnected boxes and the reason is not obvious from the picture.
    """
    inst = [e for e in edges if e.get("relation") == "instantiates"]
    if not inst:
        return "no `instantiates` edges in this graph - nothing to draw a hierarchy from"
    cross = sum(1 for e in inst
                if e["source"] in nodes and e["target"] in nodes
                and nodes[e["source"]].get("source_file") != nodes[e["target"]].get("source_file"))
    if cross == 0:
        return ("every instantiates edge is intra-file, so this graph has no cross-file "
                "hierarchy - it predates instantiation resolution. Re-run `graphify update`")
    return None


def select(nodes, edges, roots, depth, max_nodes):
    """Root ids plus what they instantiate, breadth-first to `depth`."""
    kids = {}
    for e in edges:
        if e.get("relation") == "instantiates":
            kids.setdefault(e["source"], []).append(e["target"])
    keep, frontier = list(roots), list(roots)
    seen = set(roots)
    truncated = False
    for _ in range(max(depth, 0)):
        nxt = []
        for nid in frontier:
            for k in kids.get(nid, []):
                if k in seen or k not in nodes:
                    continue
                if len(keep) >= max_nodes:
                    truncated = True
                    break
                seen.add(k)
                keep.append(k)
                nxt.append(k)
            if truncated:
                break
        frontier = nxt
        if truncated or not frontier:
            break
    sel = set(keep)
    # Collapse repeat instantiations into one arrow carrying the count.  RTL instantiates
    # the same module many times over (banks, lanes, tiles), which as separate arrows is
    # both unreadable and a lint FAIL for stacked parallel segments -- and "x16" says more
    # than sixteen identical lines ever could.
    counts = {}
    widths = {}
    names = {}
    for e in edges:
        if e.get("relation") != "instantiates":
            continue
        k = (e["source"], e["target"])
        if e["source"] in sel and e["target"] in sel:
            counts[k] = counts.get(k, 0) + 1
            w = e.get("verilog_conn_bits_max")
            if w and w >= widths.get(k, 0):
                widths[k] = w
                # the name that goes with the width being shown
                names[k] = e.get("verilog_conn_widest_port") or names.get(k)
    kept_edges = [{"source": s, "target": t, "count": c, "bits": widths.get((s, t)),
                   "name": names.get((s, t))}
                  for (s, t), c in counts.items()]
    return keep, kept_edges, truncated


def _port_desc(node):
    """What to write inside a box, beyond its name.

    NOT the port counts. "24 in/20 out, widest 128b" answers a question the wires
    already answer, and answers it in a way that invites the wrong arithmetic -- an
    arrow reading `x4 128b` next to a box reading `widest 128b` looks like it might
    mean 512b, and does not. Anything derivable from the edges belongs on the edges.

    What is worth saying inside a box is what the wires CANNOT say: that a block is
    outside the corpus, or what a named instance is an instance of.
    """
    if node.get("external"):
        return ["not defined in corpus"]
    inst = node.get("chisel_instance_of") or node.get("firrtl_instance_of")
    if inst and node.get("label", "").startswith(f"{node.get('chisel_instance_name', '')}:"):
        return None                      # the label already reads "name: Type"
    return None


def _weight_for(edge, node):
    """Line weight from the widest thing that actually crosses this connection.

    `verilog_conn_bits_max` is real: the instantiation names the child's ports and the
    child declares their widths, so this is the connection, not a proxy. Where the
    connection is positional or its ports are parameterised, fall back to the block's
    widest port -- a statement about the block rather than the wire, which is why it is
    only the fallback.
    """
    b = (edge or {}).get("bits")
    if not b:
        b = ((node or {}).get("verilog_port_summary") or {}).get("widest_bits") or 0
    return "fat" if b >= 256 else "bus" if b >= 32 else "signal"


def _edge_label(e, label_edges, ports=False):
    """Name the wire and give its size; a bare relation says nothing.

    A width alone tells a reader how much crosses. The NAME tells them what crosses,
    and only the name lets anyone check the drawing back against the RTL -- which is
    the difference between a picture that illustrates and one that can be audited.

    `8x` is the number of instantiations, not a multiplier on the width: `8x d_data 128b`
    is eight connections each carrying a 128-bit `d_data`, not a 1024-bit bus. The count
    leads so the eye does not read it as arithmetic on the number that follows.
    """
    parts = []
    if e.get("count", 1) > 1:
        parts.append(f"{e['count']}x")
    if ports and e.get("name"):
        parts.append(str(e["name"]))
    if ports and e.get("bits"):
        parts.append(f"{e['bits']}b")
    elif ports:
        parts.append("width n/k")        # known to be unknown, not merely missing
    if label_edges:
        parts.append("instantiates")
    return " ".join(parts) or None


def _safe(bid):
    return "".join(c if c.isalnum() or c == "_" else "_" for c in bid)


def emit_dsl(title, keep, kept_edges, nodes, label_edges, ports=False):
    """The skill's DSL as text, for hand-tuning -- its recommended workflow."""
    lines = [f'import sys; sys.path.insert(0, "{SKILL}")',
             "from blockdiagram import Diagram", "",
             f'd = Diagram({title!r})', ""]
    for nid in keep:
        n = nodes[nid]
        kind = "ip" if n.get("external") else "block"
        desc = _port_desc(n)
        lines.append(f'd.node({_safe(nid)!r}, {n.get("label", nid)!r}, {desc!r}, kind={kind!r})')
    lines.append("")
    for e in kept_edges:
        w = _weight_for(e, nodes.get(e["target"])) if ports else "signal"
        lines.append(f'd.edge({_safe(e["source"])!r}, {_safe(e["target"])!r}, '
                     f'label={_edge_label(e, label_edges, ports)!r}, weight={w!r})')
    lines += ["", 'print(d.save("diagram.svg"))']
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-g", "--graph", default=DEFAULT_GRAPH, help=f"graph.json (default {DEFAULT_GRAPH})")
    ap.add_argument("--root", help="module name to start from")
    ap.add_argument("--depth", type=int, default=1, help="how many levels below the root (default 1)")
    ap.add_argument("--modules", nargs="+", help="explicit module names instead of --root")
    ap.add_argument("-o", "--out", default="diagram.svg", help="output SVG (a PNG is written beside it)")
    ap.add_argument("--emit-dsl", action="store_true", help="print the DSL instead of rendering")
    ap.add_argument("--label-edges", action="store_true", help="label arrows 'instantiates'")
    ap.add_argument("--ports", action="store_true",
                    help="show each block's interface (port counts + widest bus) and scale "
                         "line weight by the instantiated block's widest port")
    ap.add_argument("--max-nodes", type=int, default=DEFAULT_MAX, help=f"box cap (default {DEFAULT_MAX})")
    ap.add_argument("--list", action="store_true", help="list module names in the graph and exit")
    ap.add_argument("--skill-path", help="blockdiagram scripts/ dir (else $BLOCKDIAGRAM_SKILL)")
    ap.add_argument("--verify", action="store_true",
                    help="read the finished SVG back and check every connection in the "
                         "data is recoverable from the picture")
    a = ap.parse_args()

    if not os.path.exists(a.graph):
        sys.exit(f"graph not found: {a.graph}\nRun `graphify update <design_rtl>` first.")
    _, nodes, edges = load(a.graph)
    mods = modules(nodes)

    if a.list:
        for name in sorted(mods):
            print(f"  {name}")
        print(f"\n  {len(mods)} modules")
        return 0

    warn = check_hierarchy(nodes, edges)
    if warn:
        sys.stderr.write(f"warning: {warn}\n")

    if a.modules:
        missing = [m for m in a.modules if m not in mods]
        if missing:
            sys.exit(f"not in the graph: {', '.join(missing)}   (try --list)")
        roots = [mods[m] for m in a.modules]
        depth = 0 if len(a.modules) > 1 else a.depth
        title = " / ".join(a.modules[:3]) + ("..." if len(a.modules) > 3 else "")
    elif a.root:
        if a.root not in mods:
            sys.exit(f"not in the graph: {a.root}   (try --list)")
        roots = [mods[a.root]]
        depth = a.depth
        title = f"{a.root} (depth {a.depth})"
    else:
        sys.exit("give --root or --modules (or --list)")

    keep, kept_edges, truncated = select(nodes, edges, roots, depth, a.max_nodes)
    if truncated:
        sys.stderr.write(f"warning: stopped at --max-nodes={a.max_nodes}; the diagram is a "
                         f"SUBSET of what the root reaches. Narrow --depth or name modules "
                         f"explicitly rather than reading this as the whole picture.\n")
    ext = sum(1 for n in keep if nodes[n].get("external"))
    sys.stderr.write(f"{len(keep)} boxes, {len(kept_edges)} edges"
                     + (f", {ext} external (drawn as IP)" if ext else "") + "\n")

    if a.emit_dsl:
        print(emit_dsl(title, keep, kept_edges, nodes, a.label_edges, a.ports))
        return 0

    skill = a.skill_path or SKILL
    sys.path.insert(0, skill)
    try:
        from blockdiagram import Diagram
    except ImportError as exc:
        sys.exit(f"blockdiagram engine not importable from {skill}: {exc}\n"
                 f"Set --skill-path or $BLOCKDIAGRAM_SKILL to the skill's scripts/ dir.")
    d = Diagram(title)
    for nid in keep:
        n = nodes[nid]
        desc = _port_desc(n)
        d.node(_safe(nid), n.get("label", nid), desc,
               kind="ip" if n.get("external") else "block")
    for e in kept_edges:
        d.edge(_safe(e["source"]), _safe(e["target"]),
               label=_edge_label(e, a.label_edges, a.ports),
               weight=_weight_for(e, nodes.get(e["target"])) if a.ports else "signal")
    report = d.save(a.out)
    print(report)
    if a.verify:
        # Read the picture back. Lint says the geometry is sound; this says the diagram
        # still means what the data meant, which is a different and stronger question.
        import json as _json
        import subprocess as _sp
        import tempfile as _tf
        labels = {_safe(nid): nodes[nid].get("label", nid) for nid in keep}
        want = [[labels[_safe(e["source"])], labels[_safe(e["target"])],
                 e.get("name"), e.get("bits")] for e in kept_edges]
        with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            _json.dump(want, fh)
            expect = fh.name
        r = _sp.run([sys.executable, os.path.join(skill, "verify_diagram.py"),
                     a.out, "--expect", expect], capture_output=True, text=True)
        sys.stderr.write(r.stdout + r.stderr)
    if any(level == "FAIL" for level, _ in (report or [])):
        sys.stderr.write(
            "\nlint FAILED: a wide fan-out does not lay out well automatically. Autoplace is "
            "a heuristic seed, not a router. Re-run with --emit-dsl, then hand-tune "
            "(set col/row, src_side/dst_side) as the skill's README describes, or pick fewer "
            "modules with --modules.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
