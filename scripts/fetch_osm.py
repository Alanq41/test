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
SEARCH_M = 3000          # radius around the tracks.js point to look for the circuit

# Per-track tweaks. `search` = circuit search radius (m), `prefer` = regex the
# circuit relation's name should match, `avoid` = regex for names to skip.
TRACK_OPTS = {
    "bathurst-t-shirt": {"prefer": r"panorama"},
    "isle-of-man-1": {"search": 4000, "prefer": r"mountain|snaefell|TT"},
    "le-mans-t-shirt": {"search": 7000, "prefer": r"sarthe|24"},
    "long-beach-1": {"prefer": r"long beach"},
    "monaco-t-shirt": {"avoid": r"e-?prix|formula e"},
    "nurburgring-t-shirt": {"search": 6000, "prefer": r"nordschleife"},
    "silverstone-kart-track-1": {"search": 2000, "prefer": r"kart"},
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


def overpass(query, label):
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha1(query.encode()).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"{label}-{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
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


# ------------------------------------------------------------------ main ---

def way_coords(el):
    return [(g["lon"], g["lat"]) for g in el.get("geometry", []) if g]


def fetch_track(t):
    opts = TRACK_OPTS.get(t["handle"], {})
    search = opts.get("search", SEARCH_M)
    print(f"[{t['handle']}] {t['name']}", flush=True)

    q1 = f"""[out:json][timeout:120];
(
  rel[type=circuit](around:{search},{t['lat']},{t['lon']});
  way[highway=raceway](around:{search},{t['lat']},{t['lon']});
);
out geom;"""
    d1 = overpass(q1, t["handle"] + "-circuit")
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
    if rels:
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
                circuit.append(way_coords(el))
        print(f"    raceway ways: {len(circuit)}")
    if not circuit:
        print("    !! no circuit found, skipping")
        return None
    circuit = join_lines(circuit)

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
    d2 = overpass(q2, t["handle"] + "-area")

    # spatial filter: within MARGIN_M of the circuit
    cell = MARGIN_M
    grid = set()
    for l in circuit:
        p = [loc.fwd(*c) for c in l]
        for a, b in zip(p, p[1:]):
            steps = max(1, int(math.dist(a, b) / 200))
            for i in range(steps + 1):
                x = a[0] + (b[0] - a[0]) * i / steps
                y = a[1] + (b[1] - a[1]) * i / steps
                grid.add((int(math.floor(x / cell)), int(math.floor(y / cell))))

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
            xy = simplify(xy + [xy[0]], tol)[:-1]
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
            if hw in ROAD_CLASSES:
                add_line("road", [way_coords(el)], {"c": ROAD_CLASSES[hw]})
            elif "building" in tags:
                add_poly("building", [way_coords(el)], 40 * max(1, area_scale))
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
