#!/usr/bin/env python3
"""Download OpenStreetMap data for every circuit in tracks.js.

For each track this writes data/tracks/<handle>.geojson with:
  * the circuit itself (an OSM `type=circuit` relation near the track, or else
    the `highway=raceway` ways), joined into as few lines as possible,
  * roads, water (lakes, rivers, sea), forest/wood and buildings within
    ~2.5 km of the circuit,
simplified and rounded so each file stays small.

Data comes from the Overpass API. Mirrors are tried in turn with back-off,
because the public servers are often busy. Raw responses are cached in
scripts/.cache so a re-run does not hit the servers again (delete it to
refresh).

Usage:  python3 scripts/fetch_osm.py [handle ...]
Only the Python standard library is needed.

Map data © OpenStreetMap contributors, available under the ODbL.
"""

import json
import math
import os
import re
import sys
import time
import hashlib
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "tracks")
CACHE_DIR = os.path.join(ROOT, "scripts", ".cache")

ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "trackline-globe-fetch/1.0 (static site build script)"

MARGIN_M = 2500          # surroundings kept within this distance of the circuit
BUILDING_M = 800         # buildings only this close (they dominate file size)
SEARCH_M = 3000          # radius around the tracks.js point to look for the circuit

# Per-track tweaks. `search` = circuit search radius (m), `prefer` = regex the
# circuit relation's name should match, `avoid` = regex for names to skip,
# `relation` = use this OSM relation id as the circuit, `ways` = Overpass
# selector for the circuit's ways (when it has no relation), `loop` = the exact
# lap as an ordered list of OSM way ids: `id` = whole way in its own direction,
# `-id` = whole way reversed, `[id, i, j]` = that way from node index i to j
# (j < i runs backwards). Use `loop` when the lap must match the TrackLine
# shirt print and no single relation describes it.
TRACK_OPTS = {
    "bathurst-t-shirt": {"prefer": r"panorama"},
    # the TT course is mapped as a motorcycle route relation, not a circuit
    "isle-of-man-1": {"relation": 188240},
    # the full 24h layout is a route=raceway relation; the type=circuit nearby is Bugatti
    "le-mans-t-shirt": {"relation": 2126739},
    # street circuit: only fragments are tagged raceway; the full lap is a route relation
    "long-beach-1": {"relation": 18052024},
    "monaco-t-shirt": {"avoid": r"e-?prix|formula e"},
    # As on the shirt: the 24h layout = Nordschleife (route relation 38566) plus the
    # GP-circuit section at the start/finish, joined via T13 and the
    # "Variante 24h-Rennen" into one continuous lap.
    "nurburgring-t-shirt": {
        "name": "Nürburgring 24h (Nordschleife + GP)",
        "loop": [
            41395647, 683006908, 245166664, 799394508, 799394509, 683061804, 799394510, 799394511,
            799394512, 799394513, 414785755, 414785756, 799394515, 799394514, 799394516, 799394517,
            799394518, 799394519, 799394520, 799394521, 799394522, 799394523, 799394524, 41395652,
            683303211, 683303210, 683303209, 683303208, 41792406, 1571450724, 799394494, 799394495,
            41395668, [41395670,0,1], 27852583, [1149161210,28,45], [1149161208,0,5], 31010602, [1149161208,76,80], 1443047846,
            1443047847, 1079809244, 820679447, 820679448, 820679446, 1113009623, 820679445, 820330155,
            820330154, 1113009624, 820330153, 820330152, 1079809245, 1443047845, 1443047844, 27852990,
            1149161211, [1149161210,0,6], 26543901, [41395670,10,13], 41395673, 1009142894, 1009142895, 799394496,
            799394497, 41395681, 41395684, 799394498, 799394499, 799394500, 799394501, 799394502,
            799394503, 799394504, 799394505, 799394506, 799394507, 683061813, 683061814,
        ],
    },
    "silesia-ring-t-shirt": {"avoid": r"serwisowa|eco-marathon"},     # pit lane, alternate layout
    # The kart track beside the GP circuit has several layouts and link roads; keep
    # only the lap printed on the shirt (outer loop with the spiral hairpin).
    "silverstone-kart-track-1": {
        "name": "Kart Silverstone",
        "loop": [
            1430385979, -1526452832, [1526452829,25,0], [1526452830,14,12], [1526452835,0,10],
            1526452836, 1430385980, -1526452831, 1526452833,
        ],
    },
}

ROAD_CLASSES = {
    "motorway": 1, "trunk": 1, "motorway_link": 2, "trunk_link": 2,
    "primary": 2, "primary_link": 3, "secondary": 3, "secondary_link": 3,
    "tertiary": 3, "tertiary_link": 4, "unclassified": 4, "residential": 4,
    "living_street": 4, "service": 5, "track": 5,
}
BIG_EXTENT_M = 12000     # above this extent, drop minor roads / tiny buildings


# ---------------------------------------------------------------- helpers ---

def parse_tracks():
    src = open(os.path.join(ROOT, "tracks.js"), encoding="utf-8").read()
    rows = re.findall(r'\[\s*"([^"]+)",\s*"([^"]+)",\s*(-?[\d.]+),\s*(-?[\d.]+),\s*"([^"]+)"', src)
    return [dict(name=n, country=c, lat=float(la), lon=float(lo), handle=h) for n, c, la, lo, h in rows]


def overpass(query, label, bbox=None):
    """Run an Overpass query (cached). With bbox=(s, w, n, e), a cached answer for
    the same label whose bbox covers this one is reused: the caller clips anyway."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha1(query.encode()).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"{label}-{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    if bbox:
        for name in sorted(os.listdir(CACHE_DIR)):
            side = os.path.join(CACHE_DIR, name)
            if name.startswith(label + "-") and name.endswith(".bbox"):
                cs, cw, cn, ce = json.load(open(side))
                if cs <= bbox[0] and cw <= bbox[1] and cn >= bbox[2] and ce >= bbox[3]:
                    print(f"    {label}: reusing cached {name[:-5]}")
                    with open(side[:-5], encoding="utf-8") as f:
                        return json.load(f)
    body = urllib.parse.urlencode({"data": query}).encode()
    delay = 10
    for attempt in range(40):
        url = ENDPOINTS[attempt % len(ENDPOINTS)]
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=240) as r:
                raw = r.read()
            data = json.loads(raw)
            if data.get("remark") and "error" in data["remark"].lower():
                raise RuntimeError(data["remark"][:200])
            with open(path, "wb") as f:
                f.write(raw)
            if bbox:
                with open(path + ".bbox", "w") as f:
                    json.dump(list(bbox), f)
            return data
        except Exception as e:  # noqa: BLE001 - any failure: try the next mirror
            msg = str(e).splitlines()[0][:120]
            print(f"    {label}: {urllib.parse.urlparse(url).netloc} failed ({msg}); retry in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 1.5, 120)
    raise SystemExit(f"Overpass query for {label} failed on every mirror")


class Local:
    """Equirectangular projection to metres around a reference point."""

    def __init__(self, lat, lon):
        self.lat0, self.lon0 = lat, lon
        self.kx = 111320.0 * math.cos(math.radians(lat))
        self.ky = 110540.0

    def fwd(self, lon, lat):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def inv(self, x, y):
        return (self.lon0 + x / self.kx, self.lat0 + y / self.ky)


def simplify(pts, tol):
    """Douglas-Peucker on a list of (x, y)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    t2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if best > t2:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def ring_area(pts):
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        s += x1 * y2 - x2 * y1
    return s / 2.0


def clip_ring(pts, box):
    """Sutherland-Hodgman clip of a ring against box = (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    edges = [
        (lambda p: p[0] >= x0, lambda p, q: (x0, p[1] + (q[1] - p[1]) * (x0 - p[0]) / (q[0] - p[0]))),
        (lambda p: p[0] <= x1, lambda p, q: (x1, p[1] + (q[1] - p[1]) * (x1 - p[0]) / (q[0] - p[0]))),
        (lambda p: p[1] >= y0, lambda p, q: (p[0] + (q[0] - p[0]) * (y0 - p[1]) / (q[1] - p[1]), y0)),
        (lambda p: p[1] <= y1, lambda p, q: (p[0] + (q[0] - p[0]) * (y1 - p[1]) / (q[1] - p[1]), y1)),
    ]
    out = pts
    for inside, cut in edges:
        if not out:
            break
        src, out = out, []
        prev = src[-1]
        for cur in src:
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
            prev = cur
    return out


def clip_line(pts, box):
    """Split a polyline into the pieces inside box (Liang-Barsky per segment)."""
    x0, y0, x1, y1 = box
    pieces, cur = [], []
    for p, q in zip(pts, pts[1:]):
        dx, dy = q[0] - p[0], q[1] - p[1]
        t0, t1, ok = 0.0, 1.0, True
        for pp, qq in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
            if pp == 0:
                if qq < 0:
                    ok = False
                    break
            else:
                r = qq / pp
                if pp < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
        if not ok or t0 > t1:
            if cur:
                pieces.append(cur)
                cur = []
            continue
        a = (p[0] + t0 * dx, p[1] + t0 * dy)
        b = (p[0] + t1 * dx, p[1] + t1 * dy)
        if not cur:
            cur = [a]
        cur.append(b)
        if t1 < 1.0:
            pieces.append(cur)
            cur = []
    if cur:
        pieces.append(cur)
    return [c for c in pieces if len(c) > 1]


def join_lines(lines):
    """Merge polylines that share endpoints (either direction)."""
    lines = [list(l) for l in lines if len(l) > 1]
    k = lambda p: (round(p[0], 7), round(p[1], 7))
    merged = True
    while merged:
        merged = False
        ends = {}
        for i, l in enumerate(lines):
            ends.setdefault(k(l[0]), []).append(i)
            ends.setdefault(k(l[-1]), []).append(i)
        used = set()
        out = []
        for i, l in enumerate(lines):
            if i in used:
                continue
            used.add(i)
            grew = True
            while grew and k(l[0]) != k(l[-1]):
                grew = False
                for end in (k(l[-1]), k(l[0])):
                    for j in ends.get(end, []):
                        if j in used:
                            continue
                        m = lines[j]
                        at_tail = end == k(l[-1])
                        if at_tail:
                            l = l + (m[1:] if k(m[0]) == end else m[::-1][1:])
                        else:
                            l = (m if k(m[-1]) == end else m[::-1])[:-1] + l
                        used.add(j)
                        grew = merged = True
                        break
                    if grew:
                        break
            out.append(l)
        lines = out
    return lines


def tidy_circuit(lines, loc, snap_m=40.0, loop_m=600.0):
    """Drop tiny closed loops (roundabouts in route relations) and join pieces
    whose ends are within snap_m of each other."""
    def length(l):
        p = [loc.fwd(*c) for c in l]
        return sum(math.dist(a, b) for a, b in zip(p, p[1:]))
    longest = max(length(l) for l in lines)
    lines = [l for l in lines if not (l[0] == l[-1] and length(l) < min(loop_m, longest * 0.5))]
    d = lambda a, b: math.dist(loc.fwd(*a), loc.fwd(*b))
    changed = True
    while changed and len(lines) > 1:
        changed = False
        for i in range(len(lines)):
            for j in range(len(lines)):
                if i == j:
                    continue
                a, b = lines[i], lines[j]
                for bb in (b, b[::-1]):
                    if d(a[-1], bb[0]) < snap_m:
                        lines[i] = a + bb[1:]
                        del lines[j]
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
    # close a loop whose two ends nearly meet
    lines = [l + [l[0]] if len(l) > 3 and l[0] != l[-1] and d(l[0], l[-1]) < snap_m else l for l in lines]
    # drop short leftovers (layout connectors, pit entries) so only the main lap is drawn
    longest = max(length(l) for l in lines)
    return [l for l in lines if length(l) >= 0.15 * longest]


# ------------------------------------------------------------- coastline ---

def sea_polygons(chains, box):
    """Build sea rings from coastline chains (land is on the left of the way)."""
    x0, y0, x1, y1 = box
    W, H = x1 - x0, y1 - y0

    def perim(p):  # clockwise position around the box, starting at the NW corner
        x, y = p
        e = 1e-6 * max(W, H)
        if abs(y - y1) < e:
            return (x - x0) / W
        if abs(x - x1) < e:
            return 1 + (y1 - y) / H
        if abs(y - y0) < e:
            return 2 + (x1 - x) / W
        return 3 + (y - y0) / H

    corners = [(1, (x1, y1)), (2, (x1, y0)), (3, (x0, y0)), (4, (x0, y1))]
    pieces, islands = [], []
    for c in chains:
        closed = c[0] == c[-1]
        inside = all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in c)
        if closed and inside:
            islands.append(c[:-1])
            continue
        pieces += clip_line(c, box)
    rings = []
    if pieces:
        starts = sorted(((perim(p[0]), i) for i, p in enumerate(pieces)))
        left = set(range(len(pieces)))
        while left:
            first = min(left)
            ring, cur = [], first
            for _ in range(len(pieces) + 1):
                left.discard(cur)
                ring += pieces[cur]
                t = perim(pieces[cur][-1])
                # walk clockwise to the next entry point
                nxt = None
                for s, i in starts:
                    if s >= t:
                        nxt = (s, i)
                        break
                if nxt is None:
                    nxt = (starts[0][0] + 4, starts[0][1])
                s = nxt[0]
                for c_t, c_p in corners + [(c + 4, p) for c, p in corners]:
                    if t < c_t < s:
                        ring.append(c_p)
                cur = nxt[1]
                if cur == first or cur not in left:
                    break
            if len(ring) > 2:
                rings.append(ring)
        rings += islands
    elif islands:
        rings = [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]] + islands
    return rings


def osm_api_ways(ids, label):
    """Node geometry of the given way ids, from the main OSM API (id lookups are
    cheap there, and it is more reliable than the public Overpass mirrors).
    Returns {way_id: [(lon, lat), ...]} in each way's node order. Cached."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    ids = sorted(set(ids))
    key = hashlib.sha1(",".join(map(str, ids)).encode()).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"{label}-{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return {int(k): [tuple(c) for c in v] for k, v in json.load(f).items()}

    def get(url):
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read())
            except Exception as e:  # noqa: BLE001
                print(f"    {label}: OSM API failed ({str(e)[:80]}); retry", flush=True)
                time.sleep(5 * (attempt + 1))
        raise SystemExit(f"OSM API lookup for {label} failed")

    way_nodes = {}
    for i in range(0, len(ids), 100):
        chunk = ",".join(map(str, ids[i:i + 100]))
        for el in get(f"https://api.openstreetmap.org/api/0.6/ways.json?ways={chunk}")["elements"]:
            way_nodes[el["id"]] = el["nodes"]
    node_ids = sorted({n for v in way_nodes.values() for n in v})
    coords = {}
    for i in range(0, len(node_ids), 300):
        chunk = ",".join(map(str, node_ids[i:i + 300]))
        for el in get(f"https://api.openstreetmap.org/api/0.6/nodes.json?nodes={chunk}")["elements"]:
            coords[el["id"]] = (el["lon"], el["lat"])
    out = {w: [coords[n] for n in nds] for w, nds in way_nodes.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in out.items()}, f)
    return out


def loop_line(loop, geom):
    """Concatenate the `loop` override's way pieces into one polyline."""
    line = []
    for item in loop:
        if isinstance(item, list):
            wid, i, j = item
            g = geom[wid]
            part = g[i:j + 1] if j >= i else g[j:i + 1][::-1]
        else:
            g = geom[abs(item)]
            part = g if item > 0 else g[::-1]
        if line and part[0] != line[-1]:
            raise SystemExit(f"loop override is not continuous at way {item}")
        line += part if not line else part[1:]
    return line


# ------------------------------------------------------------------ main ---

def way_coords(el):
    return [(g["lon"], g["lat"]) for g in el.get("geometry", []) if g]


def fetch_track(t):
    opts = TRACK_OPTS.get(t["handle"], {})
    search = opts.get("search", SEARCH_M)
    print(f"[{t['handle']}] {t['name']}", flush=True)

    if opts.get("loop"):
        q1 = None
    elif opts.get("ways"):
        q1 = f"[out:json][timeout:120];\n{opts['ways']};\nout geom;"
    elif opts.get("relation"):
        q1 = f"[out:json][timeout:120];\nrel({opts['relation']});\nout geom;"
    else:
        q1 = f"""[out:json][timeout:120];
(
  rel[type=circuit](around:{search},{t['lat']},{t['lon']});
  way[highway=raceway](around:{search},{t['lat']},{t['lon']});
);
out geom;"""
    d1 = overpass(q1, t["handle"] + "-circuit") if q1 else {"elements": []}
    loc = Local(t["lat"], t["lon"])

    def length(lines):
        tot = 0
        for l in lines:
            p = [loc.fwd(*c) for c in l]
            tot += sum(math.dist(a, b) for a, b in zip(p, p[1:]))
        return tot

    rels = []
    for el in d1["elements"]:
        if el["type"] != "relation":
            continue
        name = el.get("tags", {}).get("name", "")
        if opts.get("avoid") and re.search(opts["avoid"], name, re.I):
            continue
        lines = [way_coords(m) for m in el.get("members", [])
                 if m["type"] == "way" and "pit" not in (m.get("role") or "").lower()]
        lines = [l for l in lines if len(l) > 1]
        if lines:
            score = 1 if opts.get("prefer") and re.search(opts["prefer"], name, re.I) else 0
            rels.append((score, length(lines), name, lines))
    if opts.get("loop"):
        geom = osm_api_ways([abs(x[0]) if isinstance(x, list) else abs(x) for x in opts["loop"]],
                            t["handle"] + "-loop")
        line = loop_line(opts["loop"], geom)
        circuit, cname = [line], opts.get("name", t["name"])
        print(f"    loop override: {len(opts['loop'])} pieces, {length(circuit) / 1000:.2f} km, "
              f"closed={'yes' if line[0] == line[-1] else 'no'}")
    elif rels:
        rels.sort(key=lambda r: (r[0], r[1]), reverse=True)
        _, _, cname, circuit = rels[0]
        print(f"    circuit relation: {cname!r} ({len(rels)} candidates)")
    else:
        circuit, cname = [], t["name"]
        for el in d1["elements"]:
            tags = el.get("tags", {})
            if el["type"] == "way" and tags.get("highway") == "raceway" and tags.get("area") != "yes":
                n = (tags.get("name", "") + " " + tags.get("service", "")).lower()
                if "pit" in n or "stand" in n:
                    continue
                if opts.get("avoid") and re.search(opts["avoid"], n, re.I):
                    continue
                circuit.append(way_coords(el))
        print(f"    raceway ways: {len(circuit)}")
    if not circuit:
        print("    !! no circuit found, skipping")
        return None
    circuit = tidy_circuit(join_lines(circuit), Local(t["lat"], t["lon"]))

    # extent of the circuit (metres) and surroundings box
    pts = [loc.fwd(*c) for l in circuit for c in l]
    cx0 = min(p[0] for p in pts); cx1 = max(p[0] for p in pts)
    cy0 = min(p[1] for p in pts); cy1 = max(p[1] for p in pts)
    extent = max(cx1 - cx0, cy1 - cy0)
    box = (cx0 - MARGIN_M, cy0 - MARGIN_M, cx1 + MARGIN_M, cy1 + MARGIN_M)
    w, s = loc.inv(box[0], box[1])
    e, n = loc.inv(box[2], box[3])
    big = extent > BIG_EXTENT_M
    print(f"    extent {extent / 1000:.1f} km, box {w:.4f},{s:.4f},{e:.4f},{n:.4f}")

    roads = "|".join(k for k, v in ROAD_CLASSES.items() if v <= (3 if big else 5))
    building = "" if big else "way[building];"
    q2 = f"""[out:json][timeout:240][bbox:{s:.5f},{w:.5f},{n:.5f},{e:.5f}];
(
  way[highway~"^({roads})$"];
  way[natural=water]; rel[natural=water];
  way[waterway~"^(riverbank|river|canal|stream|dock)$"]; rel[waterway=riverbank];
  way[landuse~"^(reservoir|basin)$"];
  way[landuse=forest]; rel[landuse=forest];
  way[natural=wood]; rel[natural=wood];
  way[natural=coastline];
  {building}
);
out geom;"""
    d2 = overpass(q2, t["handle"] + "-area", bbox=(s, w, n, e))

    # spatial filter: within MARGIN_M of the circuit
    cell = MARGIN_M
    grid = set()
    samples = {}
    for l in circuit:
        p = [loc.fwd(*c) for c in l]
        for a, b in zip(p, p[1:]):
            steps = max(1, int(math.dist(a, b) / 200))
            for i in range(steps + 1):
                x = a[0] + (b[0] - a[0]) * i / steps
                y = a[1] + (b[1] - a[1]) * i / steps
                grid.add((int(math.floor(x / cell)), int(math.floor(y / cell))))
                samples.setdefault((int(math.floor(x / 200)), int(math.floor(y / 200))), []).append((x, y))

    def near_dist(xy, dist):
        # tighter check for buildings: any vertex within `dist` of a circuit sample
        for x, y in xy[:: max(1, len(xy) // 4)]:
            gx, gy = int(math.floor(x / 200)), int(math.floor(y / 200))
            r = int(dist // 200) + 1
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    for sx, sy in samples.get((gx + dx, gy + dy), ()):
                        if (sx - x) ** 2 + (sy - y) ** 2 < dist * dist:
                            return True
        return False

    def near(xy):
        bx0 = min(p[0] for p in xy); bx1 = max(p[0] for p in xy)
        by0 = min(p[1] for p in xy); by1 = max(p[1] for p in xy)
        if (bx1 - bx0) * (by1 - by0) > 4 * cell * cell:
            return True  # big polygon: keep (it is clipped to the box anyway)
        for x, y in xy:
            gx, gy = int(math.floor(x / cell)), int(math.floor(y / cell))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if (gx + dx, gy + dy) in grid:
                        return True
        return False

    tol = max(1.0, extent / 3000)           # simplification tolerance (m)
    nd = 6 if extent < 1500 else 5          # decimals in output

    def out_coords(xy):
        res, last = [], None
        for x, y in xy:
            lon, lat = loc.inv(x, y)
            c = [round(lon, nd), round(lat, nd)]
            if c != last:
                res.append(c)
            last = c
        return res

    features = []

    def add_line(kind, lines, props=None, clip=True):
        for l in lines:
            xy = [loc.fwd(*c) for c in l]
            for piece in (clip_line(xy, box) if clip else [xy]):
                piece = simplify(piece, tol)
                c = out_coords(piece)
                if len(c) > 1:
                    features.append({"type": "Feature", "properties": dict(kind=kind, **(props or {})),
                                     "geometry": {"type": "LineString", "coordinates": c}})

    def add_poly(kind, rings, min_area):
        out = []
        for r in rings:
            xy = [loc.fwd(*c) for c in r]
            if xy and xy[0] == xy[-1]:
                xy = xy[:-1]
            if len(xy) < 3:
                continue
            xy = clip_ring(xy, box)
            if len(xy) < 3:
                continue
            xy = simplify(xy + [xy[0]], max(tol, 2.5) if kind == "building" else max(tol * 2, 3.0))[:-1]
            if len(xy) < 3 or abs(ring_area(xy)) < min_area:
                continue
            c = out_coords(xy)
            if len(c) >= 3:
                out.append(c + [c[0]])
        if out:
            features.append({"type": "Feature", "properties": {"kind": kind},
                             "geometry": {"type": "Polygon", "coordinates": out}})

    coast = []
    area_scale = (extent / 3000) ** 2
    for el in d2["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "way":
            xy = [loc.fwd(*c) for c in way_coords(el)]
            if len(xy) < 2:
                continue
            if tags.get("natural") == "coastline":
                coast.append(way_coords(el))
                continue
            if not near(xy):
                continue
            hw = tags.get("highway")
            if hw in ROAD_CLASSES and ROAD_CLASSES[hw] == 5 and (extent > 4000 or not near_dist(xy, 800)):
                continue                     # service roads/tracks: only close to the circuit
            if hw in ROAD_CLASSES:
                add_line("road", [way_coords(el)], {"c": ROAD_CLASSES[hw]})
            elif "building" in tags:
                if near_dist(xy, BUILDING_M):
                    add_poly("building", [way_coords(el)], 80 * max(1, area_scale))
            elif tags.get("waterway") in ("river", "canal", "stream"):
                add_line("waterline", [way_coords(el)], {"c": 1 if tags["waterway"] != "stream" else 2})
            elif tags.get("natural") == "water" or tags.get("waterway") in ("riverbank", "dock") \
                    or tags.get("landuse") in ("reservoir", "basin"):
                add_poly("water", [way_coords(el)], 50 * max(1, area_scale))
            elif tags.get("landuse") == "forest" or tags.get("natural") == "wood":
                add_poly("forest", [way_coords(el)], 400 * max(1, area_scale))
        elif el["type"] == "relation":
            members = [way_coords(m) for m in el.get("members", []) if m["type"] == "way"]
            members = [m for m in members if len(m) > 1]
            if not members:
                continue
            rings = [r for r in join_lines(members) if len(r) > 3]
            allxy = [loc.fwd(*c) for r in rings for c in r]
            if not allxy or not near(allxy):
                continue
            if "building" in tags:
                kind, mina = "building", 25
            elif tags.get("landuse") == "forest" or tags.get("natural") == "wood":
                kind, mina = "forest", 400
            else:
                kind, mina = "water", 50
            add_poly(kind, rings, mina * max(1, area_scale))

    if coast:
        chains = [[loc.fwd(*c) for c in l] for l in join_lines(coast)]
        sea = sea_polygons(chains, box)
        if sea:
            add_poly("water", [[loc.inv(*p) for p in r] for r in sea], 0)

    # circuit last so it is drawn on top; keep it unclipped and lightly simplified
    circ_tol = max(0.5, extent / 6000)
    for l in sorted(circuit, key=lambda l: -len(l)):
        xy = simplify([loc.fwd(*c) for c in l], circ_tol)
        c = out_coords(xy)
        if len(c) > 1:
            features.append({"type": "Feature", "properties": {"kind": "circuit", "name": cname},
                             "geometry": {"type": "LineString", "coordinates": c}})

    # merge each layer (kind + road class) into one Multi* feature to keep files small;
    # the circuit stays as separate, ordered LineStrings
    order = {"water": 0, "forest": 1, "waterline": 2, "building": 3, "road": 4, "circuit": 5}
    layers, circuit_feats = {}, []
    for f_ in features:
        pr = f_["properties"]
        if pr["kind"] == "circuit":
            circuit_feats.append(f_)
            continue
        key = (pr["kind"], pr.get("c"))
        layers.setdefault(key, []).append(f_["geometry"]["coordinates"])
    features = []
    for (kind, c), parts in sorted(layers.items(), key=lambda kv: (order[kv[0][0]], -(kv[0][1] or 0))):
        multi = "MultiLineString" if kind in ("road", "waterline") else "MultiPolygon"
        props = {"kind": kind} if c is None else {"kind": kind, "c": c}
        features.append({"type": "Feature", "properties": props,
                         "geometry": {"type": multi, "coordinates": parts}})
    features += circuit_feats
    cw, cs = loc.inv(cx0, cy0)
    ce, cn = loc.inv(cx1, cy1)
    fc = {
        "type": "FeatureCollection",
        "name": t["name"],
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "bbox": [round(w, 5), round(s, 5), round(e, 5), round(n, 5)],
        "circuitBbox": [round(cw, 5), round(cs, 5), round(ce, 5), round(cn, 5)],
        "features": features,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, t["handle"] + ".geojson")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    counts = {}
    for f_ in features:
        k_ = f_["properties"]["kind"]
        n_ = len(f_["geometry"]["coordinates"]) if f_["geometry"]["type"].startswith("Multi") else 1
        counts[k_] = counts.get(k_, 0) + n_
    print(f"    wrote {os.path.relpath(path, ROOT)} ({os.path.getsize(path) // 1024} KB) {counts}", flush=True)
    return path


def main():
    tracks = parse_tracks()
    want = set(sys.argv[1:])
    for t in tracks:
        if want and t["handle"] not in want:
            continue
        fetch_track(t)
        time.sleep(2)


if __name__ == "__main__":
    main()
