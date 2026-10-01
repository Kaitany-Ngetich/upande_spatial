# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""Lays out a whole EPANET network inside a boundary polygon in one go,
instead of digitizing hundreds of junctions and pipes by hand in the Map
Viewer.

The layout is a looped subdivision-style pattern:

- a ring main running just inside the boundary,
- parallel distribution mains at a chosen angle, each end tied into the ring,
- cross-connectors every few hundred metres so the mains form loops,
- short dead-end laterals off the mains, alternating sides, each with a
  dog-leg at the end (the "cul-de-sac" look),
- one Reservoir just outside the ring, fed in by a short transmission pipe.

Everything is saved as ordinary Spatial Features owned by the boundary's own
record (e.g. Farm Kapkolia's own "Water Pipes"/"Water Junctions"), exactly
like elements added through the EPANET plugin (see api/epanet.py), so
run_simulation and the Map Viewer need nothing special for a generated
network. Pipe endpoints are written with their nodes' exact coordinates and
nodes are kept at least MIN_NODE_SPACING_M apart, so run_simulation's
SNAP_TOLERANCE_M snapping can never attach a pipe to the wrong node."""

import json
import math

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_spatial.api.epanet import SNAP_TOLERANCE_M, _first_geometry

EARTH_RADIUS_M = 6371000.0
MIN_NODE_SPACING_M = 2 * SNAP_TOLERANCE_M
SOURCE_MODULE = "EPANET Network Generator"

# Pipe classes, in the order a shared segment should be classified by
# (a segment lying on both the ring and a main is ring).
PIPE_CLASSES = ("Feed", "Ring", "Main", "Cross", "Lateral")


class _LocalProjection:
	"""Equirectangular projection around the boundary's own centroid - same
	approach as Spatial Feature's area/length maths, plenty accurate at farm
	scale and avoids a pyproj dependency just for this."""

	def __init__(self, lon0, lat0):
		self.lon0, self.lat0 = lon0, lat0
		self.kx = EARTH_RADIUS_M * math.cos(math.radians(lat0)) * math.pi / 180
		self.ky = EARTH_RADIUS_M * math.pi / 180

	def to_xy(self, lon, lat):
		return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

	def to_lonlat(self, x, y):
		return [round(self.lon0 + x / self.kx, 8), round(self.lat0 + y / self.ky, 8)]


def _load_boundary(boundary_feature):
	from shapely.geometry import Polygon

	doc = frappe.get_doc("Spatial Feature", boundary_feature)
	geom = _first_geometry(doc.geometry)
	if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
		frappe.throw(_("{0} is not a Polygon feature.").format(boundary_feature))
	if geom["type"] == "MultiPolygon":
		# Largest part only - one connected network per call.
		rings = max(geom["coordinates"], key=lambda p: len(p[0]))
	else:
		rings = geom["coordinates"]
	outer = rings[0]
	lon0 = sum(p[0] for p in outer) / len(outer)
	lat0 = sum(p[1] for p in outer) / len(outer)
	proj = _LocalProjection(lon0, lat0)
	poly = Polygon([proj.to_xy(p[0], p[1]) for p in outer]).buffer(0)
	if poly.geom_type == "MultiPolygon":
		poly = max(poly.geoms, key=lambda g: g.area)
	return doc, poly, proj


def _parts(geom, min_length):
	"""Flattens an intersection result into its LineString parts."""
	if geom.is_empty:
		return []
	parts = getattr(geom, "geoms", [geom])
	return [g for g in parts if g.geom_type == "LineString" and g.length >= min_length]


def _layout(poly, p):
	"""Returns (tagged_lines, ring_polygon): tagged_lines is a list of
	(pipe_class, LineString) in projected metres."""
	from shapely import affinity
	from shapely.geometry import LineString

	ring_poly = poly.buffer(-p["ring_inset_m"], join_style=2).simplify(4)
	if ring_poly.is_empty:
		frappe.throw(_("Boundary is too small for a {0} m ring inset.").format(p["ring_inset_m"]))
	if ring_poly.geom_type == "MultiPolygon":
		ring_poly = max(ring_poly.geoms, key=lambda g: g.area)

	origin = ring_poly.centroid
	angle = p["angle_deg"]
	# Work in a rotated frame where mains run along x and are stacked along y.
	frame = affinity.rotate(ring_poly, -angle, origin=origin)
	interior = frame.buffer(-MIN_NODE_SPACING_M)
	minx, miny, maxx, maxy = frame.bounds

	mains = []
	y = miny + p["main_spacing_m"] / 2
	while y < maxy:
		cut = LineString([(minx - 1, y), (maxx + 1, y)]).intersection(frame)
		mains += _parts(cut, 2 * MIN_NODE_SPACING_M)
		y += p["main_spacing_m"]

	crosses, cross_xs = [], []
	x = minx + p["cross_spacing_m"] / 2
	while x < maxx:
		cut = LineString([(x, miny - 1), (x, maxy + 1)]).intersection(frame)
		parts = _parts(cut, 2 * MIN_NODE_SPACING_M)
		if parts:
			crosses += parts
			cross_xs.append(x)
		x += p["cross_spacing_m"]

	laterals = []
	dog = p["lateral_dogleg_m"]
	reach = p["lateral_length_m"]
	clearance = abs(dog) + 2 * MIN_NODE_SPACING_M
	for main in mains:
		xs = [c[0] for c in main.coords]
		x0, x1, y0 = min(xs), max(xs), main.coords[0][1]
		i = 0
		x = x0 + p["lateral_spacing_m"] / 2
		while x < x1 - MIN_NODE_SPACING_M:
			side = 1 if i % 2 == 0 else -1
			bend = dog if (i // 2) % 2 == 0 else -dog
			i += 1
			lat = LineString([(x, y0), (x, y0 + side * reach), (x + bend, y0 + side * (reach + dog / 2))])
			x += p["lateral_spacing_m"]
			# Keep clear of cross-connectors and of the ring itself.
			if any(abs(lat.coords[0][0] - cx) < clearance for cx in cross_xs):
				continue
			if not interior.contains(LineString(lat.coords[1:])) or lat.coords[0][0] - x0 < MIN_NODE_SPACING_M:
				continue
			laterals.append(lat)

	back = lambda g: affinity.rotate(g, angle, origin=origin)
	tagged = [("Ring", LineString(ring_poly.exterior.coords))]
	tagged += [("Main", back(g)) for g in mains]
	tagged += [("Cross", back(g)) for g in crosses]
	tagged += [("Lateral", back(g)) for g in laterals]
	return tagged, ring_poly


def _build_graph(tagged, max_pipe_m):
	"""Nodes every crossing, splits long runs, merges near-coincident nodes.
	Returns (nodes, pipes): nodes = {node_key: (x, y)}, pipes = list of
	dicts with a/b node keys, class, coords, length."""
	from shapely.geometry import Point
	from shapely.ops import substring, unary_union

	noded = unary_union([g for cls, g in tagged])
	segments = list(getattr(noded, "geoms", [noded]))

	def classify(seg):
		mid = seg.interpolate(0.5, normalized=True)
		best = None
		for cls, g in tagged:
			if g.distance(mid) < 0.05 and (best is None or PIPE_CLASSES.index(cls) < PIPE_CLASSES.index(best)):
				best = cls
		return best or "Lateral"

	raw = []
	for seg in segments:
		if seg.length < 0.01:
			continue
		cls = classify(seg)
		n = max(1, math.ceil(seg.length / max_pipe_m))
		for k in range(n):
			raw.append((cls, substring(seg, seg.length * k / n, seg.length * (k + 1) / n)))

	# Cluster endpoints closer than MIN_NODE_SPACING_M into one node.
	nodes = {}
	def node_for(pt):
		for key, xy in nodes.items():
			if math.dist(xy, pt) < MIN_NODE_SPACING_M:
				return key
		key = len(nodes)
		nodes[key] = pt
		return key

	# Register crossings/junction points first (endpoints shared by >1
	# segment), so a cluster keeps the real junction rather than a split point.
	counts = {}
	for cls, s in raw:
		for pt in (s.coords[0], s.coords[-1]):
			k = (round(pt[0], 3), round(pt[1], 3))
			counts[k] = counts.get(k, 0) + 1
	for k in sorted(counts, key=lambda k: -counts[k]):
		node_for(k)

	pipes, seen = [], set()
	for cls, s in raw:
		a, b = node_for(s.coords[0]), node_for(s.coords[-1])
		if a == b or (min(a, b), max(a, b)) in seen:
			continue
		seen.add((min(a, b), max(a, b)))
		coords = [nodes[a]] + list(s.coords[1:-1]) + [nodes[b]]
		length = sum(math.dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
		pipes.append({"a": a, "b": b, "class": cls, "coords": coords, "length": length})

	# Drop nodes nothing ended up using (merged-away split points).
	used = {p["a"] for p in pipes} | {p["b"] for p in pipes}
	nodes = {k: v for k, v in nodes.items() if k in used}
	return nodes, pipes


def _largest_component(nodes, pipes, keep):
	"""Keeps only the part of the graph connected to node `keep`."""
	adj = {}
	for p in pipes:
		adj.setdefault(p["a"], []).append(p["b"])
		adj.setdefault(p["b"], []).append(p["a"])
	reached, stack = {keep}, [keep]
	while stack:
		for nxt in adj.get(stack.pop(), []):
			if nxt not in reached:
				reached.add(nxt)
				stack.append(nxt)
	return {k: v for k, v in nodes.items() if k in reached}, [p for p in pipes if p["a"] in reached]


@frappe.whitelist()
def generate_network(
	boundary_feature,
	network_name,
	angle_deg=42,
	ring_inset_m=12,
	main_spacing_m=150,
	cross_spacing_m=340,
	lateral_spacing_m=90,
	lateral_length_m=35,
	lateral_dogleg_m=22,
	max_pipe_m=0,
	elevation_m=0,
	source_head_m=45,
	demand_mm_per_day=6,
	irrigation_hours=12,
	demand_placement="lateral_ends",
	demand_pattern=None,
	feed_diameter_mm=250,
	ring_diameter_mm=200,
	main_diameter_mm=110,
	lateral_diameter_mm=63,
	roughness=140,
	replace=0,
	dry_run=0,
):
	"""Generates a looped pipe network inside `boundary_feature` (a Polygon
	Spatial Feature, e.g. a Farm Boundary) and saves it as EPANET Network
	`network_name`, scoped to whatever the boundary itself references.

	Demand: demand_mm_per_day of water over the boundary's area, delivered
	within irrigation_hours. demand_placement="lateral_ends" puts it all,
	split equally, on the dead-end node of each lateral - the block offtake
	a lateral exists to feed - so laterals actually carry their block's flow.
	"length" instead spreads it over every junction by the pipe length each
	one serves (a uniform-drip approximation). demand_pattern (an EPANET
	Pattern name) is attached to every junction that carries demand, so an
	extended-period run follows e.g. an irrigation schedule. Every node gets the same elevation_m (no terrain data yet); the
	Reservoir's head is elevation_m + source_head_m.

	max_pipe_m splits long runs into pieces no longer than that (spreads
	"length" demand more finely, at the cost of many more pipes); 0 leaves
	every run between two junctions as one pipe.

	dry_run=1 returns the layout statistics without writing anything.
	replace=1 deletes an existing network's elements first; without it an
	existing, non-empty network is refused rather than silently doubled."""
	p = {
		"angle_deg": flt(angle_deg),
		"ring_inset_m": flt(ring_inset_m),
		"main_spacing_m": flt(main_spacing_m),
		"cross_spacing_m": flt(cross_spacing_m),
		"lateral_spacing_m": flt(lateral_spacing_m),
		"lateral_length_m": flt(lateral_length_m),
		"lateral_dogleg_m": flt(lateral_dogleg_m),
	}
	diameters = {
		"Feed": flt(feed_diameter_mm),
		"Ring": flt(ring_diameter_mm),
		"Main": flt(main_diameter_mm),
		"Cross": flt(main_diameter_mm),
		"Lateral": flt(lateral_diameter_mm),
	}
	elevation_m = flt(elevation_m)

	if demand_pattern and not frappe.db.exists("EPANET Pattern", demand_pattern):
		frappe.throw(_("EPANET Pattern {0} not found.").format(demand_pattern))

	boundary, poly, proj = _load_boundary(boundary_feature)
	tagged, ring_poly = _layout(poly, p)

	# Source just outside the ring's northern-most point, pointing away from the centre.
	top = max(ring_poly.exterior.coords, key=lambda c: c[1])
	cx, cy = ring_poly.centroid.x, ring_poly.centroid.y
	d = math.dist(top, (cx, cy)) or 1
	source = (top[0] + (top[0] - cx) / d * 25, top[1] + (top[1] - cy) / d * 25)
	from shapely.geometry import LineString

	tagged.append(("Feed", LineString([source, top])))

	nodes, pipes = _build_graph(tagged, flt(max_pipe_m) or math.inf)
	source_key = min(nodes, key=lambda k: math.dist(nodes[k], source))
	nodes, pipes = _largest_component(nodes, pipes, source_key)

	total_lps = poly.area / 10000 * flt(demand_mm_per_day) * 10 / (flt(irrigation_hours) or 24) / 3.6
	served = {k: 0.0 for k in nodes}
	degree = {k: 0 for k in nodes}
	for pipe in pipes:
		degree[pipe["a"]] += 1
		degree[pipe["b"]] += 1
	lateral_ends = {
		n for pipe in pipes if pipe["class"] == "Lateral"
		for n in (pipe["a"], pipe["b"]) if degree[n] == 1 and n != source_key
	}
	if demand_placement == "lateral_ends" and lateral_ends:
		for n in lateral_ends:
			served[n] = 1.0
	else:
		# Length-weighted: each pipe hands half its length to each end node.
		for pipe in pipes:
			if pipe["class"] == "Feed":
				continue
			served[pipe["a"]] += pipe["length"] / 2
			served[pipe["b"]] += pipe["length"] / 2
		served[source_key] = 0.0
	total_served = sum(served.values()) or 1

	stats = {
		"area_ha": round(poly.area / 10000, 2),
		"junctions": len(nodes) - 1,
		"reservoirs": 1,
		"pipes": len(pipes),
		"total_length_km": round(sum(x["length"] for x in pipes) / 1000, 2),
		"total_demand_lps": round(total_lps, 1),
		"length_by_class_m": {
			c: round(sum(x["length"] for x in pipes if x["class"] == c)) for c in PIPE_CLASSES
		},
		"dead_ends": sum(1 for k in nodes if k != source_key and degree[k] == 1),
		"demand_nodes": sum(1 for v in served.values() if v),
	}
	if cint(dry_run):
		return stats

	network, owner = _prepare_network(network_name, boundary, cint(replace), stats)

	layer_cache = {}
	junction_no = 0
	for k in sorted(nodes, key=lambda k: (-nodes[k][1], nodes[k][0])):
		if k == source_key:
			role, title = "Reservoir", "R-01"
			props = {"base_head_m": round(elevation_m + flt(source_head_m), 2), "elevation_m": elevation_m}
		else:
			junction_no += 1
			role, title = "Junction", f"J-{junction_no:03d}"
			props = {
				"elevation_m": elevation_m,
				"base_demand_lps": round(total_lps * served[k] / total_served, 4),
			}
			if demand_pattern and served[k]:
				props["demand_pattern"] = demand_pattern
		_insert_element(owner, boundary, role, title, {
			"type": "Point", "coordinates": proj.to_lonlat(*nodes[k]),
		}, props, layer_cache)

	for i, pipe in enumerate(pipes, start=1):
		_insert_element(owner, boundary, "Pipe", f"P-{i:03d}", {
			"type": "LineString", "coordinates": [proj.to_lonlat(x, y) for x, y in pipe["coords"]],
		}, {
			"diameter_mm": diameters[pipe["class"]],
			"roughness": flt(roughness),
			"pipe_class": pipe["class"],
		}, layer_cache)

	frappe.db.commit()
	return {"network": network, "assets_owned_by": f"{owner[0]}: {owner[1]}", **stats}


def _prepare_network(network_name, boundary, replace, stats):
	"""Creates (or reuses) the EPANET Network and clears out whatever a
	previous generation left behind. Returns (network name, owner), owner
	being the boundary's own record (e.g. Farm Kapkolia) that the new
	elements belong to, so the simulation runs on the farm's own pipes.

	Controls pointing at an element being replaced are removed (counted in
	stats["controls_removed"]) - the new elements have new names.

	Only elements this generator created are ever deleted on replace -
	pipes or junctions somebody drew by hand on the same Farm are left
	alone (and will simply be part of the simulated network too)."""
	from upande_spatial.api.epanet import LINK_ROLES, NODE_ROLES, _owner_accepts

	ref_dt, ref_name = boundary.reference_doctype, boundary.reference_name
	if not (ref_dt and ref_name and _owner_accepts(ref_dt, "Pipe", "LineString")):
		frappe.throw(_(
			"{0} must reference a record whose type holds water-network assets (e.g. a Farm with "
			"Junction/Pipe roles in Spatial Entity Config) - the generated pipes belong to it."
		).format(boundary.name))

	if frappe.db.exists("EPANET Network", network_name):
		doc = frappe.get_doc("EPANET Network", network_name)
		if (doc.reference_doctype, doc.reference_name) != (ref_dt, ref_name):
			frappe.throw(_("EPANET Network {0} belongs to {1} {2}, not {3} {4}.").format(
				network_name, doc.reference_doctype or "-", doc.reference_name or "-", ref_dt, ref_name
			))
	else:
		doc = frappe.new_doc("EPANET Network")
		doc.network_name = network_name
		if ref_dt and ref_name:
			doc.reference_doctype = ref_dt
			doc.reference_name = ref_name
		doc.company = boundary.company
	doc.description = _(
		"Generated inside {0}: {1} junctions, {2} pipes, {3} km, {4} L/s total demand."
	).format(boundary.title or boundary.name, stats["junctions"], stats["pipes"],
		stats["total_length_km"], stats["total_demand_lps"])

	previous = frappe.get_all(
		"Spatial Feature",
		filters={"reference_doctype": "EPANET Network", "reference_name": network_name},
		pluck="name",
	)
	previous += frappe.get_all(
		"Spatial Feature",
		filters={
			"reference_doctype": ref_dt,
			"reference_name": ref_name,
			"feature_role": ["in", NODE_ROLES + LINK_ROLES],
			"source_module": SOURCE_MODULE,
		},
		pluck="name",
	)
	if previous and not replace:
		frappe.throw(_("{0} generated elements already exist for {1} - pass replace=1 to regenerate them.").format(
			len(previous), f"{ref_dt} {ref_name}"
		))
	# Regenerated elements get new record names, so any Control pointing at
	# an old one is meaningless (and would block the delete as a link) -
	# drop those rows, on whichever network holds them.
	stats["controls_removed"] = 0
	if previous:
		stale = frappe.get_all(
			"EPANET Control",
			or_filters=[["target_element", "in", previous], ["source_node", "in", previous]],
			fields=["name", "parent"],
		)
		for parent in {r.parent for r in stale}:
			net = doc if parent == doc.name else frappe.get_doc("EPANET Network", parent)
			keep = [r for r in net.controls if r.target_element not in previous and r.source_node not in previous]
			stats["controls_removed"] += len(net.controls) - len(keep)
			net.controls = keep
			if net is not doc:
				net.save()
		if doc.name and any(r.parent == doc.name for r in stale):
			doc.save()
	for name in previous:
		frappe.delete_doc("Spatial Feature", name)

	doc.save()
	return doc.name, (ref_dt, ref_name)


def _insert_element(owner, boundary, role, title, geometry, properties, layer_cache):
	from upande_spatial.api.spatial import _as_geojson_feature, _resolve_layer_name, _validate_against_config

	gtype = geometry["type"]
	if (role, gtype) not in layer_cache:
		_validate_against_config(owner[0], role, gtype)
		layer_cache[(role, gtype)] = _resolve_layer_name(owner[0], role, gtype)["layer_name"]

	doc = frappe.new_doc("Spatial Feature")
	doc.reference_doctype, doc.reference_name = owner
	doc.feature_role = role
	doc.title = title
	doc.farm = boundary.farm
	doc.company = boundary.company
	doc.geometry = _as_geojson_feature(geometry, properties)
	doc.properties = json.dumps(properties)
	doc.source_module = SOURCE_MODULE
	doc.crs = "EPSG:4326"
	doc.layer = layer_cache[(role, gtype)]
	doc.insert()
	return doc.name
