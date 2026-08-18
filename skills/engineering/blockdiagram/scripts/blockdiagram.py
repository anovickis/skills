#!/usr/bin/env python3
"""blockdiagram — a small Python DSL for spec block diagrams.

Design choices (enforced, not re-derived each time):
  * Explicit GRID placement: boxes go in (col,row) cells with optional spans;
    the engine computes x/y/w/h so things align and never silently overlap.
  * Boxes auto-fit: sized to MEASURED text (PIL + an Arial-metric font), with a
    heuristic fallback if PIL/font are unavailable.
  * Edge-anchored ORTHOGONAL routing: arrows attach at computed box edges, run
    at right angles through column/row GAP corridors, never under a box and never
    along another wire — every placed wire becomes an obstacle to the ones after
    it, and corridors are divided into tracks so several wires can share a gutter
    side by side rather than on top of each other. Among the legal paths it takes
    the one with fewest crossings, then the shortest. For awkward cases set
    src_side/dst_side or add a waypoint; the lint reports what it could not solve.
  * Small, proportional arrowheads.
  * Optional ports: declare named connection points on a side and attach edges
    to them ("box:port"); ports are drawn only when declared.
  * House palette: #1F4E79 emphasis, #cfe0f0 blocks, grey dashed = IP black box.

Quality gate: save() writes the SVG, renders a PNG with inkscape, and runs
geometric lint (box overlap, text overflow, wire-through-box, stacked parallel
segments, arrowhead size, glyphs missing from the font). Always eyeball the PNG.

Self-test:  python3 blockdiagram.py --selftest
"""
import math
import os, subprocess, sys

# ---- house palette / sizes ----------------------------------------------
BLUE = "#1F4E79"
BLOCK_FILL = "#cfe0f0"
EMPH_FILL = "#1F4E79"
IP_FILL = "#ececec"
NOTE_FILL = "#f7f9fc"
GREY = "#555"
FS_LABEL = 13
FS_DESC = 11
FS_SMALL = 10
FS_PORT = 9
PAD = 12
ARROW = 8
LABEL_BOTH_ENDS = 300   # a wire longer than this is named at both ends
WIRE_PITCH = 22     # vertical room reserved per wire attached to a box
LANE = 8           # spacing between parallel runs in a corridor
# line weight encodes cardinality (THREE tiers only):
#   signal = one wire (1 px) · bus = a little wider · fat = a fat bus (much wider)
WEIGHTS = {"signal": 1.0, "bus": 3.0, "fat": 6.0, "wide": 6.0}  # "wide" = alias of fat


# ---- text metrics (measured, with fallback) ------------------------------
class _Metrics:
    _inst = None

    def __init__(self):
        self.ok = False
        try:
            from PIL import ImageFont  # noqa
            self.reg = self._fc("Arial")
            self.bold = self._fc("Arial:bold")
            self._ImageFont = ImageFont
            self._cache = {}
            self._cmap = self._load_cmap(self.reg)
            self.ok = bool(self.reg)
        except Exception:
            self.ok = False

    @staticmethod
    def _fc(pattern):
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", pattern],
                                 capture_output=True, text=True).stdout.strip()
            return out or None
        except Exception:
            return None

    @staticmethod
    def _load_cmap(path):
        try:
            from fontTools.ttLib import TTFont
            return set(TTFont(path).getBestCmap().keys())
        except Exception:
            return None

    def _font(self, size, bold):
        key = (size, bold)
        if key not in self._cache:
            path = self.bold if (bold and self.bold) else self.reg
            self._cache[key] = self._ImageFont.truetype(path, int(round(size)))
        return self._cache[key]

    def width(self, text, size, bold=False):
        if not self.ok:
            return len(text) * size * 0.58           # conservative fallback
        f = self._font(size, bold)
        try:
            return f.getlength(text)
        except AttributeError:
            return f.getbbox(text)[2]

    def missing(self, ch):
        if self._cmap is None:
            # fallback allowlist
            return ord(ch) > 0x7F and ch not in set("×·–—→←↔↑↓≈≤≥°±§…✓✗")
        return ord(ch) not in self._cmap

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst


def _tw(text, fs, bold=False):
    return _Metrics.get().width(text, fs, bold)


# ---- model ---------------------------------------------------------------
class _Box:
    def __init__(self, bid, col, row, label, desc, kind, colspan, rowspan, ports):
        self.id, self.col, self.row = bid, col, row
        self.colspan, self.rowspan = colspan, rowspan
        self.label = label
        self.desc = desc or []
        self.kind = kind
        self.title = None
        # ports: list of (name, side) -> resolved to fractional positions
        self.ports = {}
        self._port_decl = ports or []
        self.x = self.y = self.w = self.h = 0

    def need_w(self):
        widths = [_tw(self.label, FS_LABEL, bold=True)] + [_tw(d, FS_DESC) for d in self.desc]
        if self.title:
            widths.append(_tw(self.title, FS_LABEL, bold=True))
        return max(widths, default=0) + 2 * PAD

    def need_h(self):
        # honoured by the caller below via max(); see _layout's wire-pitch reservation
        n = (1 if self.label else 0) + len(self.desc) + (1 if self.title else 0)
        return max(n * (FS_DESC + 6) + 2 * PAD + 6, getattr(self, 'min_h', 0))

    @property
    def cx(self): return self.x + self.w / 2
    @property
    def cy(self): return self.y + self.h / 2
    @property
    def right(self): return self.x + self.w
    @property
    def bottom(self): return self.y + self.h

    def _resolve_ports(self):
        by_side = {}
        for name, side in self._port_decl:
            by_side.setdefault(side, []).append(name)
        for side, names in by_side.items():
            for i, name in enumerate(names):
                frac = (i + 1) / (len(names) + 1)
                self.ports[name] = (side, frac)

    def port_point(self, name):
        side, frac = self.ports[name]
        if side == "L":
            return (self.x, self.y + frac * self.h)
        if side == "R":
            return (self.right, self.y + frac * self.h)
        if side == "T":
            return (self.x + frac * self.w, self.y)
        return (self.x + frac * self.w, self.bottom)         # B


class _Edge:
    def __init__(self, src, dst, label, src_side, dst_side, shape, weight):
        self.src, self.src_port = self._split(src)
        self.dst, self.dst_port = self._split(dst)
        self.label = label
        self.src_side, self.dst_side = src_side, dst_side
        self.shape = shape          # "ortho" (default) | "straight" (diagonal ok)
        # weight = line thickness: "signal" | "bus" | "wide", or a number (px)
        self.weight = WEIGHTS.get(weight, weight) if isinstance(weight, str) else float(weight)
        self.pts = []

    @staticmethod
    def _split(ref):
        return tuple(ref.split(":", 1)) if ":" in ref else (ref, None)


class Diagram:
    def __init__(self, title, cols=None, rows=None, gap_x=72, gap_y=44,
                 margin=24, title_h=34):
        self.title = title
        self.ncol, self.nrow = cols, rows
        self.gap_x, self.gap_y, self.margin, self.title_h = gap_x, gap_y, margin, title_h
        self.boxes = {}
        self.edges = []
        self._order = []

    def box(self, bid, col, row, label, desc=None, kind="block",
            colspan=1, rowspan=1, ports=None):
        b = _Box(bid, col, row, label, desc, kind, colspan, rowspan, ports)
        self.boxes[bid] = b
        self._order.append(bid)
        return b

    def node(self, bid, label, desc=None, kind="block", ports=None):
        """Add a box WITHOUT a position; autoplace() assigns (col,row)."""
        return self.box(bid, None, None, label, desc, kind, ports=ports)

    def note(self, col, row, title, lines, colspan=1, rowspan=1):
        b = self.box("__note_%d" % len(self.boxes), col, row, "", lines,
                     kind="note", colspan=colspan, rowspan=rowspan)
        b.title = title
        return b

    def edge(self, src, dst, label=None, src_side=None, dst_side=None,
             shape="ortho", weight="signal"):
        self.edges.append(_Edge(src, dst, label, src_side, dst_side, shape, weight))

    # ---- autoplace (layered, aesthetic-tuned) ----------------------------
    def autoplace(self):
        """Assign (col,row) from connectivity. Layered left-to-right flow with
        median-barycenter row ordering (few crossings, straightened chains) and
        vertically-centered columns (balance). A bounded heuristic seed, not a
        placer/router — override any box's col/row to hand-tune."""
        ids = [b.id for b in self.boxes.values()]
        succ = {i: [] for i in ids}
        pred = {i: [] for i in ids}
        for e in self.edges:
            if e.src in succ and e.dst in pred and e.src != e.dst:
                succ[e.src].append(e.dst)
                pred[e.dst].append(e.src)
        # 1. rank = longest path from sources (bounded so cycles can't hang)
        rank = {i: 0 for i in ids}
        for _ in range(len(ids) + 1):
            changed = False
            for e in self.edges:
                if e.src in rank and e.dst in rank and rank[e.dst] < rank[e.src] + 1:
                    rank[e.dst] = rank[e.src] + 1
                    changed = True
            if not changed:
                break
        # tighten: pull a pure source (no predecessors) rightward to just before
        # its nearest consumer, so its edges span one rank and don't cross boxes
        for _ in range(len(ids) + 1):
            moved = False
            for i in ids:
                if not pred[i] and succ[i]:
                    r = min(rank[s] for s in succ[i]) - 1
                    if r > rank[i]:
                        rank[i] = r
                        moved = True
            if not moved:
                break
        ranks = {}
        for i in ids:
            ranks.setdefault(rank[i], []).append(i)
        maxr = max(rank.values(), default=0)
        pos = {i: float(k) for r in ranks for k, i in enumerate(ranks[r])}

        # 2. ordering sweeps: median barycenter over both neighbour sides
        def bary(i):
            ns = pred[i] + succ[i]
            if not ns:
                return pos[i]
            v = sorted(pos[n] for n in ns)
            m = len(v)
            return v[m // 2] if m % 2 else (v[m // 2 - 1] + v[m // 2]) / 2
        for _ in range(6):
            for r in sorted(ranks):                 # iterate populated ranks only
                ranks[r].sort(key=bary)
                for k, i in enumerate(ranks[r]):
                    pos[i] = float(k)

        # 3. assign cells; centre each column vertically for balance. Columns are
        # re-indexed densely so empty ranks (left by tightening) don't appear.
        #
        # A WIDE RANK IS WRAPPED over several columns. A fan-out puts every child at
        # the same rank, so one parent with eleven children became an eleven-row strip:
        # aspect 0.3:1, every wire from the parent running the length of the column, and
        # fourteen lint failures for wires crossing boxes. There was no free corridor
        # because the column WAS the corridor. Wrapping keeps the diagram near-square,
        # which is both easier to read and leaves the router somewhere to go.
        cap = max(3, int(math.ceil(math.sqrt(max(len(ids), 1)))))
        chunked = []                       # [(rank, [ids])] in column order
        for r in sorted(ranks):
            lst = ranks[r]
            if len(lst) > cap:
                for i in range(0, len(lst), cap):
                    chunked.append((r, lst[i:i + cap]))
            else:
                chunked.append((r, lst))
        nrow = max((len(c) for _, c in chunked), default=1)
        for col, (_, lst) in enumerate(chunked):
            off = (nrow - len(lst)) // 2
            for k, i in enumerate(lst):
                self.boxes[i].col = col
                self.boxes[i].row = off + k
        self.ncol, self.nrow = len(chunked), nrow

    def _crossings(self):
        """Count edge crossings between adjacent columns (lower = cleaner)."""
        n = 0
        es = [(self.boxes[e.src], self.boxes[e.dst]) for e in self.edges]
        for a in range(len(es)):
            for b in range(a + 1, len(es)):
                (s1, d1), (s2, d2) = es[a], es[b]
                if s1.col == s2.col and d1.col == d2.col and s1.col != d1.col:
                    if (s1.row - s2.row) * (d1.row - d2.row) < 0:
                        n += 1
        return n

    # ---- layout ----------------------------------------------------------
    def _layout(self):
        if any(b.col is None for b in self.boxes.values()):
            self.autoplace()
        # A box must be tall enough for the wires attached to it. Eight wires leaving a
        # 40px box put their anchors ~5px apart, so their labels land on top of one
        # another and the picture stops saying which wire is which. Reserve a pitch per
        # attachment on the busier side; the box grows, the anchors spread, the labels
        # fit. Counted before sides are assigned, so it uses in/out counts as the proxy.
        incoming, outgoing = {}, {}
        for e in self.edges:
            outgoing[e.src] = outgoing.get(e.src, 0) + 1
            incoming[e.dst] = incoming.get(e.dst, 0) + 1
        for bid, b in self.boxes.items():
            n = max(incoming.get(bid, 0), outgoing.get(bid, 0))
            if n > 1:
                # Enough for one pitch per wire plus a margin. A larger reservation
                # was tried on the theory that slack would let wires straighten; it
                # did not -- straightness is set by where anchors AIM, not by how much
                # room they have -- and it only made boxes enormous.
                b.min_h = max(getattr(b, "min_h", 0), n * WIRE_PITCH + 12)
        # widen horizontal gaps so an edge label sits over the wire without
        # bumping either box (a label needs room in the corridor between cols).
        max_lbl = max((_tw(e.label, FS_SMALL) for e in self.edges if e.label), default=0)
        if max_lbl:
            self.gap_x = max(self.gap_x, max_lbl + 16)
        col_w = [0] * self.ncol
        row_h = [0] * self.nrow
        for b in self.boxes.values():
            if b.colspan == 1:
                col_w[b.col] = max(col_w[b.col], b.need_w())
            if b.rowspan == 1:
                row_h[b.row] = max(row_h[b.row], b.need_h())
        col_w = [max(w, 90) for w in col_w]
        row_h = [max(h, 50) for h in row_h]

        def cx(c): return self.margin + sum(col_w[:c]) + c * self.gap_x
        def cy(r): return self.margin + self.title_h + sum(row_h[:r]) + r * self.gap_y

        for b in self.boxes.values():
            b.x = cx(b.col); b.y = cy(b.row)
            b.w = sum(col_w[b.col:b.col + b.colspan]) + (b.colspan - 1) * self.gap_x
            b.h = sum(row_h[b.row:b.row + b.rowspan]) + (b.rowspan - 1) * self.gap_y
            b._resolve_ports()
        self._col_w, self._row_h = col_w, row_h
        self.W = cx(self.ncol) - self.gap_x + self.margin
        self.H = cy(self.nrow) - self.gap_y + self.margin

    def _route(self, order_hint=None):
        """Place every wire. `order_hint` overrides the order wires attach to a box
        side, which is the lever _untangle() pulls to reduce crossings."""
        self._vgap_cache = None
        self._hgap_cache = None
        # pass 1: pick a side for each edge end
        info = self._info = []
        for e in self.edges:
            s, d = self.boxes[e.src], self.boxes[e.dst]
            ss = e.src_side or (s.ports[e.src_port][0] if e.src_port else None)
            ds = e.dst_side or (d.ports[e.dst_port][0] if e.dst_port else None)
            if ss is None or ds is None:
                a, b = self._auto_sides(s, d)
                ss = ss or a; ds = ds or b
            info.append((e, s, d, ss, ds))

        # pass 2: group edge-ends sharing a (box, side) and FAN them out so they
        # leave at distinct points (a single edge stays centred / aligned).
        groups = self._groups = {}
        for idx, (e, s, d, ss, ds) in enumerate(info):
            if not e.src_port:
                groups.setdefault((s.id, ss), []).append((idx, "s"))
            if not e.dst_port:
                groups.setdefault((d.id, ds), []).append((idx, "d"))
        anchor = {}
        for (bid, side), mem in groups.items():
            box = self.boxes[bid]

            def other_coord(m):
                idx, end = m
                ob = info[idx][2] if end == "s" else info[idx][1]
                return ob.cy if side in "LR" else ob.cx
            if order_hint:
                mem.sort(key=lambda m: order_hint.get(m, other_coord(m)))
            else:
                mem.sort(key=other_coord)
            k = len(mem)
            # Where each wire WANTS to leave: level with the box at the other end, so
            # the wire can run straight. Even fractions of the box height gave every
            # anchor an arbitrary position that matched nothing -- making boxes taller
            # simply scaled both ends and left straightness unchanged (measured: 9%).
            # Preferences are then separated by a minimum pitch, in preference order,
            # so wires that can be straight are and the rest stay ordered and distinct.
            if k > 1:
                lo = (box.y if side in "LR" else box.x) + 10
                hi = (box.bottom if side in "LR" else box.right) - 10
                want = []
                for idx, end in mem:
                    ob = info[idx][2] if end == "s" else info[idx][1]
                    want.append(min(max(ob.cy if side in "LR" else ob.cx, lo), hi))
                span = max(hi - lo, 1)
                pitch = min(WIRE_PITCH, span / max(k - 1, 1))
                placed = []
                for v in want:
                    if placed and v < placed[-1] + pitch:
                        v = placed[-1] + pitch
                    placed.append(v)
                if placed and placed[-1] > hi:            # slid past the end: shift back
                    shift = placed[-1] - hi
                    placed = [v - shift for v in placed]
                # An anchor MUST end up on its own box. Shifting the run back could push
                # the first entries above `lo`, putting the wire's start outside the box
                # it belongs to -- the wire then began in empty space, which reading the
                # finished SVG back is what exposed. Geometry lint never saw it, because
                # a wire floating beside a box crosses nothing. If the run cannot fit,
                # spread evenly across the side instead of letting anything escape.
                if placed and (placed[0] < lo - 0.01 or placed[-1] > hi + 0.01):
                    placed = [lo + j * span / max(k - 1, 1) for j in range(k)]
                for (idx, end), v in zip(mem, placed):
                    if side in "LR":
                        anchor[(idx, end)] = (box.x if side == "L" else box.right, v)
                    else:
                        anchor[(idx, end)] = (v, box.y if side == "T" else box.bottom)
            for j, (idx, end) in enumerate(mem):
                ob = info[idx][2] if end == "s" else info[idx][1]
                if k == 1:                        # straighten to the other box
                    if side in "LR":
                        oc = _overlap_center(box.y, box.bottom, ob.y, ob.bottom)
                        y = oc if oc is not None else box.cy
                        anchor[(idx, end)] = (box.x if side == "L" else box.right, y)
                    else:
                        oc = _overlap_center(box.x, box.right, ob.x, ob.right)
                        x = oc if oc is not None else box.cx
                        anchor[(idx, end)] = (x, box.y if side == "T" else box.bottom)
                else:                             # several wires on one side
                    pass                          # positions assigned below, together

        # pass 3: build orthogonal (or straight) point lists.
        #
        # Order matters. Each wire is an obstacle to the ones after it, so the short
        # direct hops are routed FIRST -- they have the least freedom and most deserve
        # to be straight, and a long wire has the whole canvas to detour through. Doing
        # it in declaration order let an incidental long wire take the gutter a
        # neighbouring pair needed.
        self._used_h, self._used_v = [], []
        self._box_rects = [(b.x, b.y, b.right, b.bottom) for b in self.boxes.values()]
        ends = {}
        for idx, (e, s, d, ss, ds) in enumerate(info):
            ends[idx] = (s.port_point(e.src_port) if e.src_port else anchor[(idx, "s")],
                         d.port_point(e.dst_port) if e.dst_port else anchor[(idx, "d")])
        order = sorted(range(len(info)),
                       key=lambda i: abs(ends[i][0][0] - ends[i][1][0]) +
                                     abs(ends[i][0][1] - ends[i][1][1]))
        for idx in order:
            e, s, d, ss, ds = info[idx]
            sp, dp = ends[idx]
            if e.shape == "straight":
                e.pts = [sp, dp]
            elif ss in "LR" and ds in "LR":
                # Level ends can go straight across -- but ONLY if nothing is in the
                # way. This short-circuit did not check, so two boxes level with each
                # other drew a wire straight through every block between them, and it
                # was the single worst offender in a crowded diagram: one such wire
                # accounted for four of the overlaps and three of the wires under
                # blocks. Level is a reason to PREFER the straight path, not to skip
                # the check.
                if abs(sp[1] - dp[1]) < 1 and not any(
                        _seg_in_box((sp, dp), b) for b in self.boxes.values()):
                    e.pts = [sp, dp]
                else:
                    default = sp[0] + (self.gap_x / 2 if ss == "R" else -self.gap_x / 2)
                    e.pts = self._ortho_route(sp, dp, default, {s.id, d.id},
                                              1 if ss == "R" else -1,
                                              _arrow_len(e.weight) + 2)
                    continue                       # _ortho_route records its own path
            elif (ss in "TB" and ds in "TB" and abs(sp[0] - dp[0]) < 1
                  and not any(_seg_in_box((sp, dp), b) for b in self.boxes.values())):
                e.pts = [sp, dp]
            else:
                midy = (sp[1] + dp[1]) / 2
                e.pts = [sp, (sp[0], midy), (dp[0], midy), dp]
            # A wire the router did not place is still a wire, and later wires must
            # treat it as occupied ground -- otherwise they are routed around only
            # SOME of what is on the canvas.
            self._remember(e.pts)

    @staticmethod
    def _free_spans(spans, before, after):
        """Merge box extents on one axis and return the free bands between them."""
        gaps, cur = [], None
        for a0, a1 in sorted(spans):
            if cur is None:
                cur = [a0, a1]
                continue
            if a0 > cur[1]:
                gaps.append((cur[1], a0))
                cur = [a0, a1]
            else:
                cur[1] = max(cur[1], a1)
        if cur is not None:
            gaps.append((cur[1], cur[1]))
        out = [g for g in gaps if g[1] - g[0] > 6]
        if spans:
            lo = min(s[0] for s in spans)
            hi = max(s[1] for s in spans)
            out.append((lo - before, lo - 6))
            out.append((hi + 6, hi + after))
        return out

    def _crossings(self):
        """How many times wires cross each other in the current routing."""
        segs = [(sg, i) for i, e in enumerate(self.edges)
                for sg in zip(e.pts, e.pts[1:]) if sg[0] != sg[1]]
        return sum(1 for i, (a, ea) in enumerate(segs)
                   for b, eb in segs[i + 1:] if ea != eb and _crosses(a, b))

    def _untangle(self, rounds=8):
        """Reshuffle which wire attaches where, to reduce crossings.

        Routing decides how ONE wire gets across the page. Most crossings are not
        decided there at all -- they are decided by the ORDER wires attach along a box
        side, and no amount of clever routing undoes a bad order. Two wires whose
        attachment points are swapped relative to their destinations must cross
        somewhere, whatever path each takes.

        So the order is treated as the variable. Each round re-attaches every wire in
        the order of where its OTHER end actually ended up (rather than where its
        partner box roughly sits), re-routes, and counts. That is the classical
        barycentre sweep, and it converges quickly. Adjacent pairs are then swapped one
        at a time, keeping a swap only when it measurably helps -- which catches the
        cases the sweep leaves knotted.

        The best arrangement seen is the one kept, so this can never make a diagram
        worse than not running it.
        """
        self._route()
        best_n, best_hint = self._crossings(), None
        hint, seen = None, set()
        for _ in range(rounds):                       # barycentre sweep
            if best_n == 0:
                break
            new = {}
            for idx, e in enumerate(self.edges):
                if len(e.pts) < 2:
                    continue
                new[(idx, "s")] = e.pts[-1]
                new[(idx, "d")] = e.pts[0]
            hint = {}
            for (bid, side), mem in self._groups.items():
                for m in mem:
                    pt = new.get(m)
                    if pt is not None:
                        hint[m] = pt[1] if side in "LR" else pt[0]
            key = tuple(sorted((k, round(v, 1)) for k, v in hint.items()))
            if key in seen:
                break
            seen.add(key)
            self._route(hint)
            n = self._crossings()
            if n < best_n:
                best_n, best_hint = n, dict(hint)

        # Local improvement. The sweep gets the broad ordering right; these moves fix
        # the knots it leaves. Reversing a whole side is included because a fan-out
        # attached in exactly the wrong sense is one move from correct and many
        # adjacent swaps from it.
        cur = dict(best_hint) if best_hint else {}
        for _ in range(3):
            if not best_n:
                break
            improved = False
            self._route(cur or None)
            for (bid, side), mem in list(self._groups.items()):
                if len(mem) < 2:
                    continue
                order = list(mem)
                vals = sorted(cur.get(m, i) for i, m in enumerate(order))
                trial = dict(cur)
                for m, v in zip(reversed(order), vals):
                    trial[m] = v
                self._route(trial)
                n = self._crossings()
                if n < best_n:
                    best_n, best_hint, cur = n, dict(trial), trial
                    improved = True
                    order.reverse()
                for i in range(len(order) - 1):
                    trial = dict(cur)
                    a, b = order[i], order[i + 1]
                    trial[a], trial[b] = cur.get(b, i + 1), cur.get(a, i)
                    self._route(trial)
                    n = self._crossings()
                    if n < best_n:
                        best_n, best_hint, cur = n, dict(trial), trial
                        order[i], order[i + 1] = b, a
                        improved = True
            if not improved:
                break

        self._route(best_hint)
        return best_n

    def _vgap_spans(self):
        """Vertical corridors as (lo, hi) RANGES, not centres.

        A centre is one line, and one line holds one wire. Handing the router the whole
        width of each gutter is what lets several wires share a corridor while each
        keeping its own track -- the difference between three buses drawn side by side
        and three buses drawn on top of each other, which is what "they merge into one"
        was.
        """
        if getattr(self, "_vgap_cache", None) is None:
            self._vgap_cache = self._free_spans(
                [(b.x, b.right) for b in self.boxes.values()],
                self.gap_x, self.gap_x)
        return self._vgap_cache

    def _hgap_spans(self):
        if getattr(self, "_hgap_cache", None) is None:
            out = self._free_spans([(b.y, b.bottom) for b in self.boxes.values()],
                                   self.gap_y, self.gap_y)
            # Never above the title -- a corridor at the very top drew wires straight
            # through the diagram's own heading.
            self._hgap_cache = [(max(lo, self.title_h + 6), hi) for lo, hi in out
                                if hi > self.title_h + 12]
        return self._hgap_cache

    @staticmethod
    def _tracks(spans, near):
        """Every legal track in these corridors, nearest the wanted position first.

        Tracks are LANE apart, so two wires assigned different tracks can never be
        drawn on top of one another however long they run together.
        """
        out = []
        for lo, hi in spans:
            if hi - lo < 2:
                out.append((lo + hi) / 2)
                continue
            lo, hi = lo + 3, hi - 3
            n = max(int((hi - lo) // LANE) + 1, 1)
            if n == 1:
                out.append((lo + hi) / 2)
                continue
            # centred run of n tracks
            start = (lo + hi) / 2 - (n - 1) * LANE / 2
            out += [start + i * LANE for i in range(n)]
        return sorted(out, key=lambda v: abs(v - near))

    def _vgaps(self):
        return [(a + b) / 2 for a, b in self._vgap_spans()]

    def _hgaps(self):
        return [(a + b) / 2 for a, b in self._hgap_spans()]

    def _ortho_route(self, sp, dp, default, skip_ids, sign, need=0):
        """An orthogonal path from sp to dp that shares no ground with anything else.

        Three hard rules, in order. A path that breaks one is not drawn:

          1. it may not pass under a box
          2. it may not run along another wire -- crossing at 90 degrees is fine,
             travelling together is not
          3. of the paths that satisfy both, take the one that crosses fewest wires,
             then the shortest, then the one with fewest corners

        Rule 2 is the one that was missing, and missing completely: the old clearance
        test asked only whether a path hit a BOX. Wires already on the canvas were
        invisible to it, so three buses that all wanted the same gutter all got it and
        were drawn one on top of another -- which is exactly what "the three wide ones
        merge into one" looks like when the top wire is the widest.

        Placed wires are kept split by orientation, because only a horizontal run can
        lie along another horizontal run, and only a horizontal can cross a vertical.
        Checking every segment against every other was the honest version and far too
        slow to run inside the untangle loop.
        """
        boxes = self._box_rects
        uh, uv = self._used_h, self._used_v

        def ok_and_cost(pts):
            """(crossings, length) for a legal path, or None if it breaks a rule."""
            cross = 0
            total = 0
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                if x1 == x2 and y1 == y2:
                    continue
                total += abs(x2 - x1) + abs(y2 - y1)
                if y1 == y2:                                   # horizontal
                    xa, xb = (x1, x2) if x1 < x2 else (x2, x1)
                    for bx0, by0, bx1, by1 in boxes:
                        if xa < bx1 - 1 and xb > bx0 + 1 and by0 + 1 < y1 < by1 - 1:
                            return None
                    for yy, xx0, xx1 in uh:
                        if abs(yy - y1) < 3 and max(xa, xx0) < min(xb, xx1) - 2:
                            return None
                    for xx, yy0, yy1 in uv:
                        if xa - 1 < xx < xb + 1 and yy0 - 1 < y1 < yy1 + 1:
                            cross += 1
                else:                                          # vertical
                    ya, yb = (y1, y2) if y1 < y2 else (y2, y1)
                    for bx0, by0, bx1, by1 in boxes:
                        if ya < by1 - 1 and yb > by0 + 1 and bx0 + 1 < x1 < bx1 - 1:
                            return None
                    for xx, yy0, yy1 in uv:
                        if abs(xx - x1) < 3 and max(ya, yy0) < min(yb, yy1) - 2:
                            return None
                    for yy, xx0, xx1 in uh:
                        if ya - 1 < yy < yb + 1 and xx0 - 1 < x1 < xx1 + 1:
                            cross += 1
            # The wire must enter its arrowhead through the flat back, not through one
            # of the slanted sides. orient="auto" already aims the head along the last
            # segment, so the axis is right -- but if that segment is SHORTER than the
            # head is long, the corner before it lies inside the triangle and the wire
            # visibly joins the point from the side.
            (lx1, ly1), (lx2, ly2) = pts[-2], pts[-1]
            if abs(lx2 - lx1) + abs(ly2 - ly1) < need:
                return None
            return cross, total

        best = None
        vt = self._tracks(self._vgap_spans(), default)
        for mx in vt[:40]:
            c = ok_and_cost([sp, (mx, sp[1]), (mx, dp[1]), dp])
            if c and (best is None or (c, 2) < best[0]):
                best = ((c, 2), [sp, (mx, sp[1]), (mx, dp[1]), dp])
        if best is None or best[0][0][0] > 0:
            # Five segments: out to a track, along a horizontal band, in. Needed when
            # the target is several columns away -- the 3-segment form's last run has
            # to cross the intervening columns at the target's own height, which is
            # precisely where their boxes are.
            ht = self._tracks(self._hgap_spans(), (sp[1] + dp[1]) / 2)[:10]
            xt = self._tracks(self._vgap_spans(), dp[0])[:10]
            done = False
            for mx in vt[:10]:
                for ex in xt:
                    for my in ht:
                        pts = [sp, (mx, sp[1]), (mx, my), (ex, my), (ex, dp[1]), dp]
                        c = ok_and_cost(pts)
                        if c and (best is None or (c, 4) < best[0]):
                            best = ((c, 4), pts)
                            if c[0] == 0:
                                done = True
                                break
                    if done:
                        break
                if done:
                    break
        if best is not None:
            self._remember(best[1])
            return best[1]

        # Nothing legal. Keep the simple shape, on a track no one else is using if one
        # exists, and let the lint report it rather than hiding the failure.
        for mx in vt:
            pts = [sp, (mx, sp[1]), (mx, dp[1]), dp]
            if all(not (abs(yy - y) < 3 and max(min(a[0], b[0]), xx0) < min(max(a[0], b[0]), xx1) - 2)
                   for a, b in [(pts[0], pts[1]), (pts[2], pts[3])]
                   for y in [a[1]] for yy, xx0, xx1 in uh):
                self._remember(pts)
                return pts
        pts = [sp, (default, sp[1]), (default, dp[1]), dp]
        self._remember(pts)
        return pts

    def _remember(self, pts):
        """Record a placed path so later wires treat it as an obstacle."""
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if x1 == x2 and y1 == y2:
                continue
            if y1 == y2:
                self._used_h.append((y1, min(x1, x2), max(x1, x2)))
            else:
                self._used_v.append((x1, min(y1, y2), max(y1, y2)))

    @staticmethod
    def _auto_sides(s, d):
        if d.x >= s.right - 1:
            return "R", "L"
        if d.right <= s.x + 1:
            return "L", "R"
        if d.y >= s.bottom - 1:
            return "B", "T"
        if d.bottom <= s.y + 1:
            return "T", "B"
        dx, dy = d.cx - s.cx, d.cy - s.cy
        if abs(dx) >= abs(dy):
            return ("R", "L") if dx > 0 else ("L", "R")
        return ("B", "T") if dy > 0 else ("T", "B")

    def _anchor(self, b, side):
        return {"L": (b.x, b.cy), "R": (b.right, b.cy),
                "T": (b.cx, b.y), "B": (b.cx, b.bottom)}[side]

    # ---- emit ------------------------------------------------------------
    def _svg(self):
        self._layout(); self._untangle()
        self._lbl_boxes = []       # label rectangles already placed, so none collide
        # Grow the canvas to whatever the router actually used. Detour corridors can sit
        # outside the box extents -- that is the point of them -- and the width computed
        # from the grid alone left those wires running off the edge of the drawing.
        for e in self.edges:
            for x, y in e.pts:
                self.W = max(self.W, x + self.margin)
                self.H = max(self.H, y + self.margin)
        o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.W:.0f}" '
             f'height="{self.H:.0f}" viewBox="0 0 {self.W:.0f} {self.H:.0f}" font-family="Arial">']
        # One arrowhead marker per line weight. Two things this gets right that the
        # previous version did not:
        #
        #   SYMMETRY. The old triangle was (0,0) (L,h/2) (0,h-2): the tip sat at h/2
        #   while the base ran 0..h-2, so its midpoint was (h-2)/2. The head was skewed
        #   by exactly one unit and refY aligned the tip rather than the axis, which is
        #   why arrowheads looked off-centre against their wire. The base is now
        #   symmetric about refY, so the head's axis IS the wire's centreline.
        #
        #   PROPORTION. A 6px bus with a 9px head reads as a skinny dart on a fat wire.
        #   The base is now at least 2.6x the stroke, so a heavy wire gets a head that
        #   looks like it belongs to it, while a 1px signal keeps the small tasteful one.
        weights = sorted({e.weight for e in self.edges})
        defs = ['<defs>']
        for w in weights:
            base = max(ARROW + 1.4 * w, 2.6 * w)      # across the wire
            length = base * 1.15                      # along it
            defs.append(
                f'<marker id="arr{_wkey(w)}" markerUnits="userSpaceOnUse" '
                f'markerWidth="{length:.1f}" markerHeight="{base:.1f}" '
                f'refX="{length:.1f}" refY="{base/2:.1f}" orient="auto">'
                f'<path d="M0,0 L{length:.1f},{base/2:.1f} L0,{base:.1f} Z" '
                f'fill="{BLUE}"/></marker>')
        defs.append('</defs>')
        o.append("".join(defs))
        o.append(f'<text x="{self.margin}" y="22" fill="{BLUE}" font-size="15" '
                 f'font-weight="bold">{_esc(self.title)}</text>')
        for e in self.edges:
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in e.pts)
            o.append(f'<polyline points="{pts}" fill="none" stroke="{BLUE}" '
                     f'stroke-width="{e.weight:.1f}" marker-end="url(#arr{_wkey(e.weight)})"/>')
            if e.label:
                # Label the wire where it can be read. On a long run the two ends can be
                # most of the diagram apart, and a single mid-wire label means tracing
                # the line back to find out what it is -- so a long wire is named at
                # BOTH ends. A halo goes behind the glyphs because dark text over a dark
                # fat wire is simply not readable.
                span = sum(abs(q[0] - r[0]) + abs(q[1] - r[1])
                           for q, r in zip(e.pts, e.pts[1:]))
                # A long wire is named at BOTH ends: when the ends are most of the
                # diagram apart, one mid-wire label means tracing the line back to
                # find out what it is.
                spots = self._label_spots(e, 2 if span > LABEL_BOTH_ENDS else 1)
                # If there is genuinely nowhere legible, the name is NOT printed on
                # top of a block to pretend the wire is labelled. The verifier then
                # reports it, which is the honest outcome: a diagram too crowded to
                # name its wires needs fewer boxes, not smaller lies.
                for lx, ly in spots:
                    o.append(f'<text x="{lx:.0f}" y="{ly:.0f}" fill="{GREY}" '
                             f'font-size="{FS_SMALL}" text-anchor="middle" '
                             f'stroke="#ffffff" stroke-width="3" stroke-linejoin="round" '
                             f'paint-order="stroke">{_esc(e.label)}</text>')
        for bid in self._order:
            o.append(self._box_svg(self.boxes[bid]))
        o.append("</svg>")
        return "\n".join(o)

    def _lbl_free(self, cx, cy, w, e, others):
        """Is this a legal place for a label? Returns its box, or None.

        Positions are ROUNDED first, because that is what gets written into the SVG
        (`x="{:.0f}"`). Checking the exact float and emitting the rounded one let a
        label drift up to half a pixel into a block after it had been approved -- which
        is precisely how a graze got past the check.
        """
        cx, cy = round(cx), round(cy)
        m = 2                                  # don't let a label graze a block either
        bb = (cx - w / 2 - m, cy - FS_SMALL - m, cx + w / 2 + m, cy + 2 + m)
        if any(bb[0] < b.right and bb[2] > b.x and
               bb[1] < b.bottom and bb[3] > b.y for b in self.boxes.values()):
            return None
        if any(bb[0] < o[2] and bb[2] > o[0] and
               bb[1] < o[3] and bb[3] > o[1] for o in self._lbl_boxes):
            return None
        # The label must be unmistakably NEARER its own wire than any other. Without
        # this a name printed between two closely spaced wires belongs, visually, to
        # neither -- and reading the finished SVG back showed exactly that: names
        # swapping between neighbours. A reader cannot resolve that ambiguity any
        # better than the checker can, so it is a defect in the drawing, not the check.
        mine = _dist_to_pts((cx, cy), e.pts)
        if any(_dist_to_pts((cx, cy), p) < mine + 8 for p in others):
            return None
        return bb, cx, cy

    def _label_spots(self, e, want):
        """Find up to `want` places on this wire where its name can actually be read.

        A label centred over the longest run and sitting above the wire is right when
        there is room, and wrong the moment the run passes a box: half the text vanishes
        under the block, and a name that is half visible is no name at all.

        So every horizontal run is a candidate, longest first, and within a run the
        label slides along and may drop below the wire. Chosen spots are also kept clear
        of each OTHER, so a wire named at both ends does not print its two labels on top
        of one another, and clear of labels already placed on other wires.

        Dropping the label is the last resort, not the second: an unnamed wire cannot be
        checked against the RTL by anyone reading the picture, which is the whole point
        of naming it.
        """
        w = _tw(e.label, FS_SMALL)
        # Only wires with a DIFFERENT name can be confused with this one. Three
        # parallel wires all called auto_source_in_d_bits_data are not ambiguous --
        # whichever one a reader attributes the label to, they read the same name --
        # and treating them as rivals suppressed all three names for no gain.
        others = [o.pts for o in self.edges
                  if o is not e and len(o.pts) > 1 and o.label != e.label]
        runs = sorted(((abs(q[0] - r[0]), q, r) for q, r in zip(e.pts, e.pts[1:])
                       if abs(q[1] - r[1]) < 1), reverse=True)
        # A wire that runs mostly vertically has no long horizontal run to sit a name
        # on. Rather than leave it nameless, treat its vertical runs as candidates too,
        # with the text beside the line instead of above it.
        vruns = sorted(((abs(q[1] - r[1]), q, r) for q, r in zip(e.pts, e.pts[1:])
                        if abs(q[0] - r[0]) < 1 and abs(q[1] - r[1]) > FS_SMALL * 2),
                       reverse=True)
        out = []
        for _, q, r in runs:
            if len(out) >= want:
                break
            x0, x1 = sorted((q[0], r[0]))
            above = q[1] - 5 - e.weight / 2
            below = q[1] + FS_SMALL + 3 + e.weight / 2
            for frac in (0.5, 0.3, 0.7, 0.15, 0.85):
                cx = x0 + (x1 - x0) * frac
                # Allow a little overhang: a short run can still carry a name, and the
                # alternative is no name at all.
                if cx - w / 2 < x0 - w * 0.4 or cx + w / 2 > x1 + w * 0.4:
                    continue
                for cy in (above, below):
                    got = self._lbl_free(cx, cy, w, e, others)
                    if not got:
                        continue
                    bb, rx, ry = got
                    self._lbl_boxes.append(bb)
                    out.append((rx, ry))
                    break
                else:
                    continue
                break
        for _, q, r in vruns:
            if len(out) >= want:
                break
            y0, y1 = sorted((q[1], r[1]))
            for frac in (0.5, 0.3, 0.7):
                cy = y0 + (y1 - y0) * frac
                for cx in (q[0] + w / 2 + 5 + e.weight / 2,
                           q[0] - w / 2 - 5 - e.weight / 2):
                    got = self._lbl_free(cx, cy, w, e, others)
                    if not got:
                        continue
                    bb, cx, cy = got
                    self._lbl_boxes.append(bb)
                    out.append((cx, cy))
                    break
                else:
                    continue
                break
        return out

    def _box_svg(self, b):
        styles = {
            "emphasis": (EMPH_FILL, BLUE, "#fff", "#dde", ""),
            "ip": (IP_FILL, "#888", BLUE, GREY, ' stroke-dasharray="4 3"'),
            "note": (NOTE_FILL, BLUE, BLUE, GREY, ""),
            "block": (BLOCK_FILL, BLUE, BLUE, GREY, ""),
        }
        fill, stroke, lab_c, desc_c, dash = styles.get(b.kind, styles["block"])
        s = [f'<rect x="{b.x:.0f}" y="{b.y:.0f}" width="{b.w:.0f}" height="{b.h:.0f}" '
             f'rx="5" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>']
        ty = b.y + PAD + FS_LABEL
        head = b.title or b.label
        if head:
            s.append(f'<text x="{b.x+PAD:.0f}" y="{ty:.0f}" fill="{lab_c}" '
                     f'font-size="{FS_LABEL}" font-weight="bold">{_esc(head)}</text>')
            ty += FS_DESC + 8
        for line in b.desc:
            s.append(f'<text x="{b.x+PAD:.0f}" y="{ty:.0f}" fill="{desc_c}" '
                     f'font-size="{FS_DESC}">{_esc(line)}</text>')
            ty += FS_DESC + 6
        for name, (side, frac) in b.ports.items():
            px, py = b.port_point(name)
            s.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.5" fill="{stroke}"/>')
            if side in "LR":
                tx = px + (6 if side == "L" else -6)
                anc = "start" if side == "L" else "end"
                s.append(f'<text x="{tx:.0f}" y="{py+3:.0f}" fill="{desc_c}" '
                         f'font-size="{FS_PORT}" text-anchor="{anc}">{_esc(name)}</text>')
            else:
                s.append(f'<text x="{px:.0f}" y="{py+(12 if side=="T" else -5):.0f}" fill="{desc_c}" '
                         f'font-size="{FS_PORT}" text-anchor="middle">{_esc(name)}</text>')
        return "\n".join(s)

    # ---- save + gate -----------------------------------------------------
    def save(self, svg_path, render=True):
        svg = self._svg()
        os.makedirs(os.path.dirname(os.path.abspath(svg_path)), exist_ok=True)
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg)
        report = self.lint()
        png = None
        if render:
            png = os.path.splitext(svg_path)[0] + ".png"
            try:
                subprocess.run(["inkscape", svg_path, "--export-type=png",
                                f"--export-filename={png}", "--export-dpi=110"],
                               check=True, capture_output=True)
            except Exception as e:
                report.append(("WARN", f"inkscape render failed: {e}"))
        print(f"WROTE {svg_path}" + (f" + {png}" if png else ""))
        for level, msg in report:
            print(f"  [{level}] {msg}")
        fails = [m for l, m in report if l == "FAIL"]
        print("  lint: " + ("OK" if not report else ("FAILED" if fails else "warnings only")))
        return report

    def lint(self, reroute=True):
        """Geometric checks over the drawn result.

        `reroute=False` checks the geometry exactly as it stands. The router is good
        enough now that some violations cannot be produced through the normal path at
        all, so the only way to test that the DETECTOR still works is to hand it
        constructed geometry -- otherwise the checks quietly become untested.
        """
        if reroute:
            # Must be _untangle, not _route: the SVG is drawn from the untangled
            # arrangement, so linting a plain route would report on geometry that was
            # never actually drawn.
            self._layout(); self._untangle()
        rep = []
        bs = list(self.boxes.values())
        # box overlap
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                a, b = bs[i], bs[j]
                if a.x < b.right and b.x < a.right and a.y < b.bottom and b.y < a.bottom:
                    rep.append(("FAIL", f"box overlap: {a.id} ∩ {b.id}"))
        # text overflow (measured)
        for b in bs:
            inner = b.w - 2 * PAD
            head = b.title or b.label
            if head and _tw(head, FS_LABEL, bold=True) > inner + 0.5:
                rep.append(("WARN", f"label overflow {b.id!r}: {head!r}"))
            for t in b.desc:
                if _tw(t, FS_DESC) > inner + 0.5:
                    rep.append(("WARN", f"text overflow {b.id!r}: {t!r}"))
        # wire through a non-endpoint box (orthogonal edges only; a diagonal
        # "straight" edge is an explicit author choice and is exempt)
        for e in self.edges:
            if e.shape == "straight":
                continue
            keep = {e.src, e.dst}
            for k in range(len(e.pts) - 1):
                seg = (e.pts[k], e.pts[k + 1])
                for b in bs:
                    if b.id in keep:
                        continue
                    if _seg_in_box(seg, b):
                        rep.append(("FAIL", f"wire {e.src}->{e.dst} crosses box {b.id} "
                                    f"— fix: place {e.src} and {e.dst} in adjacent cells, "
                                    f"or set src_side=/dst_side= to leave via a free side, "
                                    f"or move {b.id} out of the straight corridor between them"))
        # stacked parallel segments (report which edges collide, and how to fix)
        segs = [((e.pts[k], e.pts[k + 1]), e) for e in self.edges for k in range(len(e.pts) - 1)]
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                ea, eb = segs[i][1], segs[j][1]
                if ea is eb:
                    continue
                if _stacked(segs[i][0], segs[j][0]):
                    rep.append(("FAIL", f"stacked parallel segments: {ea.src}->{ea.dst} and "
                                f"{eb.src}->{eb.dst} overlap in one corridor — fix: remove the "
                                f"duplicate edge, give them distinct src_side/dst_side, or "
                                f"route one through an intermediate box"))
        # glyphs missing from the font
        M = _Metrics.get()
        seen = set()
        for b in bs:
            for t in ([b.title or "", b.label] + b.desc):
                for ch in t:
                    if ch not in seen and M.missing(ch):
                        seen.add(ch)
                        rep.append(("WARN", f"glyph {ch!r} (U+{ord(ch):04X}) missing from font"))
        for e in self.edges:
            for ch in (e.label or ""):
                if ch not in seen and M.missing(ch):
                    seen.add(ch)
                    rep.append(("WARN", f"glyph {ch!r} missing from font (edge label)"))
        # aesthetic advisories (not failures)
        nx = self._crossings()
        if nx:
            rep.append(("WARN", f"{nx} edge crossing(s) — reorder or hand-tune rows"))
        ar = self.W / max(self.H, 1)
        if ar > 3.2 or ar < 0.31:
            rep.append(("WARN", f"aspect ratio {ar:.1f}:1 — long/thin; consider "
                                "a different flow or wrapping the chain"))
        # dedupe
        out, s = [], set()
        for r in rep:
            if r not in s:
                s.add(r); out.append(r)
        return out


# ---- geometry helpers ----------------------------------------------------
def _overlap_center(a0, a1, b0, b1):
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo + hi) / 2 if hi - lo > 8 else None


def _seg_in_box(seg, b):
    (x1, y1), (x2, y2) = seg
    lo_x, hi_x = sorted((x1, x2)); lo_y, hi_y = sorted((y1, y2))
    # interior overlap on both axes (strict, with small margin)
    m = 1.0
    return (lo_x < b.right - m and hi_x > b.x + m and
            lo_y < b.bottom - m and hi_y > b.y + m)


def _seg_len(sg):
    (x1, y1), (x2, y2) = sg
    return abs(x2 - x1) + abs(y2 - y1)


def _dist_to_pts(pt, pts):
    """Manhattan distance from a point to the nearest point on a polyline."""
    px, py = pt
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            continue
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, abs(px - (x1 + t * dx)) + abs(py - (y1 + t * dy)))
    return best


def _arrow_len(w):
    """How far the arrowhead reaches back along the wire from its tip."""
    return max(ARROW + 1.4 * w, 2.6 * w) * 1.15


def _crosses(s1, s2):
    """True if one segment cuts the other at right angles.

    A crossing is allowed -- wires must sometimes cross -- but each one costs a reader
    a moment, so the router counts them and prefers the path that makes fewest.
    """
    (ax1, ay1), (ax2, ay2) = s1
    (bx1, by1), (bx2, by2) = s2
    a_h, b_h = abs(ay1 - ay2) < 1, abs(by1 - by2) < 1
    a_v, b_v = abs(ax1 - ax2) < 1, abs(bx1 - bx2) < 1
    if a_h and b_v:
        h, v = s1, s2
    elif a_v and b_h:
        h, v = s2, s1
    else:
        return False
    (hx1, hy), (hx2, _) = h
    (vx, vy1), (_, vy2) = v
    return (min(hx1, hx2) - 1 < vx < max(hx1, hx2) + 1 and
            min(vy1, vy2) - 1 < hy < max(vy1, vy2) + 1)


def _stacked(s1, s2):
    (ax1, ay1), (ax2, ay2) = s1
    (bx1, by1), (bx2, by2) = s2
    if abs(ay1 - ay2) < 1 and abs(by1 - by2) < 1 and abs(ay1 - by1) < 3:
        return max(min(ax1, ax2), min(bx1, bx2)) < min(max(ax1, ax2), max(bx1, bx2)) - 2
    if abs(ax1 - ax2) < 1 and abs(bx1 - bx2) < 1 and abs(ax1 - bx1) < 3:
        return max(min(ay1, ay2), min(by1, by2)) < min(max(ay1, ay2), max(by1, by2)) - 2
    return False


def _wkey(w):
    return str(w).replace(".", "p")


def _esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---- self-test -----------------------------------------------------------
def _selftest():
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(("ok  " if cond else "FAIL") + "  " + name)
        fails += 0 if cond else 1

    # metrics available + measured width grows with length
    M = _Metrics.get()
    check("metrics: font resolved (measured, not fallback)", M.ok)
    check("metrics: longer text is wider", _tw("WWWWWW", 13) > _tw("W", 13))

    # clean diagram lints clean
    d = Diagram("t", cols=2, rows=2)
    d.box("a", 0, 0, "A", ["x"])
    d.box("b", 1, 0, "B", ["y"])
    d.edge("a", "b")
    check("clean diagram: no FAIL", not any(l == "FAIL" for l, _ in d.lint()))

    # overlap detection (force two boxes into same cell via absolute coords)
    d2 = Diagram("t", cols=1, rows=1)
    b1 = d2.box("a", 0, 0, "A"); b2 = d2.box("b", 0, 0, "B")
    d2._layout()
    b2.x, b2.y, b2.w, b2.h = b1.x, b1.y, b1.w, b1.h
    check("overlap detected", any("overlap" in m for l, m in d2.lint() if l == "FAIL"))

    # wire-through-box. A->C with B between them USED to produce this, because a
    # wire whose ends were level was drawn straight across without checking what stood
    # in the way. The router now routes around B, so the lint has nothing to report --
    # which is the right outcome, but it means the case no longer exercises the lint.
    # So both things are checked: that the router avoids B, and that the lint still
    # catches the violation when a path is forced through one.
    d3 = Diagram("t", cols=3, rows=1)
    d3.box("a", 0, 0, "A"); d3.box("bmid", 1, 0, "B"); d3.box("c", 2, 0, "C")
    d3.edge("a", "c")
    d3._layout(); d3._route()
    check("router: level ends route AROUND the box between them",
          not any("crosses box" in m for l, m in d3.lint() if l == "FAIL"))
    mid = d3.boxes["bmid"]
    d3.edges[0].pts = [(d3.boxes["a"].right, mid.cy), (d3.boxes["c"].x, mid.cy)]
    check("wire-through-box detected",
          any("crosses box" in m for l, m in d3.lint(reroute=False) if l == "FAIL"))

    # glyph coverage: a clearly-absent glyph warns
    d4 = Diagram("t", cols=1, rows=1)
    d4.box("a", 0, 0, "A", ["\U0001F600"])     # emoji, not in Arial
    check("missing glyph warned",
          any("missing from font" in m for l, m in d4.lint()))

    # ports resolve and edge attaches
    d5 = Diagram("t", cols=2, rows=1)
    d5.box("a", 0, 0, "A", ports=[("p0", "R")])
    d5.box("b", 1, 0, "B")
    d5.edge("a:p0", "b")
    d5._layout(); d5._route()
    check("port resolved + edge uses it", "p0" in d5.boxes["a"].ports)

    # diagonal (straight) edge: 2-point line, exempt from wire-through-box
    d6 = Diagram("t", cols=3, rows=2)
    d6.box("a", 0, 0, "A"); d6.box("bmid", 1, 0, "B"); d6.box("c", 2, 1, "C")
    d6.edge("a", "c", shape="straight")
    rep6 = d6.lint()
    check("straight edge is 2-point", len(d6.edges[0].pts) == 2)
    check("straight edge exempt from crossing FAIL",
          not any("crosses box" in m for l, m in rep6 if l == "FAIL"))

    # label spacing widens the horizontal gap so a label clears the boxes
    d7 = Diagram("t", cols=2, rows=1, gap_x=20)
    d7.box("a", 0, 0, "A"); d7.box("b", 1, 0, "B")
    d7.edge("a", "b", label="a-very-wide-bus-label")
    d7._layout()
    check("label spacing widened the gap", d7.gap_x >= _tw("a-very-wide-bus-label", FS_SMALL))

    # autoplace: ranks follow dataflow (source col < sink col), no overlap
    d8 = Diagram("t")                       # no cols/rows -> autoplace
    for n in "abcd":
        d8.node(n, n.upper())
    d8.node("e", "E")
    d8.edge("a", "b"); d8.edge("a", "c"); d8.edge("b", "d"); d8.edge("c", "d"); d8.edge("d", "e")
    d8._layout()
    check("autoplace: source left of sink", d8.boxes["a"].col < d8.boxes["e"].col)
    check("autoplace: branch siblings share a column",
          d8.boxes["b"].col == d8.boxes["c"].col)
    check("autoplace: no FAILs (fan-out, no stacking/overlap)",
          not any(l == "FAIL" for l, _ in d8.lint()))

    # fan-out: two edges leaving the same side must not stack collinearly
    d9 = Diagram("t", cols=2, rows=2)
    d9.box("a", 0, 0, "A", rowspan=2); d9.box("b", 1, 0, "B"); d9.box("c", 1, 1, "C")
    d9.edge("a", "b"); d9.edge("a", "c")
    check("fan-out: no stacked segments from shared side",
          not any("stacked" in m for l, m in d9.lint() if l == "FAIL"))

    # line weight: signal(1px) < bus < fat -> 3 distinct stroke widths
    d10 = Diagram("t", cols=2, rows=3)
    for r, w in enumerate(("signal", "bus", "fat")):
        d10.box(f"s{r}", 0, r, w); d10.box(f"d{r}", 1, r, w.upper())
        d10.edge(f"s{r}", f"d{r}", weight=w)
    svg = d10._svg()
    # --- the three rules the router now enforces, on a graph dense enough to break
    # them. These were all violated before the router treated placed wires as
    # obstacles: a 20-box fan-out drew 46 pairs of wires along each other.
    d10 = Diagram("crowded")
    for i in range(12):
        d10.node(f"n{i}", f"Block{i}")
    for i in range(1, 12):
        d10.edge("n0", f"n{i}", label=f"bus_{i} [64]", weight="bus")
    for i in range(1, 6):
        d10.edge(f"n{i}", f"n{i + 6}", label=f"link_{i} [8]")
    d10.autoplace()
    svg10 = d10._svg()
    segs = [sg for e in d10.edges for sg in zip(e.pts, e.pts[1:]) if sg[0] != sg[1]]
    check("crowded: no wire runs along another wire",
          not any(_stacked(a, b) for i, a in enumerate(segs) for b in segs[i + 1:]))
    check("crowded: no wire passes under a box",
          not any(_seg_in_box(sg, b) for sg in segs for b in d10.boxes.values()
                  if not any(abs(sg[k][0] - c) < 0.01 or abs(sg[k][1] - r) < 0.01
                             for k in (0, 1) for c in (b.x, b.right) for r in (b.y, b.bottom))))
    import re as _re
    lbls = [(float(m.group(1)), float(m.group(2)), m.group(3)) for m in
            _re.finditer(r'<text x="([\d.-]+)" y="([\d.-]+)"[^>]*paint-order="stroke">([^<]*)</text>', svg10)]
    check("crowded: no wire label sits on a block",
          not any(lx - _tw(t, FS_SMALL) / 2 < b.right and lx + _tw(t, FS_SMALL) / 2 > b.x
                  and ly - FS_SMALL < b.bottom and ly + 2 > b.y
                  for lx, ly, t in lbls for b in d10.boxes.values()))
    check("crowded: every wire enters its arrowhead from the flat back",
          all(_seg_len((e.pts[-2], e.pts[-1])) >= _arrow_len(e.weight)
              for e in d10.edges))
    d11 = Diagram("tangle")
    for i in range(8):
        d11.node(f"m{i}", f"M{i}")
    for i in range(4):                       # deliberately crossed fan-out
        d11.edge("m0", f"m{4 + (3 - i)}", label=f"w{i} [8]")
    d11.autoplace(); d11._layout()
    d11._route(); before = d11._crossings()
    check("untangle never makes crossings worse", d11._untangle() <= before)

    check("crowded: most wires still carry their name",
          len(lbls) >= int(0.8 * len(d10.edges)))

    check("weights: signal is 1px", 'stroke-width="1.0"' in svg)
    check("weights: 3 distinct stroke widths",
          all(f'stroke-width="{WEIGHTS[w]:.1f}"' in svg for w in ("signal", "bus", "fat")))
    check("weights: arrowhead does not scale with stroke (userSpaceOnUse)",
          'markerUnits="userSpaceOnUse"' in svg)

    print(f"\n{'ALL PASS' if not fails else str(fails)+' FAILED'}")
    return fails


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(__doc__)
