# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""EPANET hydraulic simulation for Map Viewer's EPANET plugin.

Network elements (junctions, tanks, reservoirs, pipes, pumps, valves) are
plain Spatial Features - reference_doctype="EPANET Network", reference_name
being the network's own name, feature_role telling nodes from links apart
(see Spatial Entity Config's "EPANET Network" row for the exact role/
geometry/layer mapping). Hydraulic attributes (elevation, diameter, demand,
...) live in each feature's generic `properties` JSON rather than dedicated
doctype fields - same reuse-don't-duplicate spirit as the rest of this app.

Pipe/Pump/Valve topology (which two nodes a link connects) is inferred from
geometry, not stored explicitly: a link's LineString start/end coordinates
are snapped to the nearest node within SNAP_TOLERANCE_M metres. That mirrors
how a person actually digitizes a network in any GIS - draw the node, draw
the link so its endpoint lands on/near it - rather than requiring a separate
start-node/end-node field per link. A link whose endpoint has nothing within
tolerance is skipped and reported back as a warning, not a hard failure -
one bad link shouldn't block simulating the rest of the network.

The EPANET engine itself is EPA's own open-source toolkit, used here via
`wntr` (Water Network Tool for Resilience) - no separate binary/service to
install, wntr bundles the compiled toolkit."""

import json
import math
from datetime import datetime as dt

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

NODE_ROLES = ("Junction", "Tank", "Reservoir")
LINK_ROLES = ("Pipe", "Pump", "Valve")
SNAP_TOLERANCE_M = 5.0
VALID_VALVE_TYPES = {"PRV", "PSV", "PBV", "FCV", "TCV", "GPV"}

# Every field on EPANET Network that maps onto wntr's Hydraulics/Time/
# Quality/Energy/Reactions options - the plugin's Options modal reads and
# writes exactly this set. Each one's doctype default already matches
# wntr's own out-of-the-box default, so a network that's never had its
# options touched behaves identically to before this feature existed
# (a single steady-state instant, DDA, no quality analysis).
NETWORK_OPTION_FIELDS = (
	"hyd_trials", "hyd_accuracy", "hyd_unbalanced", "hyd_unbalanced_trials",
	"hyd_demand_model", "hyd_minimum_pressure", "hyd_required_pressure",
	"time_duration_hours", "time_hydraulic_timestep_min", "time_pattern_timestep_min",
	"time_report_timestep_min", "time_start_clocktime",
	"qual_mode", "qual_chemical_name", "qual_units", "qual_trace_node",
	"energy_price", "energy_efficiency_pct",
	"react_bulk_coeff", "react_wall_coeff",
)


def _apply_network_options(wn, net_doc):
	"""Wires the EPANET Network doc's option fields onto wntr's
	WaterNetworkModel.options before solving. Select fields store their
	human-readable Frappe labels ("Continue", "Chemical", ...) - wntr's
	own enums are uppercase ("CONTINUE", "CHEMICAL", ...)."""
	h = wn.options.hydraulic
	h.trials = int(net_doc.hyd_trials or 40)
	h.accuracy = flt(net_doc.hyd_accuracy or 0.001)
	h.unbalanced = (net_doc.hyd_unbalanced or "Continue").upper()
	if h.unbalanced == "CONTINUE":
		h.unbalanced_value = int(net_doc.hyd_unbalanced_trials or 10)
	h.demand_model = net_doc.hyd_demand_model or "DDA"
	if h.demand_model == "PDA":
		h.minimum_pressure = flt(net_doc.hyd_minimum_pressure or 0)
		h.required_pressure = flt(net_doc.hyd_required_pressure or 0.1)

	t = wn.options.time
	t.duration = flt(net_doc.time_duration_hours or 0) * 3600
	t.hydraulic_timestep = int(flt(net_doc.time_hydraulic_timestep_min or 60) * 60)
	t.pattern_timestep = int(flt(net_doc.time_pattern_timestep_min or 60) * 60)
	t.report_timestep = int(flt(net_doc.time_report_timestep_min or 60) * 60)
	clocktime = net_doc.time_start_clocktime
	if clocktime:
		# Frappe's Time fieldtype hands back a datetime.time, not a
		# timedelta - flt() on a bare datetime.time silently returns 0.0
		# rather than erroring, so this has to be handled explicitly or a
		# non-midnight start time gets quietly discarded.
		if hasattr(clocktime, "hour"):
			t.start_clocktime = clocktime.hour * 3600 + clocktime.minute * 60 + clocktime.second
		elif hasattr(clocktime, "total_seconds"):
			t.start_clocktime = clocktime.total_seconds()
		else:
			t.start_clocktime = flt(clocktime)

	quality_warning = None
	q = wn.options.quality
	q.parameter = (net_doc.qual_mode or "None").upper()
	if q.parameter == "CHEMICAL":
		q.chemical_name = net_doc.qual_chemical_name or "Chemical"
		q.inpfile_units = net_doc.qual_units or "mg/L"
	elif q.parameter == "TRACE":
		if net_doc.qual_trace_node and net_doc.qual_trace_node in wn.node_name_list:
			q.trace_node = net_doc.qual_trace_node
		else:
			# An unset/unknown trace node would make wntr fail outright -
			# fall back to no quality analysis rather than losing the
			# hydraulic results too over a bad quality setting.
			q.parameter = "NONE"
			quality_warning = _("Trace analysis needs a valid Trace Node - skipped water quality analysis.")

	e = wn.options.energy
	e.global_price = flt(net_doc.energy_price or 0)
	e.global_efficiency = flt(net_doc.energy_efficiency_pct or 75)

	r = wn.options.reaction
	r.bulk_coeff = flt(net_doc.react_bulk_coeff or 0)
	r.wall_coeff = flt(net_doc.react_wall_coeff or 0)

	return quality_warning


def _first_geometry(geometry_field):
	"""Spatial Feature.geometry is stored as a GeoJSON FeatureCollection
	string holding exactly one Feature (see _as_geojson_feature in
	api/spatial.py). Returns that Feature's bare geometry dict, or None."""
	if not geometry_field:
		return None
	fc = json.loads(geometry_field) if isinstance(geometry_field, str) else geometry_field
	feats = fc.get("features") or []
	return feats[0]["geometry"] if feats else None


def _haversine_m(a, b):
	"""a, b are (lon, lat) pairs. Great-circle distance in metres - good
	enough for snap-tolerance purposes at pipe-network scale."""
	lon1, lat1, lon2, lat2 = (math.radians(v) for v in (a[0], a[1], b[0], b[1]))
	dlon = lon2 - lon1
	dlat = lat2 - lat1
	h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
	return 2 * 6371000 * math.asin(min(1, math.sqrt(h)))


def _load_network_features(network):
	rows = frappe.get_all(
		"Spatial Feature",
		filters={"reference_doctype": "EPANET Network", "reference_name": network},
		fields=["name", "title", "feature_role", "geometry", "properties", "length_m"],
	)
	for row in rows:
		row["_geom"] = _first_geometry(row.get("geometry"))
		try:
			row["_props"] = json.loads(row.get("properties") or "{}")
		except Exception:
			row["_props"] = {}
	return rows


def _nearest_node(coord, nodes):
	"""nodes: list of (spatial_feature_name, (lon, lat)). Returns the
	closest node's name if it's within SNAP_TOLERANCE_M, else None."""
	best, best_d = None, None
	for name, node_coord in nodes:
		d = _haversine_m(coord, node_coord)
		if best_d is None or d < best_d:
			best, best_d = name, d
	return best if best_d is not None and best_d <= SNAP_TOLERANCE_M else None


@frappe.whitelist()
def list_owner_doctypes():
	"""Which doctypes an EPANET Network can be scoped to. Reuses Spatial
	Entity Config's own registry of "what's spatially trackable" rather
	than a second hardcoded list - a network isn't only ever a farm's
	network; it can belong to a Warehouse, a Location, or anything else a
	module registers there, or to nothing at all."""
	return frappe.get_all("Spatial Entity Config", pluck="name", order_by="name")


@frappe.whitelist()
def list_owner_candidates(doctype):
	"""Existing records of the given owner doctype, for the network-owner
	picker - respects the caller's own read permissions on that doctype,
	same as any other frappe.get_all call."""
	if not doctype or not frappe.db.exists("DocType", doctype):
		return []
	meta = frappe.get_meta(doctype)
	title_field = meta.get_title_field()
	fields = ["name"] + ([title_field] if title_field and title_field != "name" else [])
	rows = frappe.get_all(doctype, fields=fields, limit=500, order_by="name")
	return [{"name": r.name, "title": (r.get(title_field) if title_field else None) or r.name} for r in rows]


@frappe.whitelist()
def list_networks():
	"""Every EPANET Network with its element counts by role - drives the
	network picker in the Map Viewer EPANET plugin."""
	networks = frappe.get_all(
		"EPANET Network", fields=["name", "network_name", "reference_doctype", "reference_name"]
	)
	counts = frappe.db.sql(
		"""
		select reference_name, feature_role, count(*) as n
		from `tabSpatial Feature`
		where reference_doctype = 'EPANET Network'
		group by reference_name, feature_role
		""",
		as_dict=True,
	)
	by_network = {}
	for row in counts:
		by_network.setdefault(row.reference_name, {})[row.feature_role] = row.n
	for n in networks:
		n["element_counts"] = by_network.get(n["name"], {})
	return networks


@frappe.whitelist()
def add_element(network, feature_role, geometry, title=None, properties=None):
	"""Creates one new network element (Junction/Tank/Reservoir/Pipe/Pump/
	Valve) as its own Spatial Feature.

	Deliberately does NOT go through spatial.upsert_feature for creation:
	upsert_feature treats (reference_doctype, reference_name, feature_role)
	as a unique key and updates in place on a repeat call with the same
	triple - exactly right for a Farm's one Boundary or one Gate, but wrong
	here, where a single network legitimately has many Junctions, many
	Pipes, etc. all sharing that same triple. Calling upsert_feature
	per-element would silently overwrite the previous element of the same
	role instead of adding a new one. Editing an element afterwards is
	still exactly upsert_feature(name=<this element's name>, ...) -
	matching by the element's own record name is unambiguous and always
	was safe; only the create-by-role path is the problem."""
	from upande_spatial.api.spatial import (
		_as_geojson_feature,
		_geometry_type_of,
		_resolve_layer_name,
		_validate_against_config,
	)

	if not frappe.db.exists("EPANET Network", network):
		frappe.throw(_("EPANET Network {0} not found").format(network))
	if feature_role not in NODE_ROLES + LINK_ROLES:
		frappe.throw(_("feature_role must be one of {0}").format(", ".join(NODE_ROLES + LINK_ROLES)))

	if isinstance(properties, str):
		properties = json.loads(properties) if properties else {}
	properties = properties or {}

	geometry_type = _geometry_type_of(geometry)
	_validate_against_config("EPANET Network", feature_role, geometry_type)

	doc = frappe.new_doc("Spatial Feature")
	doc.reference_doctype = "EPANET Network"
	doc.reference_name = network
	doc.feature_role = feature_role
	doc.geometry = _as_geojson_feature(geometry, properties)
	doc.properties = json.dumps(properties)
	doc.source_module = "Map Viewer EPANET Plugin"
	if title:
		doc.title = title
	doc.layer = _resolve_layer_name("EPANET Network", feature_role, geometry_type)["layer_name"]
	# No ignore_permissions, same reasoning as upsert_feature: a real
	# user's own Spatial Feature create rights apply here too.
	doc.save()
	frappe.db.commit()

	return {"name": doc.name, "geometry_type": doc.geometry_type, "layer": doc.layer}


@frappe.whitelist()
def delete_element(name):
	"""Removes one network element. Thin wrapper over spatial.delete_feature
	by record name, kept here so the EPANET plugin only needs one API
	module for everything network-element-related."""
	doc = frappe.get_doc("Spatial Feature", name)
	if doc.reference_doctype != "EPANET Network":
		frappe.throw(_("{0} is not an EPANET network element.").format(name))
	doc.delete()
	frappe.db.commit()


@frappe.whitelist()
def create_network(network_name, reference_doctype=None, reference_name=None, description=None):
	doc = frappe.new_doc("EPANET Network")
	doc.network_name = network_name
	if reference_doctype:
		doc.reference_doctype = reference_doctype
	if reference_name:
		doc.reference_name = reference_name
	if description:
		doc.description = description
	doc.save()
	frappe.db.commit()
	return {"name": doc.name}


@frappe.whitelist()
def get_network_options(network):
	"""Current Hydraulics/Time/Quality/Energy/Reactions settings for this
	network, for the Options modal to populate itself from."""
	doc = frappe.get_doc("EPANET Network", network)
	return {f: doc.get(f) for f in NETWORK_OPTION_FIELDS}


@frappe.whitelist()
def update_network_options(network, options):
	"""Saves the Options modal's fields back onto the network. Only known
	option fields are ever written - an unrecognized key in `options` is
	silently ignored rather than erroring, so the modal can be extended
	later without a version mismatch breaking old clients."""
	if isinstance(options, str):
		options = json.loads(options)
	doc = frappe.get_doc("EPANET Network", network)
	for fieldname in NETWORK_OPTION_FIELDS:
		if fieldname in options:
			doc.set(fieldname, options[fieldname])
	doc.save()
	frappe.db.commit()
	return {f: doc.get(f) for f in NETWORK_OPTION_FIELDS}


@frappe.whitelist()
def run_simulation(network):
	"""Builds a wntr WaterNetworkModel from this network's Spatial
	Features, runs EPANET's hydraulic solver on it, saves an EPANET
	Simulation Run record either way (Success or Failed), and returns
	per-feature results ready for the map to color features by (pressure
	for nodes, flow/velocity for links)."""
	import wntr

	net_doc = frappe.get_doc("EPANET Network", network)

	features = _load_network_features(network)
	nodes = [f for f in features if f["feature_role"] in NODE_ROLES and f["_geom"]]
	links = [f for f in features if f["feature_role"] in LINK_ROLES and f["_geom"]]

	if not nodes:
		frappe.throw(_("This network has no Junction/Tank/Reservoir features yet."))

	wn = wntr.network.WaterNetworkModel()
	node_coords = []  # (spatial_feature_name, (lon, lat)) - for snapping links to nodes

	for f in nodes:
		geom = f["_geom"]
		if geom.get("type") != "Point" or not geom.get("coordinates"):
			continue
		lon, lat = geom["coordinates"][0], geom["coordinates"][1]
		p = f["_props"]
		role = f["feature_role"]
		node_id = f["name"]
		if role == "Junction":
			wn.add_junction(
				node_id,
				base_demand=flt(p.get("base_demand_lps", 0)) / 1000.0,  # L/s -> m3/s
				elevation=flt(p.get("elevation_m", 0)),
				coordinates=(lon, lat),
			)
		elif role == "Reservoir":
			wn.add_reservoir(node_id, base_head=flt(p.get("base_head_m", 0)), coordinates=(lon, lat))
		elif role == "Tank":
			wn.add_tank(
				node_id,
				elevation=flt(p.get("elevation_m", 0)),
				init_level=flt(p.get("init_level_m", 1)),
				min_level=flt(p.get("min_level_m", 0)),
				max_level=flt(p.get("max_level_m", 10)),
				diameter=flt(p.get("diameter_m", 5)),
				coordinates=(lon, lat),
			)
		node_coords.append((node_id, (lon, lat)))

	skipped_links = []
	usable_link_count = 0
	for f in links:
		geom = f["_geom"]
		if geom.get("type") != "LineString" or not geom.get("coordinates") or len(geom["coordinates"]) < 2:
			skipped_links.append({"name": f["name"], "reason": "not a usable LineString"})
			continue
		coords = geom["coordinates"]
		start = _nearest_node(coords[0], node_coords)
		end = _nearest_node(coords[-1], node_coords)
		if not start or not end:
			bad_end = "start" if not start else "end"
			skipped_links.append({
				"name": f["name"],
				"reason": f"could not snap its {bad_end} to a node within {SNAP_TOLERANCE_M:g}m",
			})
			continue

		p = f["_props"]
		role = f["feature_role"]
		link_id = f["name"]
		length_m = flt(f.get("length_m")) or sum(
			_haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1)
		)
		if role == "Pipe":
			wn.add_pipe(
				link_id, start, end,
				length=length_m,
				diameter=flt(p.get("diameter_mm", 150)) / 1000.0,  # mm -> m
				roughness=flt(p.get("roughness", 100)),
			)
		elif role == "Pump":
			wn.add_pump(
				link_id, start, end,
				pump_type="POWER",
				pump_parameter=flt(p.get("power_kw", 5)) * 1000.0,  # kW -> W (wntr POWER pumps want Watts)
			)
		elif role == "Valve":
			valve_type = str(p.get("valve_type") or "PRV").upper()
			if valve_type not in VALID_VALVE_TYPES:
				skipped_links.append({"name": link_id, "reason": f"unknown valve_type {valve_type!r}"})
				continue
			wn.add_valve(
				link_id, start, end,
				diameter=flt(p.get("diameter_mm", 150)) / 1000.0,
				valve_type=valve_type,
				initial_setting=flt(p.get("initial_setting", 0)),
			)
		usable_link_count += 1

	quality_warning = _apply_network_options(wn, net_doc)

	run_doc = frappe.new_doc("EPANET Simulation Run")
	run_doc.network = network
	run_doc.run_by = frappe.session.user
	run_doc.run_on = now_datetime()
	run_doc.node_count = len(node_coords)
	run_doc.link_count = usable_link_count
	warnings = [f"{s['name']}: {s['reason']}" for s in skipped_links]
	if quality_warning:
		warnings.append(quality_warning)
	if warnings:
		run_doc.warnings = "; ".join(warnings)

	started = dt.now()
	try:
		sim = wntr.sim.EpanetSimulator(wn)
		results = sim.run_sim()
		duration = (dt.now() - started).total_seconds()

		pressure = _last_row(results.node, "pressure")
		head = _last_row(results.node, "head")
		flow = _last_row(results.link, "flowrate")
		velocity = _last_row(results.link, "velocity")
		quality = _last_row(results.node, "quality") if wn.options.quality.parameter != "NONE" else {}

		node_results = {
			name: {"pressure": pressure.get(name), "head": head.get(name), "quality": quality.get(name)}
			for name in pressure
		}
		link_results = {name: {"flow": flow.get(name), "velocity": velocity.get(name)} for name in flow}

		payload = {
			"node_count": run_doc.node_count,
			"link_count": run_doc.link_count,
			"node_results": node_results,
			"link_results": link_results,
			"summary": {
				"min_pressure": min(pressure.values()) if pressure else None,
				"max_pressure": max(pressure.values()) if pressure else None,
				"min_flow": min(flow.values()) if flow else None,
				"max_flow": max(flow.values()) if flow else None,
			},
			"settings": {
				"duration_hours": flt(net_doc.time_duration_hours or 0),
				"demand_model": wn.options.hydraulic.demand_model,
				"quality_mode": wn.options.quality.parameter,
			},
			"skipped_links": skipped_links,
		}

		run_doc.status = "Success"
		run_doc.duration_seconds = duration
		run_doc.results = json.dumps(payload)
		run_doc.save(ignore_permissions=True)
		frappe.db.commit()

		return {"run": run_doc.name, **payload}

	except Exception as e:
		run_doc.status = "Failed"
		run_doc.duration_seconds = (dt.now() - started).total_seconds()
		run_doc.error = str(e)
		run_doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.log_error(title="EPANET simulation failed", message=frappe.get_traceback())
		frappe.throw(_("Simulation failed: {0}").format(str(e)))


def _last_row(result_set, attribute):
	"""results.node/.link['<attribute>'] is a DataFrame indexed by time,
	columns are element names. Returns the last timestep as {name: value},
	or {} if the attribute/table is missing or empty (e.g. no links at
	all means results.link['velocity'] may not exist)."""
	try:
		df = result_set[attribute]
	except KeyError:
		return {}
	if df is None or df.empty:
		return {}
	return df.iloc[-1].to_dict()


@frappe.whitelist()
def get_network_geojson(network):
	"""This network's elements as a single FeatureCollection - lets the
	Map Viewer EPANET plugin draw its own results-colored overlay without
	relying on get_features_geojson's farm/reference_doctype filtering,
	which can't isolate one specific network among several."""
	out = []
	for f in _load_network_features(network):
		if not f["_geom"]:
			continue
		out.append({
			"type": "Feature",
			"geometry": f["_geom"],
			"properties": {
				"_spatial_feature_name": f["name"],
				"_title": f.get("title"),
				"_feature_role": f["feature_role"],
			},
		})
	return {"type": "FeatureCollection", "features": out}


@frappe.whitelist()
def get_last_result(network):
	"""Latest successful run's results, so the map can redisplay them
	without re-running the simulation every time the plugin panel opens."""
	name = frappe.db.get_value(
		"EPANET Simulation Run",
		{"network": network, "status": "Success"},
		"name",
		order_by="run_on desc",
	)
	if not name:
		return None
	doc = frappe.get_doc("EPANET Simulation Run", name)
	payload = json.loads(doc.results or "{}")
	payload["run"] = doc.name
	payload["run_on"] = str(doc.run_on)
	return payload
