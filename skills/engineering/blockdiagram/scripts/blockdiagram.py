#!/usr/bin/env python3
"""blockdiagram — a small Python DSL for spec block diagrams.

Design choices (enforced, not re-derived each time):
  * Explicit GRID placement: boxes go in (col,row) cells with optional spans;
    the engine computes x/y/w/h so things align and never silently overlap.
  * Boxes auto-fit: sized to MEASURED text (PIL + an Arial-metric font), with a
    heuristic fallback if PIL/font are unavailable.
  * Edge-anchored ORTHOGONAL routing: arrows attach at computed box edges, run
    at right angles through column/row GAP corridors (not through boxes), and
    parallel runs sharing a corridor are pushed into separate lanes -- ORDERED BY
    TRAVEL DISTANCE, so a fan-out reads as a comb instead of crossing itself.
    A wire against the flow is taken around the outside in a reserved channel.
    This is not an autorouter — for awkward cases set src_side/dst_side (matching
    sides route around both boxes); the lint FAILs if a wire crosses a third box.
  * Small, proportional arrowheads.
  * Optional ports: declare named connection points on a side and attach edges
    to them ("box:port"); ports are drawn only when declared.
  * House palette: #1F4E79 emphasis, #cfe0f0 blocks, grey dashed = IP black box.

Quality gate: save() writes the SVG, renders a PNG with inkscape, and runs
geometric lint (box overlap, text overflow, wire-through-box, stacked parallel
segments, MEASURED wire crossings, label collisions, arrowhead size, glyphs
missing from the font). Crossings are counted by intersecting the routed wires,
not estimated from the grid -- the estimate scored a self-crossing fan-out zero.
Always eyeball the PNG.

Self-test:  python3 blockdiagram.py --selftest
"""
import copy, os, subprocess, sys

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
LANE = 8           # spacing between parallel runs in a corridor
FAN_MIN = 10       # smallest spacing between two wire-ends fanned out on one side
# Wire classes. A hierarchy diagram is unreadable in one colour once interrupts and
# clock/reset wiring are in it: they are not part of the datapath and should not have
# to be traced to be dismissed. Four is the whole vocabulary on purpose -- one more and
# the reader is consulting the legend instead of reading the picture.
EDGE_CLASSES = {"data": BLUE, "control": "#B8860B", "clock": "#6A5ACD",
                "interrupt": "#A03030"}
CLASS_ORDER = ("data", "control", "clock", "interrupt")
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
        # how many wire-ends this box must host on its vertical (L/R) and
        # horizontal (T/B) sides -- a side has to be long enough to fan them
        # out at readable spacing. Filled in by _layout().
        self.fan_lr = self.fan_tb = 0

    def need_w(self, text_only=False):
        widths = [_tw(self.label, FS_LABEL, bold=True)] + [_tw(d, FS_DESC) for d in self.desc]
        if self.title:
            widths.append(_tw(self.title, FS_LABEL, bold=True))
        text = max(widths, default=0) + 2 * PAD
        return text if text_only else max(text, self.fan_span(self.fan_tb))

    def need_h(self, text_only=False):
        n = (1 if self.label else 0) + len(self.desc) + (1 if self.title else 0)
        text = n * (FS_DESC + 6) + 2 * PAD + 6
        return text if text_only else max(text, self.fan_span(self.fan_lr))

    def fan_span(self, k):
        """Side length needed to fan k wire-ends out at readable spacing.

        The fan places end j at fraction (j+1)/(k+1) of the side, so spacing is
        side/(k+1). Below FAN_MIN the wires stop reading as separate wires -- the
        lint's own stacked-lines rule calls anything within 3 px one line -- so a
        hub with twenty children needs a side tall enough to carry twenty, not a
        50 px box with the fan crushed into it."""
        return (k + 1) * FAN_MIN if k > 1 else 0

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
    lpos = None                      # label position, chosen by Diagram._place_labels

    def __init__(self, src, dst, label, src_side, dst_side, shape, weight, cls="data"):
        self.src, self.src_port = self._split(src)
        self.dst, self.dst_port = self._split(dst)
        self.label = label
        self.src_side, self.dst_side = src_side, dst_side
        self.shape = shape          # "ortho" (default) | "straight" (diagonal ok)
        # weight = line thickness: "signal" | "bus" | "wide", or a number (px)
        self.weight = WEIGHTS.get(weight, weight) if isinstance(weight, str) else float(weight)
        self.cls = cls if cls in EDGE_CLASSES else "data"
        self.color = EDGE_CLASSES[self.cls]
        self.pts = []

    @staticmethod
    def _split(ref):
        return tuple(ref.split(":", 1)) if ":" in ref else (ref, None)


class Diagram:
    def __init__(self, title, cols=None, rows=None, gap_x=46, gap_y=26,
                 margin=24, title_h=34):
        self.title = title
        self.ncol, self.nrow = cols, rows
        self.gap_x, self.gap_y, self.margin, self.title_h = gap_x, gap_y, margin, title_h
        self.boxes = {}
        self.edges = []
        self._order = []
        self._autoplaced = False

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
             shape="ortho", weight="signal", cls="data"):
        """cls = "data" | "control" | "clock" | "interrupt" -- colours the wire and puts
        that class in the legend. Unknown values fall back to "data" rather than
        inventing a colour."""
        self.edges.append(_Edge(src, dst, label, src_side, dst_side, shape, weight, cls))

    # ---- autoplace (layered, aesthetic-tuned) ----------------------------
    def autoplace(self):
        """Assign (col,row) from connectivity. Layered left-to-right flow with
        median-barycenter row ordering and vertically-centred columns, then two
        measured passes: try a couple of alternative rankings and keep the one that
        actually draws better, and swap neighbouring boxes while that keeps helping.
        A bounded heuristic seed, not a placer/router — override any box's col/row
        to hand-tune."""
        ids = [b.id for b in self.boxes.values()]
        succ = {i: [] for i in ids}
        pred = {i: [] for i in ids}
        for e in self.edges:
            if e.src in succ and e.dst in pred and e.src != e.dst:
                succ[e.src].append(e.dst)
                pred[e.dst].append(e.src)
        self._back_edges = back = self._find_back_edges(succ)

        # Candidate rankings, cheapest-first, scored on the drawn result. The first is
        # the plain layering; the others move a "bypassed hub" to the right of the boxes
        # it feeds. That case is common and reads badly: when a second hub sits in the
        # middle of a first hub's fan, every wire that skips past it has to share a
        # corridor with the second hub's own fan, and they cross. Ranked to the right
        # instead, it fans back leftward and the two combs never meet.
        cands = [self._rank(ids, succ, pred, back)]
        for n in self._bypassed_hubs(ids, succ, pred, back):
            b2 = back | {(n, t) for t in succ[n]}
            r = self._rank(ids, succ, pred, b2)
            r[n] = max(r[t] for t in succ[n]) + 1
            cands.append(r)

        best = None
        for rank in cands:
            self._place(rank, succ, pred)
            sc = self._score()
            if best is None or sc < best[0]:
                best = (sc, {i: (b.col, b.row) for i, b in self.boxes.items()},
                        self.ncol, self.nrow)
            if sc == (0, 0):
                break
        for i, (c, r) in best[1].items():
            self.boxes[i].col, self.boxes[i].row = c, r
        self.ncol, self.nrow = best[2], best[3]
        self._autoplaced = True
        self._reduce_crossings()

    @staticmethod
    def _find_back_edges(succ):
        """Break cycles before ranking. A feedback wire (redirect, stall, retry) is a
        fact of hardware, and ranking straight through one puts the loop's boxes in the
        wrong order -- a fetch/decode/exec/wb pipeline with a redirect came out with
        exec LEFT of fetch, because the longest path ran round the loop. Find the back
        edges with a depth-first walk, rank on the DAG that is left, and let the back
        edges be drawn as what they are: wires running against the flow."""
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

    @staticmethod
    def _bypassed_hubs(ids, succ, pred, back, limit=3):
        """Nodes that feed several boxes which are ALSO fed from further left.

        Those are the ones worth trying on the right-hand side of their children: the
        wires bypassing them are what collide with their own fan. Bounded to the few
        biggest, so the number of candidate rankings stays small."""
        out = []
        for i in ids:
            kids = [t for t in succ[i] if (i, t) not in back]
            if len(kids) < 2 or not pred[i]:
                continue
            others = sum(1 for k in kids if len([p for p in pred[k] if p != i]) > 0)
            if others >= 2:
                out.append((others, len(kids), i))
        out.sort(reverse=True)
        return [i for _, _, i in out[:limit]]

    def _rank(self, ids, succ, pred, back):
        """Longest-path layering over the forward edges, then tighten."""
        fwd = [e for e in self.edges if (e.src, e.dst) not in back and e.src != e.dst]
        rank = {i: 0 for i in ids}
        for _ in range(len(ids) + 1):
            changed = False
            for e in fwd:
                if e.src in rank and e.dst in rank and rank[e.dst] < rank[e.src] + 1:
                    rank[e.dst] = rank[e.src] + 1
                    changed = True
            if not changed:
                break
        # tighten: pull a pure source (no predecessors) rightward to just before
        # its nearest consumer, so its edges span one rank and don't cross boxes
        fpred = {i: [p for p in pred[i] if (p, i) not in back] for i in ids}
        fsucc = {i: [t for t in succ[i] if (i, t) not in back] for i in ids}
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
        return rank

    def _place(self, rank, succ, pred):
        """Order the rows inside each rank, then assign cells."""
        ranks = {}
        for i in rank:
            ranks.setdefault(rank[i], []).append(i)
        pos = {i: float(k) for r in ranks for k, i in enumerate(ranks[r])}

        # ordering sweeps: median barycenter over both neighbour sides
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

        # assign cells; centre each column vertically for balance. Columns are
        # re-indexed densely so empty ranks (left by tightening) don't appear.
        nrow = max((len(v) for v in ranks.values()), default=1)
        for col, r in enumerate(sorted(ranks)):
            lst = ranks[r]
            off = (nrow - len(lst)) // 2
            for k, i in enumerate(lst):
                self.boxes[i].col = col
                self.boxes[i].row = off + k
        self.ncol, self.nrow = len(ranks), nrow

    def _clear_skip_bands(self):
        """Give every column-skipping wire a clear row to travel along.

        An edge whose endpoints are more than one column apart runs horizontally at
        the destination's row all the way across the columns in between -- straight
        through whatever sits in that row, which is the lint's "wire crosses box".
        No routing trick fixes that: the wire needs a row that is empty in the columns
        it skips, which is a placement question, not a routing one. So move the
        destination to the nearest such row, adding one row if none exists -- the
        layered ordering leaves the rest of the placement alone, which is what a
        Sugiyama dummy node buys you.
        """
        def occupied(col, row):
            for b in self.boxes.values():
                if (b.col <= col < b.col + b.colspan and
                        b.row <= row < b.row + b.rowspan):
                    return b.id
            return None
        for e in self.edges:
            s, d = self.boxes.get(e.src), self.boxes.get(e.dst)
            if not s or not d or abs(d.col - s.col) < 2:
                continue
            between = range(min(s.col, d.col) + 1, max(s.col, d.col))

            def blocked(row):
                return any(occupied(c, row) for c in between)
            if not blocked(d.row):
                continue
            cands = sorted(range(self.nrow), key=lambda r: (abs(r - d.row), r))
            for r in cands + [self.nrow]:
                if occupied(d.col, r) not in (None, d.id) or blocked(r):
                    continue
                d.row = r
                self.nrow = max(self.nrow, r + 1)
                break

    def _crossings(self):
        """Count places where two routed wires actually cross.

        This used to be counted combinatorially -- pairs of edges running between the
        same two columns whose row order inverts -- which is blind to everything the
        eye actually sees. It reported ZERO for a twenty-wire fan-out carrying eighty
        crossings, because a fan out of one box shares a source column and so no pair
        ever "inverts". Crossings are geometry, so count geometry: intersect the routed
        polylines. Parallel/collinear overlap is not counted here -- that is the
        stacked-segments FAIL, a different defect with a different fix."""
        segs = [(e, (e.pts[k], e.pts[k + 1]))
                for e in self.edges for k in range(len(e.pts) - 1)]
        hits = set()
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                (ea, a), (eb, b) = segs[i], segs[j]
                if ea is eb:
                    continue
                x = _cross_point(a, b)
                if x:
                    hits.add((id(ea), id(eb), round(x[0], 1), round(x[1], 1)))
        return len(hits)

    def _crossings_by_edge(self):
        """How many crossings each wire is involved in -- which one to fix first."""
        segs = [(e, (e.pts[k], e.pts[k + 1]))
                for e in self.edges for k in range(len(e.pts) - 1)]
        hits, tally = set(), {id(e): 0 for e in self.edges}
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                (ea, a), (eb, b) = segs[i], segs[j]
                if ea is eb:
                    continue
                x = _cross_point(a, b)
                if not x:
                    continue
                key = (id(ea), id(eb), round(x[0], 1), round(x[1], 1))
                if key in hits:
                    continue
                hits.add(key)
                tally[id(ea)] += 1
                tally[id(eb)] += 1
        return tally

    def _repair_routes(self, limit=4, gain=2):
        """Send a wire around the outside when threading it through costs crossings.

        Some crossings are not an ordering problem and no lane order fixes them: two
        boxes in one column feeding an interleaved set of destinations, or a destination
        two columns out because a second hub feeds it too. The wire has to leave the
        corridors altogether -- out into the side margin, along the reserved band, and
        back in at the far side.

        Measured, and deliberately reluctant: a go-around is a long detour, so it is
        kept only when it removes at least `gain` crossings and introduces no fault. One
        such wire accounted for 26 of 29 crossings in a 53-box merged hierarchy."""
        best = self._crossings()
        for _ in range(limit):
            if not best:
                return
            tally = self._crossings_by_edge()
            cands = [e for e in self.edges
                     if not e.src_side and not e.dst_side and e.shape == "ortho"
                     and tally.get(id(e), 0) >= 2]
            if not cands:
                return
            cands.sort(key=lambda e: -tally[id(e)])
            moved = False
            for e in cands[:3]:
                s_b, d_b = self.boxes[e.src], self.boxes[e.dst]
                right = d_b.col > s_b.col
                for shape, a, b in (("around", "L" if right else "R", "R" if right else "L"),
                                    ("ortho", "B", "B"), ("ortho", "T", "T")):
                    e.shape, e.src_side, e.dst_side = shape, a, b
                    self._layout(); self._route()   # a channel needs reserved canvas
                    got = self._crossings()
                    ok = not any(l == "FAIL" for l, _ in self._geometry_faults())
                    if ok and got <= best - gain:
                        best, moved = got, True
                        break
                    e.shape, e.src_side, e.dst_side = "ortho", None, None
                    self._layout(); self._route()
                if moved:
                    break
            if not moved:
                return

    def _geometry_faults(self):
        """The three geometric FAILs: boxes on boxes, wires through boxes, wires on
        wires. Split out of lint() because a candidate placement has to be scored on
        exactly these before the engine will accept it -- see _reduce_crossings."""
        rep = []
        bs = list(self.boxes.values())
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                a, b = bs[i], bs[j]
                if a.x < b.right and b.x < a.right and a.y < b.bottom and b.y < a.bottom:
                    rep.append(("FAIL", f"box overlap: {a.id} ∩ {b.id}"))
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
                        sb, db = self.boxes[e.src], self.boxes[e.dst]
                        if sb.col == db.col:      # a bypass down a stack of boxes
                            how = ("take it around the outside: "
                                   "src_side=\"R\", dst_side=\"R\" (or both \"L\")")
                        elif sb.row == db.row:
                            how = ("take it around the outside: "
                                   "src_side=\"T\", dst_side=\"T\" (or both \"B\")")
                        else:
                            how = (f"place {e.src} and {e.dst} in adjacent cells, or move "
                                   f"{b.id} out of the corridor between them")
                        rep.append(("FAIL", f"wire {e.src}->{e.dst} crosses box {b.id} "
                                    f"— fix: {how}"))
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
        return rep

    def _score(self):
        """(faults, crossings) for the current placement, measured on a throwaway copy
        so scoring a candidate cannot leave anything behind in this diagram."""
        t = copy.deepcopy(self)
        t._layout(); t._route()
        return (len(t._geometry_faults()), t._crossings())

    def _reduce_crossings(self, passes=3):
        """Shuffle blocks: swap neighbours in a column while the picture gets better.

        Barycenter ordering minimises a COMBINATORIAL crossing estimate, which is not
        what a reader sees -- the wires are routed, and routed wires cross in places the
        estimate cannot know about. So finish by measuring: try each adjacent pair in
        each column, keep the swap only if the measured (faults, crossings) actually
        improves, and stop when a pass changes nothing. Bounded on purpose -- this is a
        few tens of measured swaps, not a placer."""
        best = self._score()
        if best == (0, 0) or len(self.boxes) > 60:
            return
        cols = {}
        for b in self.boxes.values():
            cols.setdefault(b.col, []).append(b)
        for _ in range(passes):
            improved = False
            for mem in cols.values():
                mem.sort(key=lambda b: b.row)
                for i in range(len(mem) - 1):
                    a, b = mem[i], mem[i + 1]
                    a.row, b.row = b.row, a.row
                    sc = self._score()
                    if sc < best:
                        best, improved = sc, True
                        mem[i], mem[i + 1] = b, a
                    else:
                        a.row, b.row = b.row, a.row
            if not improved or best == (0, 0):
                break

    # ---- layout ----------------------------------------------------------
    def _grid_sizes(self, text_only=False):
        col_w = [0] * self.ncol
        row_h = [0] * self.nrow
        for b in self.boxes.values():
            if b.colspan == 1:
                col_w[b.col] = max(col_w[b.col], b.need_w(text_only))
            if b.rowspan == 1:
                row_h[b.row] = max(row_h[b.row], b.need_h(text_only))
        return [max(w, 90) for w in col_w], [max(h, 50) for h in row_h]

    def _grow_spans(self, col_w, row_h):
        """Let a box with a wide fan span the free cells next to it.

        Growth is symmetric (down, up, down, ...) so the box stays centred on its
        original cell, and only into cells nothing else claims. A box that cannot
        grow far enough keeps rowspan==1 and falls back to inflating its row."""
        claim = {}
        for b in self.boxes.values():
            for c in range(b.col, b.col + b.colspan):
                for r in range(b.row, b.row + b.rowspan):
                    claim[(c, r)] = b.id
        for b in sorted(self.boxes.values(), key=lambda b: -b.fan_span(b.fan_lr)):
            for axis in ("row", "col"):
                need = b.fan_span(b.fan_lr if axis == "row" else b.fan_tb)
                sizes = row_h if axis == "row" else col_w
                gap = self.gap_y if axis == "row" else self.gap_x
                n = self.nrow if axis == "row" else self.ncol
                span = "rowspan" if axis == "row" else "colspan"
                lo = getattr(b, axis)
                cnt = getattr(b, span)
                if need <= sum(sizes[lo:lo + cnt]) + (cnt - 1) * gap:
                    continue
                cross = range(b.col, b.col + b.colspan) if axis == "row" else \
                        range(b.row, b.row + b.rowspan)

                def free(i):
                    if not 0 <= i < n:
                        return False
                    return all(claim.get((x, i) if axis == "row" else (i, x)) in (None, b.id)
                               for x in cross)
                for direction in (1, -1) * 8:
                    have = sum(sizes[lo:lo + cnt]) + (cnt - 1) * gap
                    if need <= have:
                        break
                    nxt = lo + cnt if direction > 0 else lo - 1
                    if not free(nxt):
                        continue
                    if direction < 0:
                        lo = nxt
                    cnt += 1
                setattr(b, axis, lo)
                setattr(b, span, cnt)
                for x in cross:
                    for i in range(lo, lo + cnt):
                        claim[(x, i) if axis == "row" else (i, x)] = b.id

    def _layout(self):
        if any(b.col is None for b in self.boxes.values()):
            self.autoplace()
        # widen horizontal gaps so an edge label sits over the wire without
        # bumping either box (a label needs room in the corridor between cols).
        max_lbl = max((_tw(e.label, FS_SMALL) for e in self.edges if e.label), default=0)
        if max_lbl:
            self.gap_x = max(self.gap_x, max_lbl + 16)
        # ...and wide enough for the lanes that have to turn in it. Every edge leaving a
        # column turns in the corridor on that side, one lane apart; if the corridor is
        # narrower than the bundle, the outer lanes land BEYOND the destination and the
        # wire doubles back on itself. Size the corridor to the busiest one.
        turns = {}
        for e in self.edges:
            s_b, d_b = self.boxes.get(e.src), self.boxes.get(e.dst)
            if s_b and d_b and s_b.col != d_b.col:
                turns[s_b.col] = turns.get(s_b.col, 0) + 1
        if turns:
            self.gap_x = max(self.gap_x, (max(turns.values()) + 1) * LANE + 8)
        # count each box's wire-ends per axis, so a side can be sized to fan them.
        # Axis is taken from the grid (different column -> routed L/R, same column
        # -> T/B), which is known before any geometry exists.
        for b in self.boxes.values():
            b.fan_lr = b.fan_tb = 0
        for e in self.edges:
            s_b, d_b = self.boxes.get(e.src), self.boxes.get(e.dst)
            if not s_b or not d_b:
                continue
            lr = s_b.col != d_b.col
            for b, side in ((s_b, e.src_side), (d_b, e.dst_side)):
                horiz = (side in ("L", "R")) if side else lr
                if horiz:
                    b.fan_lr += 1
                else:
                    b.fan_tb += 1
        # Size the grid from text first, then let a box that must host a wide fan
        # claim the EMPTY cells beside it rather than inflating its whole row: a hub
        # with twenty children needs 200 px of side, and inflating row 9 to 200 px
        # would stretch every unrelated box sharing that row to 200 px too.
        col_w, row_h = self._grid_sizes(text_only=True)
        self._grow_spans(col_w, row_h)
        if self._autoplaced:
            # Only for placements the engine chose. A hand-placed diagram keeps the
            # author's cells -- if a wire crosses a box there, the lint says so and
            # names the fix; silently moving someone's box would be worse.
            self._clear_skip_bands()   # after span growth: a grown box can re-block a band
        col_w, row_h = self._grid_sizes(text_only=False)

        # Reserve the return channels. A wire taken around the outside runs in a band
        # beyond the boxes, and if the canvas stops at the last box there is nowhere for
        # it to go -- the router then has to thread it back through the corridors and it
        # crosses the very wires it is answering. So make the room first, but only when
        # something actually needs it.
        chans = len(getattr(self, "_back_edges", ()) or ())
        for e in self.edges:
            if (e.src, e.dst) in getattr(self, "_back_edges", ()):
                continue
            s_b, d_b = self.boxes.get(e.src), self.boxes.get(e.dst)
            if e.src_side and e.src_side == e.dst_side:
                chans += 1
            elif s_b and d_b and abs(d_b.col - s_b.col) >= 2:
                chans += 1               # may yet be sent around; keep the room for it
        pad = (min(chans, 4) * LANE + self.gap_y) if chans else 0
        if chans:                              # side margins carry the vertical legs
            self.margin = max(self.margin, 2 * LANE + min(chans, 4) * LANE)

        def cx(c): return self.margin + sum(col_w[:c]) + c * self.gap_x
        def cy(r): return self.margin + self.title_h + pad + sum(row_h[:r]) + r * self.gap_y

        for b in self.boxes.values():
            b.x = cx(b.col); b.y = cy(b.row)
            b.w = sum(col_w[b.col:b.col + b.colspan]) + (b.colspan - 1) * self.gap_x
            b.h = sum(row_h[b.row:b.row + b.rowspan]) + (b.rowspan - 1) * self.gap_y
            b._resolve_ports()
        self._col_w, self._row_h = col_w, row_h
        self.W = cx(self.ncol) - self.gap_x + self.margin
        self.H = cy(self.nrow) - self.gap_y + self.margin + pad

    def _route(self):
        # pass 1: pick a side for each edge end
        info = []
        for e in self.edges:
            s, d = self.boxes[e.src], self.boxes[e.dst]
            ss = e.src_side or (s.ports[e.src_port][0] if e.src_port else None)
            ds = e.dst_side or (d.ports[e.dst_port][0] if e.dst_port else None)
            if ss is None or ds is None:
                a, b = self._auto_sides(s, d, e)
                ss = ss or a; ds = ds or b
            info.append((e, s, d, ss, ds))

        # pass 1b: two wires taken around the outside cannot share a channel bank if
        # their spans INTERLEAVE (each end inside the other) -- nesting them is
        # impossible, so whichever goes inside gets cut by the other's leg. One over the
        # top and one under the bottom, and neither is crossed at all.
        banks = {"T": [], "B": []}
        for idx, (e, s, d, ss, ds) in enumerate(info):
            if ss != ds or ss not in "TB":
                continue
            lo, hi = sorted((s.cx, d.cx))

            def interleaves(bank):
                for a, b in banks[bank]:
                    nested = (lo <= a and b <= hi) or (a <= lo and hi <= b)
                    if not (hi <= a or lo >= b or nested):
                        return True
                return False
            for cand in (ss, "B" if ss == "T" else "T"):
                if self._channel_clear(s, d, cand) and not interleaves(cand):
                    break
            else:
                cand = ss
            banks[cand].append((lo, hi))
            info[idx] = (e, s, d, cand, cand)

        # pass 2: group edge-ends sharing a (box, side) and FAN them out so they
        # leave at distinct points (a single edge stays centred / aligned).
        groups = {}
        for idx, (e, s, d, ss, ds) in enumerate(info):
            if not e.src_port:
                groups.setdefault((s.id, ss), []).append((idx, "s"))
            if not e.dst_port:
                groups.setdefault((d.id, ds), []).append((idx, "d"))
        anchor = {}
        for (bid, side), mem in groups.items():
            box = self.boxes[bid]

            def fan_key(m):
                """Order the wire-ends along a side so the bundle cannot self-cross.

                Wires ARRIVING at a side may come out of different corridors, and of
                two arriving from the same direction the one that travelled FARTHEST
                must take the OUTERMOST end: its long run passes over every nearer
                wire's turn, so it has to pass beyond them. Wires LEAVING a side all
                turn in the same corridor (the one next to their own box), so there
                distance says nothing and plain reading order is right. Ends from
                opposite directions keep to their own halves of the side."""
                idx, end = m
                ob = info[idx][2] if end == "s" else info[idx][1]
                oc, bc = (ob.cy, box.cy) if side in "LR" else (ob.cx, box.cx)
                dist = 0 if end == "s" else \
                    abs((ob.col - box.col) if side in "LR" else (ob.row - box.row))
                before = oc < bc
                return (0 if before else 1, dist if before else -dist, oc)
            mem.sort(key=fan_key)
            k = len(mem)
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
                else:                             # fan across the side
                    f = (j + 1) / (k + 1)
                    if side in "LR":
                        anchor[(idx, end)] = (box.x if side == "L" else box.right,
                                              box.y + f * box.h)
                    else:
                        anchor[(idx, end)] = (box.x + f * box.w,
                                              box.y if side == "T" else box.bottom)

        # pass 3: build orthogonal (or straight) point lists.
        #
        # LANE ORDER IS NOT ARBITRARY.  Every wire leaving a side turns in the gap
        # corridor, and the order those turns are stacked across the corridor decides
        # whether the bundle reads as a comb or as a plate of spaghetti.  Handing lanes
        # out in edge-declaration order self-crosses a fan-out O(n^2) times: a wire that
        # turns late (outer lane) but travels far cuts across the run-in of every wire
        # that stopped short.  The crossing-free order is by DESTINATION coordinate --
        # the farthest target turns first (innermost lane), the nearest turns last --
        # counted per direction out of each side, since those bundles splay opposite
        # ways.  Positions are then packed: a lane is reused only for a stretch of the
        # corridor nothing else runs along, and the two entry sides start half a lane
        # apart so wires entering one gap from opposite sides cannot land on one line.
        ends = {}
        for idx, (e, s, d, ss, ds) in enumerate(info):
            ends[idx] = (s.port_point(e.src_port) if e.src_port else anchor[(idx, "s")],
                         d.port_point(e.dst_port) if e.dst_port else anchor[(idx, "d")])
        lane_of = {}
        for axis, sides in (("v", "LR"), ("h", "TB")):
            k = 1 if axis == "v" else 0          # coordinate the corridor runs across
            j = 1 - k                            # coordinate the lanes are spread along
            buckets = {}
            for idx, (e, s, d, ss, ds) in enumerate(info):
                if e.shape == "straight" or ss not in sides or ds not in sides:
                    continue
                sp, dp = ends[idx]
                if abs(sp[k] - dp[k]) < 1:       # already aligned: straight shot, no lane
                    continue
                out = 1 if ss in "RB" else -1
                mid = sp[j] + out * (self.gap_x if axis == "v" else self.gap_y) / 2
                away = 1 if dp[k] > sp[k] else -1        # down/up (or right/left)
                buckets.setdefault(round(mid / LANE), {}) \
                       .setdefault((out, away), []).append((idx, mid))
            for bundles in buckets.values():     # one corridor's worth of turns
                # Every wire in the corridor gets its own lane, so the spacing has to fit
                # the WHOLE bundle across the corridor -- sized per direction it would
                # overshoot the far column and run into the boxes there.
                n = sum(len(m) for m in bundles.values())
                room = (self.gap_x if axis == "v" else self.gap_y) - 12
                step = min(LANE, max(2.0, room / max(n, 1)))
                taken = {}                       # lane x -> stretches already run on it
                # biggest bundle first, so the main comb gets the inner lanes and the
                # odd wire against the flow is the one pushed out
                for (out, away), mem in sorted(bundles.items(), key=lambda kv: -len(kv[1])):
                    # Farthest destination innermost -- but when two BOXES in the column
                    # share the corridor, the one further from the destinations has to
                    # pass the other, so all of its lanes go outside all of theirs.
                    # Otherwise its verticals cut the nearer box's own runs.
                    mem.sort(key=lambda m: (-away * ends[m[0]][0][k],
                                            -away * ends[m[0]][1][k]))
                    for rank_i, (idx, mid) in enumerate(mem):
                        # ONE LANE, ONE WIRE. Two wires can be given the same lane
                        # position when the stretches they run along do not overlap --
                        # and it looks wrong: collinear runs with a gap between them read
                        # as a single long wire with the other wires' turns crossing it.
                        # A lane is claimed outright, and the corridor was sized for the
                        # whole bundle, so there is room.
                        # Lane 0 sits one step off this wire's own box, and the comb
                        # grows across the corridor towards the far column. (Starting at
                        # the corridor CENTRE only left half the gap for the bundle.)
                        base = ends[idx][0][j] + out * step
                        extra = 0
                        while True:
                            x = base + (rank_i + extra) * step * out
                            if round(x, 1) not in taken:
                                break
                            extra += 1
                        taken[round(x, 1)] = idx
                        lane_of[idx] = x

        # Return channels (both ends leaving the same side) are stacked outside the
        # boxes, and the wire that reaches FURTHEST must ride the OUTERMOST one. Not the
        # longest wire -- the one that comes down furthest out: a wire dropping short of
        # another's far end has to drop on the inside of it, or the two cross. (Length
        # cannot decide it: two feedback wires a rank apart are almost exactly as long
        # as each other and still nest one inside the other.)
        around = {}
        for side in ("T", "B", "L", "R", "around"):
            run = 0 if side in ("T", "B", "around") else 1   # axis the channel runs along
            def in_channel(i):
                e, _s, _d, ss, ds = info[i]
                if side == "around":
                    return e.shape == "around"
                return e.shape != "around" and ss == ds == side
            mem = [i for i in range(len(info)) if in_channel(i)]

            def reach(i):
                """Signed reach: least-reaching wire first, so it lands on the inside."""
                sp, dp = ends[i]
                return (1 if dp[run] > sp[run] else -1) * dp[run]
            mem.sort(key=reach)
            for r, i in enumerate(mem):
                around[i] = LANE * r

        for idx, (e, s, d, ss, ds) in enumerate(info):
            sp, dp = ends[idx]
            if e.shape == "around":
                # Out of the figure and back in: away from the boxes into the side
                # margin, along the reserved band past everything, and in at the far
                # side. The margins and the top/bottom bands are the only space in the
                # picture guaranteed to hold no boxes, which is what makes this route
                # safe when a wire has to get past a whole column of them -- dropping
                # down "outside the two boxes" only works if nothing else shares their
                # column, and in a real hierarchy something always does.
                lane = around.get(idx, 0)
                near_bottom = (sp[1] + dp[1]) / 2 > self.H / 2
                y = self._outer_channel("B" if near_bottom else "T", lane)
                xs = (self.margin / 2 + lane) if ss == "L" else (self.W - self.margin / 2 - lane)
                xd = (self.margin / 2 + lane) if ds == "L" else (self.W - self.margin / 2 - lane)
                e.pts = [sp, (xs, sp[1]), (xs, y), (xd, y), (xd, dp[1]), dp]
            elif e.shape == "straight":
                e.pts = [sp, dp]
            elif ss in "LR" and ds in "LR":
                if ss == ds:                      # both leave the same way: go around
                    off = self.gap_x / 2 + around.get(idx, 0)
                    midx = (min(sp[0], dp[0]) - off) if ss == "L" else \
                           (max(sp[0], dp[0]) + off)
                    e.pts = [sp, (midx, sp[1]), (midx, dp[1]), dp]
                elif idx not in lane_of:
                    e.pts = [sp, dp]
                else:
                    midx = lane_of[idx]
                    e.pts = [sp, (midx, sp[1]), (midx, dp[1]), dp]
            elif ss in "TB" and ds in "TB":
                if ss == ds:
                    # both ends leave the same way (T/T or B/B): the crossbar has to run
                    # OUTSIDE both boxes. Averaging the two y values -- which is right
                    # for a T->B route passing between them -- would drag the wire back
                    # across the boxes it is meant to go around, which is the whole
                    # point of asking for the same side (a feedback wire over the top).
                    lane = around.get(idx, 0)
                    off = self.gap_y / 2 + lane
                    midy = (min(sp[1], dp[1]) - off) if ss == "T" else \
                           (max(sp[1], dp[1]) + off)
                    if not self._band_clear(s, d, (midy - 2, midy + 2)):
                        midy = self._outer_channel(ss, lane)
                    e.pts = [sp, (sp[0], midy), (dp[0], midy), dp]
                elif idx not in lane_of:
                    e.pts = [sp, dp]
                else:
                    midy = lane_of[idx]
                    e.pts = [sp, (sp[0], midy), (dp[0], midy), dp]
            else:
                midy = (sp[1] + dp[1]) / 2
                e.pts = [sp, (sp[0], midy), (dp[0], midy), dp]

    def _channel_clear(self, s, d, side):
        """Is the channel just outside these two boxes free of other boxes?"""
        if side == "T":
            edge = min(s.y, d.y) - self.gap_y / 2
            band = (edge - LANE * 2, edge + 1)
        else:
            edge = max(s.bottom, d.bottom) + self.gap_y / 2
            band = (edge - 1, edge + LANE * 2)
        if band[0] < self.margin / 2 or band[1] > self.H - self.margin / 2:
            return False
        return self._band_clear(s, d, band)

    def _band_clear(self, s, d, band):
        x0, x1 = min(s.x, d.x), max(s.right, d.right)
        for b in self.boxes.values():
            if b.id in (s.id, d.id):
                continue
            if b.x < x1 and x0 < b.right and b.y < band[1] and band[0] < b.bottom:
                return False
        return True

    def _outer_channel(self, side, lane):
        """The channel outside EVERY box, in the canvas margin reserved for it.

        The channel next to the two boxes is only usable when they sit at the edge of
        the figure. A wire crossing the whole diagram -- a hub two columns away, a
        feedback path over several ranks -- has to go outside all of it, or it is back
        to threading through the corridors it was trying to avoid."""
        if side == "T":
            return self.margin + self.title_h + LANE + lane
        return self.H - self.margin - LANE - lane

    def _auto_sides(self, s, d, e=None):
        # A wire running against the flow (a feedback path: redirect, stall, retry) is
        # better taken around the outside than threaded back through the corridors it
        # came down -- in there it has to cross the forward wires it is answering. Take
        # the channel above if it is clear, else the one below, else fall through to the
        # ordinary rules and let the lint report what is left.
        if e is not None and (e.src, e.dst) in getattr(self, "_back_edges", ()):
            for side in "TB":
                if self._channel_clear(s, d, side):
                    return side, side
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

    # ---- labels ----------------------------------------------------------
    def _place_labels(self):
        """Put every bus label ON its own wire, and not on top of another label.

        The old rule -- midpoint of (first point, last point), 4 px up -- put the text
        nowhere near an L-shaped route and stacked a fan-out's labels into one illegible
        block, because every wire in a fan shares roughly the same midpoint. A label
        belongs on a straight run of the wire it names, and the run to prefer is the one
        ARRIVING at the destination: that is where a reader asks "what comes in here",
        and in a fan those runs are a row apart instead of a lane apart. Candidates are
        tried widest-label-first (the hardest to fit gets first pick) and the first one
        that clears the boxes and the labels already placed wins."""
        placed = []
        self._label_unplaced = []
        obstacles = [(b.x, b.y, b.right, b.bottom) for b in self.boxes.values()]
        _, legend_box = self._legend()
        if legend_box:
            obstacles.append(legend_box)
        for e in self.edges:
            e.lpos = None
        for e in sorted((e for e in self.edges if e.label),
                        key=lambda e: -_tw(e.label, FS_SMALL)):
            w, h = _tw(e.label, FS_SMALL), FS_SMALL + 2
            runs = [(a, b) for a, b in zip(e.pts, e.pts[1:])
                    if abs(a[1] - b[1]) < 1 and abs(a[0] - b[0]) > 2]
            if not runs:                      # all-vertical route: fall back to the ends
                a, z = e.pts[0], e.pts[-1]
                runs = [(a, (z[0], a[1]))]
            arrival = runs[-1]                # the run that enters the destination
            runs.sort(key=lambda r: (r is not arrival, -abs(r[0][0] - r[1][0])))
            cands = []
            for (x1, y), (x2, _) in runs:
                lo, hi = sorted((x1, x2))
                for dy in (-4, h + 4):
                    # against the far end first: the corridor is already sized to the
                    # widest label, so a label backed up to the box edge fits in it --
                    # whereas one CENTRED on a short arrival run spills into the box.
                    cands.append((hi - 2, y + dy, "end"))
                    cands.append((lo + 2, y + dy, "start"))
                    for f in (0.5, 0.35, 0.65):
                        cands.append((lo + f * (hi - lo), y + dy, "middle"))

            def rect(c):
                cx, ty, anc = c
                x0 = cx - w if anc == "end" else (cx if anc == "start" else cx - w / 2)
                return (x0, ty - h, x0 + w, ty + 2)
            for c in cands:
                r = rect(c)
                if r[0] < self.margin / 2 or r[2] > self.W - self.margin / 2:
                    continue                  # would run off the canvas
                if not any(_rect_hit(r, o) for o in placed + obstacles):
                    e.lpos = c
                    placed.append(r)
                    break
            if e.lpos is None:                # nothing clear: keep the first candidate
                e.lpos = cands[0]
                placed.append(rect(cands[0]))
                self._label_unplaced.append(e)

    def _legend(self):
        """Swatch + name for each wire class actually used, top right on the title row.

        Only the classes present: a legend entry for a colour that is not in the picture
        is noise, and four fixed entries would be a lie on a diagram that is all data.
        It returns the rectangle it occupies as well, because the label placer has to
        treat it as something to keep off -- an unreadable legend is worse than none."""
        used = [c for c in CLASS_ORDER if any(e.cls == c for e in self.edges)]
        if len(used) < 2:                      # one colour needs no key
            return [], None
        sw, gap, pad = 14, 6, 16               # swatch, swatch-to-text, item-to-item
        items, total = [], 0
        for c in used:
            w = sw + gap + _tw(c, FS_SMALL)
            items.append((c, w))
            total += w + pad
        total -= pad
        x = self.W - self.margin - total
        y = 18
        title_w = _tw(self.title, 15, bold=True)
        if x < self.margin + title_w + pad:    # would sit on the title: drop a line
            y = self.title_h + 4
            x = max(self.margin, self.W - self.margin - total)
        out = []
        for c, w in items:
            out.append((c, x, y))
            x += w + pad
        return out, (self.W - self.margin - total - 4, y - FS_SMALL - 2,
                     self.W - self.margin, y + 4)

    def _build(self):
        """Lay out, route, place labels -- widening the layout until they fit.

        A label that cannot find a clear slot is not a label problem, it is a spacing
        problem: the corridors and row gaps are too tight for what this diagram has to
        say. So spread them and try again, a bounded number of times, rather than
        stacking text on text and leaving the reader to guess. Growth is geometric and
        capped: a diagram whose labels genuinely cannot coexist still renders, and the
        lint still says which ones collide."""
        best = None
        self._layout(); self._route()
        self._repair_routes()
        for _ in range(6):
            self._layout(); self._route(); self._place_labels()
            stuck = len(self._label_unplaced)
            if not stuck:
                return
            if best is not None and stuck >= best[0]:
                # more room did not help: the obstruction is something else (a wire
                # through a box, a label wider than the whole figure). Go back to the
                # tightest layout that did this well and let the lint name the real
                # problem, rather than inflating the canvas for nothing.
                self.gap_x, self.gap_y = best[1], best[2]
                self._layout(); self._route(); self._place_labels()
                return
            best = (stuck, self.gap_x, self.gap_y)
            self.gap_x += max(24, int(self.gap_x * 0.2))
            self.gap_y += 10

    # ---- emit ------------------------------------------------------------
    def _svg(self):
        self._build()
        o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.W:.0f}" '
             f'height="{self.H:.0f}" viewBox="0 0 {self.W:.0f} {self.H:.0f}" font-family="Arial">']
        # one arrowhead marker per distinct line weight; head scales modestly
        # with weight (markerUnits=userSpaceOnUse so it does NOT blow up with it)
        heads = sorted({(e.weight, e.color) for e in self.edges})
        defs = ['<defs>']
        for w, colr in heads:
            h = ARROW + w                      # modest growth, not linear-with-stroke
            defs.append(
                f'<marker id="{_hkey(w, colr)}" markerUnits="userSpaceOnUse" '
                f'markerWidth="{h:.1f}" markerHeight="{h:.1f}" refX="{h-1:.1f}" '
                f'refY="{h/2:.1f}" orient="auto">'
                f'<path d="M0,0 L{h-2:.1f},{h/2:.1f} L0,{h-2:.1f} Z" fill="{colr}"/></marker>')
        defs.append('</defs>')
        o.append("".join(defs))
        o.append(f'<text x="{self.margin}" y="22" fill="{BLUE}" font-size="15" '
                 f'font-weight="bold">{_esc(self.title)}</text>')
        for cls, lx, ly in self._legend()[0]:
            o.append(f'<line x1="{lx:.0f}" y1="{ly:.0f}" x2="{lx+14:.0f}" y2="{ly:.0f}" '
                     f'stroke="{EDGE_CLASSES[cls]}" stroke-width="3"/>')
            o.append(f'<text x="{lx+20:.0f}" y="{ly+3.5:.0f}" fill="{GREY}" '
                     f'font-size="{FS_SMALL}">{_esc(cls)}</text>')
        for e in self.edges:
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in e.pts)
            o.append(f'<polyline points="{pts}" fill="none" stroke="{e.color}" '
                     f'stroke-width="{e.weight:.1f}" '
                     f'marker-end="url(#{_hkey(e.weight, e.color)})"/>')
            if e.label:
                lx, ly, anc = e.lpos
                # painted halo-first, so a label that has to sit over a wire is still
                # readable instead of being cut in half by it
                o.append(f'<text x="{lx:.0f}" y="{ly:.0f}" fill="{GREY}" '
                         f'font-size="{FS_SMALL}" text-anchor="{anc}" stroke="#ffffff" '
                         f'stroke-width="3" stroke-linejoin="round" paint-order="stroke">'
                         f'{_esc(e.label)}</text>')
        for bid in self._order:
            o.append(self._box_svg(self.boxes[bid]))
        o.append("</svg>")
        return "\n".join(o)

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

    def lint(self):
        self._build()
        rep = list(self._geometry_faults())
        bs = list(self.boxes.values())
        # text overflow (measured)
        for b in bs:
            inner = b.w - 2 * PAD
            head = b.title or b.label
            if head and _tw(head, FS_LABEL, bold=True) > inner + 0.5:
                rep.append(("WARN", f"label overflow {b.id!r}: {head!r}"))
            for t in b.desc:
                if _tw(t, FS_DESC) > inner + 0.5:
                    rep.append(("WARN", f"text overflow {b.id!r}: {t!r}"))
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
        # labels that could not be placed clear of a box or another label. Counted,
        # because an unreadable label is as bad as a wrong wire and the eye is the only
        # other thing that checks it.
        rects = {}
        for e in self.edges:
            if e.label and e.lpos:
                w, h = _tw(e.label, FS_SMALL), FS_SMALL + 2
                cx, ty, anc = e.lpos
                x0 = cx - w if anc == "end" else (cx if anc == "start" else cx - w / 2)
                rects[id(e)] = (e, (x0, ty - h, x0 + w, ty + 2))
        items = list(rects.values())
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if _rect_hit(items[i][1], items[j][1]):
                    rep.append(("WARN", f"labels overlap: {items[i][0].label!r} and "
                                f"{items[j][0].label!r} — shorten one, or drop the "
                                f"duplicate edge that carries it"))
        for e, r in items:
            for b in bs:
                if _rect_hit(r, (b.x, b.y, b.right, b.bottom)):
                    rep.append(("WARN", f"label {e.label!r} sits on box {b.id} — "
                                f"widen the corridor (gap_x) or shorten the label"))
        # the legend must not land on a box or on the title
        lg, lbox = self._legend()
        if lbox:
            for b in bs:
                if _rect_hit(lbox, (b.x, b.y, b.right, b.bottom)):
                    rep.append(("WARN", f"legend sits on box {b.id} — shorten the title "
                                        f"or give the figure more width"))
            if lbox[0] < self.margin + _tw(self.title, 15, bold=True) + 8 and lbox[1] < 26:
                rep.append(("WARN", "legend runs into the title — shorten the title"))
        # aesthetic advisories (not failures)
        nx = self._crossings()
        if nx:
            rep.append(("WARN", f"{nx} wire crossing(s) — reorder rows (swap two boxes in a "
                                f"column) or split the fan across sides with src_side=/dst_side="))
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
def _rect_hit(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _cross_point(s1, s2):
    """Interior intersection of two segments, or None (parallel/touching -> None)."""
    (p, q), (r, t) = s1, s2
    d1x, d1y = q[0] - p[0], q[1] - p[1]
    d2x, d2y = t[0] - r[0], t[1] - r[1]
    den = d1x * d2y - d1y * d2x
    if abs(den) < 1e-9:
        return None
    a = ((r[0] - p[0]) * d2y - (r[1] - p[1]) * d2x) / den
    b = ((r[0] - p[0]) * d1y - (r[1] - p[1]) * d1x) / den
    e = 1e-6
    if e < a < 1 - e and e < b < 1 - e:
        return (p[0] + a * d1x, p[1] + a * d1y)
    return None


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


def _stacked(s1, s2):
    (ax1, ay1), (ax2, ay2) = s1
    (bx1, by1), (bx2, by2) = s2
    if abs(ay1 - ay2) < 1 and abs(by1 - by2) < 1 and abs(ay1 - by1) < 3:
        return max(min(ax1, ax2), min(bx1, bx2)) < min(max(ax1, ax2), max(bx1, bx2)) - 2
    if abs(ax1 - ax2) < 1 and abs(bx1 - bx2) < 1 and abs(ax1 - bx1) < 3:
        return max(min(ay1, ay2), min(by1, by2)) < min(max(ay1, ay2), max(by1, by2)) - 2
    return False


def _hkey(w, colr):
    """Marker id for one (weight, colour): an arrowhead has to match its own wire."""
    return "arr%s_%s" % (_wkey(w), colr.lstrip("#"))


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

    # wire-through-box: A (left) -> C (right) with B sitting between, forced straight
    d3 = Diagram("t", cols=3, rows=1)
    d3.box("a", 0, 0, "A"); d3.box("bmid", 1, 0, "B"); d3.box("c", 2, 0, "C")
    d3.edge("a", "c")            # auto LR straight along shared row -> crosses B
    check("wire-through-box detected",
          any("crosses box" in m for l, m in d3.lint() if l == "FAIL"))

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
    check("weights: signal is 1px", 'stroke-width="1.0"' in svg)
    check("weights: 3 distinct stroke widths",
          all(f'stroke-width="{WEIGHTS[w]:.1f}"' in svg for w in ("signal", "bus", "fat")))
    check("weights: arrowhead does not scale with stroke (userSpaceOnUse)",
          'markerUnits="userSpaceOnUse"' in svg)


    # ---- crossings, fans, channels, labels (0.3.0) ------------------------------
    # the crossing count must SEE a crossing: two edges deliberately routed across
    # each other (the old combinatorial estimate scored this zero)
    d11 = Diagram("t", cols=2, rows=2)
    d11.box("a", 0, 0, "A"); d11.box("b", 0, 1, "B")
    d11.box("c", 1, 0, "C"); d11.box("e", 1, 1, "E")
    # (straight, so the lane ordering cannot quietly route around it -- the point here
    # is that the counter SEES geometry; the old estimate scored this zero)
    d11.edge("a", "e", shape="straight"); d11.edge("b", "c", shape="straight")
    d11._build()
    check("crossings: an actual crossing is counted", d11._crossings() >= 1)
    d11b = Diagram("t", cols=2, rows=2)
    d11b.box("a", 0, 0, "A"); d11b.box("b", 0, 1, "B")
    d11b.box("c", 1, 0, "C"); d11b.box("e", 1, 1, "E")
    d11b.edge("a", "e"); d11b.edge("b", "c")
    d11b._build()
    check("crossings: routed lanes avoid the obvious swap", d11b._crossings() == 0)

    # a wide fan-out draws crossing-free, and its source side is sized to host it
    d12 = Diagram("fan")
    d12.node("hub", "Hub")
    for i in range(16):
        d12.node(f"k{i}", f"Kid{i}")
        d12.edge("hub", f"k{i}", label=f"bus{i} [64]", weight="bus")
    rep12 = d12.lint()
    check("fan-out of 16: no crossings", d12._crossings() == 0)
    check("fan-out of 16: no FAILs", not any(l == "FAIL" for l, _ in rep12))
    check("fan-out of 16: source side sized for the fan",
          d12.boxes["hub"].h >= 16 * FAN_MIN)

    # a wire skipping a column gets a clear row instead of crossing what sits there
    d13 = Diagram("skip")
    for i in range(4):
        d13.node(f"m{i}", f"Mid{i}")
        d13.edge("src", f"m{i}") if False else None
    d13.node("src", "Src"); d13.node("far", "Far")
    for i in range(4):
        d13.edge("src", f"m{i}")
    d13.edge("m0", "far"); d13.edge("src", "far")
    check("column-skipping wire: no FAIL, no crossing",
          not any(l == "FAIL" for l, _ in d13.lint()) and d13._crossings() == 0)

    # feedback: the loop is not allowed to reverse the flow, and the back wires are
    # taken around the outside without crossing the forward path
    d14 = Diagram("pipe")
    for n in ("f", "d", "x", "w"):
        d14.node(n, n.upper())
    d14.edge("f", "d"); d14.edge("d", "x"); d14.edge("x", "w")
    d14.edge("x", "f", label="redirect [1]"); d14.edge("w", "d", label="stall [1]")
    rep14 = d14.lint()
    check("feedback: flow still reads left to right",
          d14.boxes["f"].col < d14.boxes["d"].col < d14.boxes["x"].col < d14.boxes["w"].col)
    check("feedback: back wires cross nothing", d14._crossings() == 0)
    check("feedback: no FAILs", not any(l == "FAIL" for l, _ in rep14))

    # same-side routing goes AROUND both boxes, never back between them
    d15 = Diagram("t", cols=3, rows=1)
    d15.box("p", 0, 0, "P"); d15.box("q", 1, 0, "Q"); d15.box("r", 2, 0, "R")
    d15.edge("r", "p", src_side="T", dst_side="T")
    d15._build()
    ys = [y for _, y in d15.edges[0].pts]
    check("same-side route runs outside the boxes",
          min(ys) < min(b.y for b in d15.boxes.values()))
    check("same-side route crosses no box",
          not any("crosses box" in m for l, m in d15.lint() if l == "FAIL"))

    # labels: on their own wire, clear of boxes and of each other
    d16 = Diagram("labels")
    d16.node("s", "S")
    for i in range(6):
        d16.node(f"t{i}", f"T{i}")
        d16.edge("s", f"t{i}", label=f"a_rather_long_signal_name_{i} [128]", weight="bus")
    rep16 = d16.lint()
    check("labels: none overlap another label",
          not any(m.startswith("labels overlap") for l, m in rep16))
    check("labels: none sit on a box", not any("sits on box" in m for l, m in rep16))
    check("labels: each sits on its own wire",
          all(any(abs(y - e.lpos[1]) < FS_SMALL + 8 for _, y in e.pts)
              for e in d16.edges if e.label))

    # spreading: a tight hand-set gap is widened until the labels fit
    d17 = Diagram("tight", cols=2, rows=3, gap_x=10, gap_y=4)
    for r in range(3):
        d17.box(f"u{r}", 0, r, f"U{r}"); d17.box(f"v{r}", 1, r, f"V{r}")
        d17.edge(f"u{r}", f"v{r}", label=f"quite_a_long_bus_label_{r} [128]")
    d17._build()
    check("spread: corridors widened to fit the labels", d17.gap_x > 10)
    check("spread: labels all placed", not d17._label_unplaced)

    # a hand-placed diagram is never silently re-placed
    d18 = Diagram("manual", cols=3, rows=1)
    d18.box("h", 0, 0, "H"); d18.box("i", 1, 0, "I"); d18.box("j", 2, 0, "J")
    d18.edge("h", "j")
    d18._build()
    check("manual placement is left alone (lint reports instead)",
          d18.boxes["j"].row == 0 and d18.nrow == 1 and
          any("crosses box" in m for l, m in d18.lint() if l == "FAIL"))


    # ---- wire classes + legend (0.4.0) ------------------------------------------
    d19 = Diagram("classes", cols=2, rows=4)
    for r, c in enumerate(CLASS_ORDER):
        d19.box(f"x{r}", 0, r, c.upper()); d19.box(f"y{r}", 1, r, c)
        d19.edge(f"x{r}", f"y{r}", label=c, cls=c, weight="bus")
    svg19 = d19._svg()
    check("classes: each wire drawn in its own colour",
          all(f'stroke="{EDGE_CLASSES[c]}"' in svg19 for c in CLASS_ORDER))
    check("classes: arrowhead matches its wire's colour",
          all(f'fill="{EDGE_CLASSES[c]}"/></marker>' in svg19 for c in CLASS_ORDER))
    check("legend: one entry per class used",
          all(f'>{c}</text>' in svg19 for c in CLASS_ORDER))
    check("legend: no FAILs, nothing sitting on it",
          not any(l == "FAIL" for l, _ in d19.lint()) and
          not any("legend sits" in m for l, m in d19.lint()))

    d20 = Diagram("one class", cols=2, rows=1)
    d20.box("p", 0, 0, "P"); d20.box("q", 1, 0, "Q"); d20.edge("p", "q", cls="data")
    check("legend: omitted when only one class is used", ">data</text>" not in d20._svg())
    d21 = Diagram("t", cols=2, rows=1)
    d21.box("p", 0, 0, "P"); d21.box("q", 1, 0, "Q")
    d21.edge("p", "q", cls="not_a_class")
    check("classes: an unknown class falls back to data, no invented colour",
          d21.edges[0].cls == "data" and d21.edges[0].color == EDGE_CLASSES["data"])

    # one lane, one wire: no two wires may share a lane position in a corridor
    d22 = Diagram("lanes")
    d22.node("root", "Root")
    for i in range(9):
        d22.node(f"z{i}", f"Z{i}")
        d22.edge("root", f"z{i}", weight="bus")
    d22._build()
    turns = [round(e.pts[1][0], 1) for e in d22.edges if len(e.pts) > 2]
    check("lanes: every wire turns on its own line", len(set(turns)) == len(turns))


    # ---- around-the-outside routing (0.5.0) --------------------------------------
    # A wire whose destination is two columns out, with its own column full below it:
    # threading it through cuts every fan in between, and dropping "just outside the two
    # boxes" runs it through its own column. It has to go round the outside.
    d23 = Diagram("skip past a full column")
    d23.node("hubA", "HubA")
    d23.node("hubB", "HubB")
    for i in range(6):
        d23.node(f"m{i}", f"Mid{i}")
        d23.edge("hubA", f"m{i}", label=f"a{i} [64]", weight="bus")
    for i in range(4):
        d23.node(f"low{i}", f"Low{i}")
        d23.edge("hubA", f"low{i}", label=f"l{i} [64]")
    d23.node("far", "Far")
    d23.edge("m0", "far", label="chain [64]")
    d23.edge("hubB", "far", label="second parent [64]", weight="bus")
    for i in range(3):
        d23.edge("hubB", f"m{i+3}", label=f"b{i} [64]")
    rep23 = d23.lint()
    tally23 = d23._crossings_by_edge()
    skipper = [e for e in d23.edges if e.src == "hubB" and e.dst == "far"][0]
    # the wire that had to get past a whole column now crosses nothing itself, and the
    # figure as a whole is far below the 13 crossings the plain route produced. What is
    # left is two hubs in one column feeding an interleaved set of children -- no lane
    # order fixes that, and the lint reports it rather than pretending otherwise.
    check("around: the wire that skips a column crosses nothing itself",
          tally23.get(id(skipper), 0) == 0)
    check("around: and runs through no box",
          not any(l == "FAIL" for l, _ in rep23))
    check("around: the figure's crossings drop sharply", d23._crossings() <= 4)
    around = [e for e in d23.edges if e.shape == "around"]
    check("around: only the offenders were sent outside, not the whole diagram",
          1 <= len(around) <= 2)
    if around:
        e = around[0]
        inside = [b for b in d23.boxes.values()
                  if b.x < min(p[0] for p in e.pts) < b.right]
        check("around: its legs run in the margin, clear of every box", not inside)
    else:
        check("around: its legs run in the margin, clear of every box", True)

    print(f"\n{'ALL PASS' if not fails else str(fails)+' FAILED'}")
    return fails


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)
    print(__doc__)
