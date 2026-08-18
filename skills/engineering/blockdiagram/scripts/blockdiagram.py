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
BUCKET = 32        # obstacle index granularity (px) -- see Diagram._route
# line weight encodes cardinality (THREE tiers only):
#   signal = one wire (1 px) · bus = a little wider · fat = a fat bus (much wider)
WEIGHTS = {"signal": 1.0, "bus": 3.0, "fat": 6.0, "wide": 6.0}  # "wide" = alias of fat
# Wire colour groups what a wire CARRIES, so a reader can pick out the control paths
# from the data paths without reading a single label. Kept few and kept dark: colour is
# a grouping cue here, not decoration, and a diagram with nine wire colours has none.
TAP = 16           # length of a rail stub (a clock/reset tap into a block)
WIRE_KINDS = {
    "data": "#1F4E79",         # house blue — payload
    "control": "#B8860B",      # amber — valid/ready, config, mode
    "clock": "#6A5ACD",        # slate — clock and reset distribution
    "interrupt": "#A03030",    # red — interrupts and error reporting
}


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
    def __init__(self, src, dst, label, src_side, dst_side, shape, weight, kind="data"):
        self.src, self.src_port = self._split(src)
        self.dst, self.dst_port = self._split(dst)
        self.label = label
        self.src_side, self.dst_side = src_side, dst_side
        self.shape = shape          # "ortho" (default) | "straight" (diagonal ok)
        self.kind = kind if kind in WIRE_KINDS else "data"
        self.color = WIRE_KINDS[self.kind]
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

    def rail(self, src, dsts, label=None, kind="clock", weight="signal", side="T"):
        """A global signal (clock, reset, scan) drawn as a TAP on each block.

        Clock and reset go to everything, and drawn as one wire per block they are what
        wrecks a deep diagram: eleven of them across five levels measured in the hundreds
        of crossings, and no router can help, because those wires genuinely do go
        everywhere. A spec does not draw them either -- it draws a tap at each block and
        names the source once. That is O(1) ink per block instead of O(depth), and a stub
        local to its own box cannot cross anything.

        The relationship stays in the picture and in the diagram source (`src` is carried
        on every tap edge, so verify_diagram can still account for it); it is simply not
        traced across the figure. Taps take no part in ranking or placement.
        """
        for dst in dsts:
            self.edges.append(_Edge(src, dst, label, side, side, "tap", weight, kind))
        return self

    def note(self, col, row, title, lines, colspan=1, rowspan=1):
        b = self.box("__note_%d" % len(self.boxes), col, row, "", lines,
                     kind="note", colspan=colspan, rowspan=rowspan)
        b.title = title
        return b

    def edge(self, src, dst, label=None, src_side=None, dst_side=None,
             shape="ortho", weight="signal", kind="data"):
        """`kind` groups wires by what they carry — see WIRE_KINDS."""
        self.edges.append(_Edge(src, dst, label, src_side, dst_side, shape, weight, kind))

    # ---- autoplace (layered, aesthetic-tuned) ----------------------------
    @staticmethod
    def _find_back_edges(succ):
        """Edges that point back into the path being walked -- the cycles, in other words.

        Depth-first, iterative (a deep hierarchy would blow a recursive one), and
        deterministic: roots are visited in insertion order, so the same graph always
        yields the same back-edge set and the same picture."""
        back, state = set(), {}                     # state: 0 = on stack, 1 = done
        for root in succ:
            if root in state:
                continue
            stack = [(root, iter(succ[root]))]
            state[root] = 0
            while stack:
                node, it = stack[-1]
                for nxt in it:
                    if state.get(nxt) == 0:         # points back into the current path
                        back.add((node, nxt))
                    elif nxt not in state:
                        state[nxt] = 0
                        stack.append((nxt, iter(succ[nxt])))
                        break
                else:
                    state[node] = 1
                    stack.pop()
        return back

    def autoplace(self):
        """Assign (col,row) from connectivity. Layered left-to-right flow with
        median-barycenter row ordering (few crossings, straightened chains) and
        vertically-centered columns (balance). A bounded heuristic seed, not a
        placer/router — override any box's col/row to hand-tune."""
        ids = [b.id for b in self.boxes.values()]
        succ = {i: [] for i in ids}
        pred = {i: [] for i in ids}
        for e in self.edges:
            if e.shape == "tap":          # local to its own box; it places nothing
                continue
            if e.src in succ and e.dst in pred and e.src != e.dst:
                succ[e.src].append(e.dst)
                pred[e.dst].append(e.src)
        # 1. break the cycles BEFORE ranking. A feedback wire is a fact of hardware --
        # redirect, stall, retry, credit return -- and ranking straight through one puts
        # the loop's boxes in the wrong order: a fetch/decode/exec/wb pipeline with a
        # redirect came out with exec at column 0, LEFT of fetch, because the longest
        # path ran round the loop. Find the back edges with a depth-first walk, rank on
        # the DAG that is left, and let the back edges be drawn as what they are: wires
        # running against the flow, which the flow convention already handles.
        self._back_edges = back = self._find_back_edges(succ)
        fpred = {i: [x for x in pred[i] if (x, i) not in back] for i in ids}
        fsucc = {i: [x for x in succ[i] if (i, x) not in back] for i in ids}

        # rank = longest path from sources over the forward edges
        rank = {i: 0 for i in ids}
        for _ in range(len(ids) + 1):
            changed = False
            for e in self.edges:
                if (e.src, e.dst) in back or e.src == e.dst or e.shape == "tap":
                    continue
                if e.src in rank and e.dst in rank and rank[e.dst] < rank[e.src] + 1:
                    rank[e.dst] = rank[e.src] + 1
                    changed = True
            if not changed:
                break
        # tighten: pull a pure source (no predecessors) rightward to just before
        # its nearest consumer, so its edges span one rank and don't cross boxes.
        # "Pure source" counts forward edges only -- a box fed solely by a feedback
        # wire is still a source of the flow, and treating it as fed pinned it left.
        for _ in range(len(ids) + 1):
            moved = False
            for i in ids:
                if not fpred[i] and fsucc[i]:
                    r = min(rank[s] for s in fsucc[i]) - 1
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

        # 2. ordering sweeps, ONE SIDE AT A TIME, alternating: forward by parents,
        # backward by children. Taking the median over both sides at once does not
        # converge -- a box is pulled towards its own children while its siblings are
        # pulled elsewhere -- and in a plain containment tree that scattered every
        # parent's children across their rank: 27 crossings on a 40-box tree that is
        # drawable with none. A forward sweep groups each parent's children by
        # construction, which also matters for the wrapping below, since a chunk of a
        # wide rank then holds whole sibling groups instead of parts of three.
        # The best ordering seen is kept, scored on adjacent-rank inversions (cheap;
        # the drawn geometry is scored properly later by _optimise).
        def bary(i, side):
            ns = side[i]
            if not ns:
                return pos[i]
            v = sorted(pos[n] for n in ns)
            m = len(v)
            return v[m // 2] if m % 2 else (v[m // 2 - 1] + v[m // 2]) / 2

        def inversions():
            n = 0
            es = [(e.src, e.dst) for e in self.edges
                  if e.src in rank and e.dst in rank and (e.src, e.dst) not in back]
            for a in range(len(es)):
                s1, d1 = es[a]
                for b in range(a + 1, len(es)):
                    s2, d2 = es[b]
                    if rank[s1] != rank[s2] or rank[d1] != rank[d2]:
                        continue
                    if (pos[s1] - pos[s2]) * (pos[d1] - pos[d2]) < 0:
                        n += 1
            return n

        rank_order = sorted(ranks)
        best, best_pos = inversions(), dict(pos)
        for it in range(8):
            if it % 2 == 0:
                sweep, side = rank_order[1:], fpred      # forward: follow the parents
            else:
                sweep, side = list(reversed(rank_order[:-1])), fsucc
            for r in sweep:
                ranks[r].sort(key=lambda i: bary(i, side))
                for k, i in enumerate(ranks[r]):
                    pos[i] = float(k)
            got = inversions()
            if got < best:
                best, best_pos = got, dict(pos)
            if not best:
                break
        pos = best_pos
        for r in ranks:
            ranks[r].sort(key=lambda i: pos[i])

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
            if len(lst) <= cap:
                chunked.append((r, lst))
                continue
            # WRAP ON FAMILY BOUNDARIES. Slicing a wide rank every `cap` boxes cuts
            # through the middle of a parent's children, and a parent whose children
            # straddle two columns has to send wires into both -- which cross the other
            # column's wires on the way. In a 40-box tree three of thirteen parents were
            # cut like that, and they accounted for the crossings that ordering alone
            # could not remove. The rank is already grouped by parent (the sweeps above
            # see to that), so the groups are consecutive runs: pack whole runs into
            # columns, and only split a run that is bigger than a column on its own.
            runs = []
            for i in lst:
                key = tuple(sorted(fpred[i]))
                if runs and runs[-1][0] == key:
                    runs[-1][1].append(i)
                else:
                    runs.append((key, [i]))
            cur = []
            for _, run in runs:
                if len(run) > cap:         # one parent with more children than a column
                    if cur:
                        chunked.append((r, cur)); cur = []
                    for i in range(0, len(run), cap):
                        chunked.append((r, run[i:i + cap]))
                    continue
                if cur and len(cur) + len(run) > cap:
                    chunked.append((r, cur)); cur = []
                cur.extend(run)
            if cur:
                chunked.append((r, cur))
        nrow = max((len(c) for _, c in chunked), default=1)
        for col, (_, lst) in enumerate(chunked):
            off = (nrow - len(lst)) // 2
            for k, i in enumerate(lst):
                self.boxes[i].col = col
                self.boxes[i].row = off + k
        self.ncol, self.nrow = len(chunked), nrow

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
            if not e.src_port and e.shape != "tap":
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
        # Obstacles, INDEXED. The clearance test asks only about things near the segment
        # being tested, but it used to walk every box and every placed run for every
        # candidate path -- 2.9 million calls and 281 million abs() on a 21-box diagram,
        # which was 95% of the runtime. Bucketing by coordinate answers the same question
        # against a handful of candidates instead of all of them. BUCKET is coarse enough
        # that a segment need only look at its own bucket and its two neighbours.
        self._used_h, self._used_v = [], []
        self._uh_by_y, self._uv_by_x = {}, {}
        self._box_rects = [(b.x, b.y, b.right, b.bottom) for b in self.boxes.values()]
        self._box_by_y, self._box_by_x = {}, {}
        for r in self._box_rects:
            bx0, by0, bx1, by1 = r
            for band in range(int(by0 // BUCKET), int(by1 // BUCKET) + 1):
                self._box_by_y.setdefault(band, []).append(r)
            for band in range(int(bx0 // BUCKET), int(bx1 // BUCKET) + 1):
                self._box_by_x.setdefault(band, []).append(r)
        ends = {}
        for idx, (e, s, d, ss, ds) in enumerate(info):
            dp = d.port_point(e.dst_port) if e.dst_port else anchor[(idx, "d")]
            if e.shape == "tap":
                ends[idx] = (dp, dp)      # no source anchor; the router makes the stub
            else:
                ends[idx] = (s.port_point(e.src_port) if e.src_port else anchor[(idx, "s")],
                             dp)
        # Whoever routes first gets the good corridors, so routing ORDER is itself a
        # lever on crossings. Short hops go first by default (least freedom, most
        # deserve to be straight); _ripup() promotes a wire that is tangled where it
        # is, to let it pick a corridor before its rivals do.
        boost = getattr(self, "_boost", set())
        order = sorted(range(len(info)),
                       key=lambda i: (0 if i in boost else 1,
                                      abs(ends[i][0][0] - ends[i][1][0]) +
                                      abs(ends[i][0][1] - ends[i][1][1])))
        for idx in order:
            e, s, d, ss, ds = info[idx]
            sp, dp = ends[idx]
            if e.shape == "tap":
                # a stub into this box's own edge, pointing inwards. Long enough that the
                # arrowhead is entered from behind, which is the rule for every wire.
                off = {"T": (0, -TAP), "B": (0, TAP), "L": (-TAP, 0), "R": (TAP, 0)}[ds]
                e.pts = [(dp[0] + off[0], dp[1] + off[1]), dp]
            elif e.shape == "straight":
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
                                              _arrow_len(e.weight) + 2,
                                              _tw(e.label, FS_SMALL) if e.label else 0)
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
        best_n, best_hint = self._quality(), None
        hint, seen = None, set()
        for _ in range(rounds):                       # barycentre sweep
            if best_n == (0, 0):
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
            n = self._quality()
            if n < best_n:
                best_n, best_hint = n, dict(hint)

        # Local improvement. The sweep gets the broad ordering right; these moves fix
        # the knots it leaves. Reversing a whole side is included because a fan-out
        # attached in exactly the wrong sense is one move from correct and many
        # adjacent swaps from it.
        cur = dict(best_hint) if best_hint else {}
        for _ in range(3):
            if best_n == (0, 0):
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
                n = self._quality()
                if n < best_n:
                    best_n, best_hint, cur = n, dict(trial), trial
                    improved = True
                    order.reverse()
                for i in range(len(order) - 1):
                    trial = dict(cur)
                    a, b = order[i], order[i + 1]
                    trial[a], trial[b] = cur.get(b, i + 1), cur.get(a, i)
                    self._route(trial)
                    n = self._quality()
                    if n < best_n:
                        best_n, best_hint, cur = n, dict(trial), trial
                        order[i], order[i + 1] = b, a
                        improved = True
            if not improved:
                break

        self._route(best_hint)
        self._best_hint = best_hint
        return best_n

    # ---- crossing removal: the ladder -----------------------------------
    #
    # Crossings are attacked in order of how much they disturb the drawing, cheapest
    # first. Each rung can fix things the ones below it cannot:
    #
    #   1. change the order wires LEAVE a module        \_ _untangle()
    #   2. change the order wires ENTER a module        /
    #   3. move the modules themselves                    _move_modules()
    #   4. route an individual wire differently           _ortho_route() cost function
    #   5. re-route wires around each other               _ripup()
    #
    # Rung 4 is inside the router and always on: among legal paths it takes the one
    # with fewest crossings. The others are search, and each keeps only moves that
    # measurably help, so none of them can make a diagram worse.

    def _quality(self):
        """(hard-rule violations, crossings) — lower is better, violations dominate.

        Crossings are an aesthetic cost; a wire under a block or along another wire is
        a correctness one. Scoring the ladder on crossings ALONE let a module move buy
        two fewer crossings at the price of a wire hidden under a box, which is a bad
        trade and was caught by the self-test rather than by eye.
        """
        segs = [sg for e in self.edges for sg in zip(e.pts, e.pts[1:]) if sg[0] != sg[1]]
        under = sum(1 for sg in segs for b in self.boxes.values() if _seg_in_box(sg, b))
        along = sum(1 for i, a in enumerate(segs) for b in segs[i + 1:] if _stacked(a, b))
        return (under + along, self._crossings())

    def _state(self):
        return ({b.id: (b.col, b.row) for b in self.boxes.values()},
                dict(getattr(self, "_hint", {}) or {}), set(getattr(self, "_boost", set())))

    def _restore(self, st):
        cells, hint, boost = st
        for bid, (c, r) in cells.items():
            self.boxes[bid].col, self.boxes[bid].row = c, r
        self._hint, self._boost = dict(hint), set(boost)
        self._layout(); self._route(self._hint or None)

    def _score(self):
        self._layout(); self._route(getattr(self, "_hint", None) or None)
        return self._quality()

    def _move_modules(self, rounds=3):
        """Rung 3: move the modules, so the wires need not cross at all.

        Some crossings are not a routing problem and not an ordering problem -- they
        are a placement problem, and no amount of shuffling attachment points or
        re-routing will remove them. If two blocks are in the wrong rows relative to
        what they talk to, their wires must cross somewhere. That is why the busiest
        fan-out sample barely improved from reordering alone: its crossings were
        decided when the boxes were placed.

        Tries swapping pairs of boxes within a column, then moving a box to a
        different row, then to a neighbouring column. Keeps a move only if it
        measurably reduces crossings.
        """
        best = self._score()
        for _ in range(rounds):
            if best == (0, 0):
                break
            improved = False
            bycol = {}
            for b in self.boxes.values():
                bycol.setdefault(b.col, []).append(b)
            for col, mem in sorted(bycol.items()):
                mem.sort(key=lambda b: b.row)
                for i in range(len(mem)):
                    for j in range(i + 1, len(mem)):
                        a, c = mem[i], mem[j]
                        a.row, c.row = c.row, a.row
                        n = self._score()
                        if n < best:
                            best, improved = n, True
                        else:
                            a.row, c.row = c.row, a.row
            # a box in the wrong column costs more than one in the wrong row
            for b in list(self.boxes.values()):
                if best == (0, 0):
                    break
                c0, r0 = b.col, b.row
                taken = {(o.col, o.row) for o in self.boxes.values() if o is not b}
                for c in (c0 - 1, c0 + 1):
                    if c < 0 or c >= self.ncol:      # stay inside the grid
                        continue
                    free = [r for r in range(self.nrow) if (c, r) not in taken]
                    if not free:
                        continue
                    b.col, b.row = c, min(free, key=lambda r: abs(r - r0))
                    n = self._score()
                    if n < best:
                        best, improved = n, True
                        break
                    b.col, b.row = c0, r0
            if not improved:
                break
        return best

    def _ripup(self, rounds=2):
        """Rung 5: let a tangled wire re-route ahead of the ones it fights with.

        Routing is sequential, so the wire that goes first gets the clean corridor and
        later wires bend around it. When two wires cross, it is often because the one
        that would have gone straight was routed second. Rip up the wires involved in
        crossings and give them first pick.
        """
        best = self._score()
        self._boost = set(getattr(self, "_boost", set()))
        for _ in range(rounds):
            if best == (0, 0):
                break
            improved = False
            segs = [(sg, i) for i, e in enumerate(self.edges)
                    for sg in zip(e.pts, e.pts[1:]) if sg[0] != sg[1]]
            guilty = {}
            for i, (a, ea) in enumerate(segs):
                for b, eb in segs[i + 1:]:
                    if ea != eb and _crosses(a, b):
                        guilty[ea] = guilty.get(ea, 0) + 1
                        guilty[eb] = guilty.get(eb, 0) + 1
            for idx, _ in sorted(guilty.items(), key=lambda kv: -kv[1])[:8]:
                if idx in self._boost:
                    continue
                self._boost.add(idx)
                n = self._score()
                if n < best:
                    best, improved = n, True
                else:
                    self._boost.discard(idx)
            if not improved:
                break
        return best

    def _optimise(self):
        """Walk the ladder, keeping the best arrangement seen at every step."""
        self._hint, self._boost = None, set()
        self._layout()
        n = self._untangle()
        self._hint = getattr(self, "_best_hint", None)
        best, state = n, self._state()
        for step in (self._move_modules, self._untangle_keep, self._ripup):
            n = step()
            if n < best:
                best, state = n, self._state()
            else:
                self._restore(state)
        self._restore(state)
        return best

    def _untangle_keep(self):
        """_untangle() again, now that the modules have moved."""
        n = self._untangle()
        self._hint = getattr(self, "_best_hint", None)
        return n

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

    def _ortho_route(self, sp, dp, default, skip_ids, sign, need=0, label_w=0):
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
        box_by_y, box_by_x = self._box_by_y, self._box_by_x
        uh_by_y, uv_by_x = self._uh_by_y, self._uv_by_x
        B = BUCKET
        # The band loops below are written out rather than wrapped in a helper: this is
        # the innermost loop of the router, run millions of times per diagram, and even
        # building one small list per call cost more than the scan it replaced. A run
        # lives in exactly one band (a horizontal has one y, a vertical one x), so
        # sweeping a range of bands can never count the same wire twice.

        segc = {}

        def seg_cross(p, q):
            """Wires this ONE segment crosses, or None if the segment is illegal --
            passing under a box, or running along another wire.

            Cached per segment, which is the whole trick: the five-segment candidates
            below are a PRODUCT of tracks (ten by ten by ten), so the first run recurs a
            hundred times and the last run ten. Scanning each distinct segment once takes
            a wire from ~5000 segment scans to ~1200. Sound because the obstacle set does
            not change inside one call -- a wire is remembered only after its path is
            chosen.
            """
            got = segc.get((p, q))
            if got is not None:
                return None if got is False else got
            x1, y1 = p
            x2, y2 = q
            if x1 == x2 and y1 == y2:
                segc[(p, q)] = 0
                return 0
            cross = 0
            if y1 == y2:                                       # horizontal
                xa, xb = (x1, x2) if x1 < x2 else (x2, x1)
                by = int(y1 // B)
                for band in (by - 1, by, by + 1):
                    for bx0, by0, bx1, by1 in box_by_y.get(band, ()):
                        if xa < bx1 - 1 and xb > bx0 + 1 and by0 + 1 < y1 < by1 - 1:
                            segc[(p, q)] = False
                            return None
                    for yy, xx0, xx1 in uh_by_y.get(band, ()):
                        if -3 < yy - y1 < 3 and max(xa, xx0) < min(xb, xx1) - 2:
                            segc[(p, q)] = False
                            return None
                for band in range(int((xa - 1) // B), int((xb + 1) // B) + 1):
                    for xx, yy0, yy1 in uv_by_x.get(band, ()):
                        if xa - 1 < xx < xb + 1 and yy0 - 1 < y1 < yy1 + 1:
                            cross += 1
            else:                                              # vertical
                ya, yb = (y1, y2) if y1 < y2 else (y2, y1)
                bx = int(x1 // B)
                for band in (bx - 1, bx, bx + 1):
                    for bx0, by0, bx1, by1 in box_by_x.get(band, ()):
                        if ya < by1 - 1 and yb > by0 + 1 and bx0 + 1 < x1 < bx1 - 1:
                            segc[(p, q)] = False
                            return None
                    for xx, yy0, yy1 in uv_by_x.get(band, ()):
                        if -3 < xx - x1 < 3 and max(ya, yy0) < min(yb, yy1) - 2:
                            segc[(p, q)] = False
                            return None
                for band in range(int((ya - 1) // B), int((yb + 1) // B) + 1):
                    for yy, xx0, xx1 in uh_by_y.get(band, ()):
                        if ya - 1 < yy < yb + 1 and xx0 - 1 < x1 < xx1 + 1:
                            cross += 1
            segc[(p, q)] = cross
            return cross

        def ok_and_cost(pts, bound=None):
            """(crossings, label-homeless, length) for a legal path, else None.

            `label-homeless` is why a wire could end up unnamed. gap_x is sized so a
            label fits the WHOLE gap between two columns -- but a 3-segment route splits
            that gap into two half-runs, and a name that fits the gap fits neither half.
            Rather than double every gap and bloat the drawing, the router prefers, among
            paths that cross equally little, one that gives its own label somewhere to sit.

            Cheap facts first: length, label room, the arrowhead approach, the no-loop
            rule -- none of them need to know about the rest of the diagram. Only then the
            obstacle scan, and not at all for a path that cannot win: once a clean path is
            in hand a longer one is beaten whatever it crosses. Same winner, same drawing.
            """
            total = 0
            longest_h = 0
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                if x1 == x2 and y1 == y2:
                    continue
                total += abs(x2 - x1) + abs(y2 - y1)
                if y1 == y2:
                    lh = abs(x2 - x1)
                    if lh > longest_h:
                        longest_h = lh
            homeless = 1 if label_w and longest_h < label_w else 0
            if bound is not None and (0, 0, total) >= bound:
                return None
            # The wire must enter its arrowhead through the flat back, not through one of
            # the slanted sides: orient="auto" aims the head along the last segment, so if
            # that segment is shorter than the head is long, the corner before it lies
            # inside the triangle and the wire visibly joins the point from the side.
            (lx1, ly1), (lx2, ly2) = pts[-2], pts[-1]
            if abs(lx2 - lx1) + abs(ly2 - ly1) < need:
                return None
            # A wire must never loop. A path that revisits its own ground reads as two
            # wires, and following it means guessing at the junction which way the signal
            # went, so non-adjacent segments of one path must stay clear of each other.
            own = [sg for sg in zip(pts, pts[1:]) if sg[0] != sg[1]]
            for oi in range(len(own)):
                (ax1, ay1), (ax2, ay2) = own[oi]
                axlo, axhi = (ax1, ax2) if ax1 < ax2 else (ax2, ax1)
                aylo, ayhi = (ay1, ay2) if ay1 < ay2 else (ay2, ay1)
                for oj in range(oi + 2, len(own)):
                    (bx1, by1), (bx2, by2) = own[oj]
                    # Segments that are nowhere near each other cannot touch, and a box
                    # test says so in four comparisons -- against two exact tests that
                    # were being run 14 million times a diagram. The 3 px margin is the
                    # tolerance the exact tests use, so nothing near enough is skipped.
                    if (bx1 if bx1 < bx2 else bx2) > axhi + 3:
                        continue
                    if (bx1 if bx1 > bx2 else bx2) < axlo - 3:
                        continue
                    if (by1 if by1 < by2 else by2) > ayhi + 3:
                        continue
                    if (by1 if by1 > by2 else by2) < aylo - 3:
                        continue
                    if _crosses(own[oi], own[oj]) or _stacked(own[oi], own[oj]):
                        return None
            cross = 0
            for p, q in zip(pts, pts[1:]):
                c = seg_cross(p, q)
                if c is None:
                    return None
                cross += c
            return cross, homeless, total

        best = None
        vt = self._tracks(self._vgap_spans(), default)
        # The length of each candidate is arithmetic -- no need to enter the evaluator to
        # find out that a path is already too long to win.
        base_len = abs(dp[1] - sp[1])
        for mx in vt[:40]:
            bound = best[0][0] if best and best[0][0][:2] == (0, 0) else None
            if bound is not None and \
                    (0, 0, abs(mx - sp[0]) + base_len + abs(dp[0] - mx)) >= bound:
                continue
            if seg_cross(sp, (mx, sp[1])) is None:   # this track cannot be reached at all
                continue
            c = ok_and_cost([sp, (mx, sp[1]), (mx, dp[1]), dp], bound)
            if c and (best is None or (c, 2) < best[0]):
                best = ((c, 2), [sp, (mx, sp[1]), (mx, dp[1]), dp])
                if c[0] == 0 and c[1] == 0:
                    break                     # clean, and its name has a home
        if best is None or best[0][0][0] > 0:
            # Five segments: out to a track, along a horizontal band, in. Needed when
            # the target is several columns away -- the 3-segment form's last run has
            # to cross the intervening columns at the target's own height, which is
            # precisely where their boxes are.
            ht = self._tracks(self._hgap_spans(), (sp[1] + dp[1]) / 2)[:10]
            xt = self._tracks(self._vgap_spans(), dp[0])[:10]
            done = False
            # A candidate that shares an illegal segment with a rejected one is itself
            # rejected, so the shared runs are tested ONCE and whole branches of the
            # product are dropped: the way out of the source (per mx), the way into the
            # target (per ex), and the drop to the middle band (per mx, my). Same paths
            # considered, a fraction of the work -- this loop is ten tracks cubed.
            for mx in vt[:10]:
                l1 = abs(mx - sp[0])
                if seg_cross(sp, (mx, sp[1])) is None:
                    continue
                for ex in xt:
                    l3, l5 = abs(ex - mx), abs(dp[0] - ex)
                    if seg_cross((ex, dp[1]), dp) is None:
                        continue
                    for my in ht:
                        if seg_cross((mx, sp[1]), (mx, my)) is None:
                            continue
                        if seg_cross((ex, my), (ex, dp[1])) is None:
                            continue
                        bound = (best[0][0] if best and best[0][0][:2] == (0, 0)
                                 else None)
                        # a thousand candidates per wire here (ten tracks cubed), so this
                        # is where dropping the hopeless ones before the call pays
                        if bound is not None and (0, 0, l1 + abs(my - sp[1]) + l3
                                                  + abs(dp[1] - my) + l5) >= bound:
                            continue
                        pts = [sp, (mx, sp[1]), (mx, my), (ex, my), (ex, dp[1]), dp]
                        c = ok_and_cost(pts, bound)
                        if c and (best is None or (c, 4) < best[0]):
                            best = ((c, 4), pts)
                            if c[0] == 0 and c[1] == 0:
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
            if all(not (-3 < yy - y < 3 and max(min(a[0], b[0]), xx0) < min(max(a[0], b[0]), xx1) - 2)
                   for a, b in [(pts[0], pts[1]), (pts[2], pts[3])]
                   for y in [a[1]]
                   for band in (int(y // B) - 1, int(y // B), int(y // B) + 1)
                   for yy, xx0, xx1 in uh_by_y.get(band, ())):
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
                run = (y1, min(x1, x2), max(x1, x2))
                self._used_h.append(run)
                self._uh_by_y.setdefault(int(y1 // BUCKET), []).append(run)
            else:
                run = (x1, min(y1, y2), max(y1, y2))
                self._used_v.append(run)
                self._uv_by_x.setdefault(int(x1 // BUCKET), []).append(run)

    @staticmethod
    def _auto_sides(s, d):
        """Outputs leave on the RIGHT, inputs arrive on the LEFT. Always.

        This is the convention every hardware engineer reads a block diagram by, and it
        holds even when the target sits to the LEFT -- a feedback path leaves the right
        edge, goes round, and comes back into the left edge. Picking the nearest pair of
        sides instead produced drawings where a block's outputs emerged from its top,
        its left and its bottom depending on who happened to be where, so nothing could
        be told about a block by looking at it.

        Set src_side/dst_side explicitly for a bidirectional link, which may attach to
        either side.
        """
        return "R", "L"

    def _anchor(self, b, side):
        return {"L": (b.x, b.cy), "R": (b.right, b.cy),
                "T": (b.cx, b.y), "B": (b.cx, b.bottom)}[side]

    # ---- emit ------------------------------------------------------------
    def _svg(self):
        self._optimise()
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
        combos = sorted({(e.weight, e.color) for e in self.edges})
        defs = ['<defs>']
        for w, col in combos:
            base = max(ARROW + 1.4 * w, 2.6 * w)      # across the wire
            length = base * 1.15                      # along it
            defs.append(
                f'<marker id="arr{_wkey(w)}{_ckey(col)}" markerUnits="userSpaceOnUse" '
                f'markerWidth="{length:.1f}" markerHeight="{base:.1f}" '
                f'refX="{length:.1f}" refY="{base/2:.1f}" orient="auto">'
                f'<path d="M0,0 L{length:.1f},{base/2:.1f} L0,{base:.1f} Z" '
                f'fill="{col}"/></marker>')
        defs.append('</defs>')
        o.append("".join(defs))
        o.append(f'<text x="{self.margin}" y="22" fill="{BLUE}" font-size="15" '
                 f'font-weight="bold">{_esc(self.title)}</text>')
        # Legend, only when colour is actually carrying information. A key for one
        # colour is noise; a diagram with several needs one or the colours are a puzzle.
        kinds = [k for k in WIRE_KINDS if any(e.kind == k for e in self.edges)]
        if len(kinds) > 1:
            lx = self.W - self.margin
            for k in reversed(kinds):
                tw = _tw(k, FS_SMALL)
                lx -= tw
                o.append(f'<text x="{lx:.0f}" y="22" fill="{GREY}" '
                         f'font-size="{FS_SMALL}">{k}</text>')
                lx -= 6
                o.append(f'<line x1="{lx - 14:.0f}" y1="18" x2="{lx:.0f}" y2="18" '
                         f'stroke="{WIRE_KINDS[k]}" stroke-width="3"/>')
                lx -= 22
        for e in self.edges:
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in e.pts)
            # A tap's far end is not on a box, so the picture has to say where it comes
            # from -- otherwise reading the SVG back reports it as a wire ending in space,
            # which is exactly the failure verify_diagram exists to catch. The source
            # travels with the stub.
            frm = f' data-tap="{_esc(e.src)}"' if e.shape == "tap" else ""
            o.append(f'<polyline points="{pts}" fill="none" stroke="{e.color}" '
                     f'stroke-width="{e.weight:.1f}"{frm} '
                     f'marker-end="url(#arr{_wkey(e.weight)}{_ckey(e.color)})"/>')
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

    def _label_spots_dry(self, e):
        """Could this wire's name be placed at all? Does not consume a slot."""
        keep = list(self._lbl_boxes) if hasattr(self, "_lbl_boxes") else []
        self._lbl_boxes = list(keep)
        got = self._label_spots(e, 1)
        self._lbl_boxes = keep
        return bool(got)

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
            self._optimise()
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
        # Rules the ROUTER enforces are checked again HERE, on the finished geometry.
        # A rule enforced only where it is generated is a rule that regresses silently:
        # the router simply stops producing the good case and nothing says so. Checking
        # the output closes that loop, and costs one pass over the segments.
        for e in self.edges:
            own = [sg for sg in zip(e.pts, e.pts[1:]) if sg[0] != sg[1]]
            for i in range(len(own)):
                for j in range(i + 2, len(own)):
                    if _crosses(own[i], own[j]) or _stacked(own[i], own[j]):
                        rep.append(("FAIL", f"wire loops over itself: {e.src}->{e.dst} "
                                    f"— fix: give it an explicit src_side/dst_side, or "
                                    f"move one of the two boxes"))
                        break
            if len(e.pts) >= 2 and e.shape != "straight":
                if _seg_len((e.pts[-2], e.pts[-1])) < _arrow_len(e.weight):
                    rep.append(("FAIL", f"arrowhead entered from the side: "
                                f"{e.src}->{e.dst} — its final run is shorter than the "
                                f"head, so the corner falls inside the triangle"))
                sb, db = self.boxes[e.src], self.boxes[e.dst]
                if not e.src_side and not e.src_port and abs(e.pts[0][0] - sb.right) > 0.5:
                    rep.append(("WARN", f"output not leaving the right edge: "
                                f"{e.src}->{e.dst}"))
                if not e.dst_side and not e.dst_port and abs(e.pts[-1][0] - db.x) > 0.5:
                    rep.append(("WARN", f"input not arriving on the left edge: "
                                f"{e.src}->{e.dst}"))
        # a wire label that cannot be read is a wire without a name
        for e in self.edges:
            if e.label and not self._label_spots_dry(e):
                rep.append(("WARN", f"no legible place for the name of "
                            f"{e.src}->{e.dst} ({e.label!r}) — the wire is drawn "
                            f"unnamed; fewer boxes or more spacing would fix it"))
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


def _ckey(col):
    return col.replace('#', '')


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
    d11._route(); before = d11._quality()
    check("untangle never makes crossings worse", d11._untangle() <= before)

    check("crowded: no wire loops back over itself",
          not any(_crosses(a, b) or _stacked(a, b)
                  for e in d10.edges
                  for own in [[sg for sg in zip(e.pts, e.pts[1:]) if sg[0] != sg[1]]]
                  for i, a in enumerate(own) for b in own[i + 2:]))
    check("flow convention: outputs leave right, inputs arrive left",
          all(abs(e.pts[0][0] - d10.boxes[e.src].right) < 0.01 and
              abs(e.pts[-1][0] - d10.boxes[e.dst].x) < 0.01 for e in d10.edges))
    d12 = Diagram("kinds")
    d12.node("x", "X"); d12.node("y", "Y")
    d12.edge("x", "y", label="d [64]", kind="data")
    d12.edge("x", "y", label="irq [1]", kind="interrupt")
    svg12 = d12._svg()
    check("wire kinds get distinct colours + a legend",
          WIRE_KINDS["interrupt"] in svg12 and WIRE_KINDS["data"] in svg12
          and svg12.count('y1="18"') == 2)

    check("crowded: most wires still carry their name",
          len(lbls) >= int(0.8 * len(d10.edges)))

    check("weights: signal is 1px", 'stroke-width="1.0"' in svg)
    check("weights: 3 distinct stroke widths",
          all(f'stroke-width="{WEIGHTS[w]:.1f}"' in svg for w in ("signal", "bus", "fat")))
    check("weights: arrowhead does not scale with stroke (userSpaceOnUse)",
          'markerUnits="userSpaceOnUse"' in svg)


    # ---- cycles, sibling order, rails -------------------------------------------
    # A feedback wire must not reverse the flow. Ranking through the loop put exec at
    # column 0, LEFT of fetch, because the longest path ran round the cycle.
    d13 = Diagram("pipeline with feedback")
    for nm in ("fetch", "decode", "exec", "wb"):
        d13.node(nm, nm.title())
    d13.edge("fetch", "decode", label="iq [32]")
    d13.edge("decode", "exec", label="uop [64]")
    d13.edge("exec", "wb", label="res [64]")
    d13.edge("exec", "fetch", label="redirect [1]", kind="control")
    d13.edge("wb", "decode", label="stall [1]", kind="control")
    d13.lint()
    cols13 = [d13.boxes[n].col for n in ("fetch", "decode", "exec", "wb")]
    check("cycles: a feedback path does not reverse the flow", cols13 == sorted(cols13))
    check("cycles: the back edges are the ones identified",
          d13._back_edges == {("exec", "fetch"), ("wb", "decode")})

    # A containment tree is drawable with no crossings at any depth, which needs each
    # parent's children kept together in their rank -- and kept together they also
    # survive the wrapping of a wide rank into columns.
    # (two levels, 13 boxes: the invariant shows at any depth, and the gate has to
    # stay quick -- the optimiser takes minutes on a forty-box tree)
    d14 = Diagram("two-level tree")
    d14.node("root", "Root", kind="emphasis")
    q, n = ["root"], 0
    for lvl in range(2):
        nxt = []
        for parent in q:
            for k in range(3):
                n += 1
                bid = f"t{n}"
                d14.node(bid, f"B{lvl}.{k}")
                d14.edge(parent, bid, label="tl [64]", weight="bus")
                nxt.append(bid)
        q = nxt
    d14.lint()
    kids14 = {}
    for e in d14.edges:
        kids14.setdefault(e.src, []).append((d14.boxes[e.dst].col, d14.boxes[e.dst].row))
    check("tree: each parent's children stay together",
          all(len({c for c, _ in v}) == 1 and
              sorted(r for _, r in v) == list(range(min(r for _, r in v),
                                                   min(r for _, r in v) + len(v)))
              for v in kids14.values()))

    # A global signal is a tap per block, not a wire per block.
    d15 = Diagram("clock rail")
    d15.node("top", "Top", kind="emphasis")
    d15.node("clk", "ClockSource", kind="emphasis")
    d15.edge("top", "clk", label="tl [64]")
    tgt = []
    for i in range(8):
        d15.node(f"b{i}", f"Blk{i}")
        d15.edge("top", f"b{i}", label="tl [64]", weight="bus")
        tgt.append(f"b{i}")
    d15.rail("clk", tgt, label="clk/rst", kind="clock")
    rep15 = d15.lint()
    taps = [e for e in d15.edges if e.shape == "tap"]
    check("rail: one tap per block", len(taps) == 8)
    check("rail: each tap is a stub on its own block, entered from behind",
          all(len(e.pts) == 2 and abs(e.pts[0][1] - e.pts[1][1]) == TAP for e in taps))
    check("rail: taps introduce no FAIL",
          not any(l == "FAIL" for l, _ in rep15))
    check("rail: taps do not drive placement", d15.boxes["clk"].col <= d15.boxes["b0"].col)

    print(f"\n{'ALL PASS' if not fails else str(fails)+' FAILED'}")
    return fails


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(__doc__)
