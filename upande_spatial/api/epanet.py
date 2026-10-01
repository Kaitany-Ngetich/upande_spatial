# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""EPANET hydraulic simulation for Map Viewer's EPANET plugin.

Network elements (junctions, tanks, reservoirs, pipes, pumps, valves) are
plain Spatial Features, feature_role telling nodes from links apart. Where
they live depends on the network's owner:

- A network owned by a record whose Spatial Entity Config allows these
  roles (e.g. a Farm) simulates THAT RECORD'S OWN assets - the Farm's pipes
  are ordinary Farm features on the Farm's own layers, and the EPANET
  Network doc is just the simulation's settings over them. Several
  networks on the same Farm are several scenarios over the same assets.
New elements always belong to the owner - a network with no owner (or an
owner type with no water-network roles configured) can't take new
elements. Elements saved under the network itself (reference_doctype=
"EPANET Network") by older versions of this app are still loaded, so such
a network keeps simulating as before. Hydraulic attributes (elevation, diameter, demand,
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
import os
import shutil
import tempfile
from datetime import datetime as dt

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

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
	"hyd_headloss", "hyd_demand_multiplier",
	"hyd_flow_units", "hyd_emitter_exponent", "hyd_specific_gravity", "hyd_viscosity",
	"time_duration_hours", "time_hydraulic_timestep_min", "time_pattern_timestep_min",
	"time_report_timestep_min", "time_start_clocktime",
	"time_quality_timestep_min", "time_statistic",
	"qual_mode", "qual_chemical_name", "qual_units", "qual_trace_node",
	"qual_diffusivity", "qual_tolerance",
	"energy_price", "energy_efficiency_pct",
	"react_bulk_coeff", "react_wall_coeff",
	"react_order_bulk", "react_order_tank", "react_order_wall",
	"react_limiting_potential", "react_roughness_correlation",
	"report_status", "report_summary", "report_energy",
)

VALID_FLOW_UNITS = {"LPS", "LPM", "MLD", "CMH", "CFS", "GPM", "MGD", "IMGD", "AFD"}
VALID_SOURCE_TYPES = {"CONCEN", "MASS", "FLOWPACED", "SETPOINT"}
VALID_MIXING_MODELS = {"MIXED", "2COMP", "FIFO", "LIFO"}


class _Quality:
	"""Converts the quality-related numbers people type (mg/L, mg/min,
	1/day, ...) into the SI units wntr holds internally, via wntr's own
	EPANET unit table so the factors can't drift from what EPANET does.
	Concentrations follow the network's chemical units (mg/L or ug/L)."""

	def __init__(self, net_doc):
		from wntr.epanet.util import MassUnits

		self.mode = (net_doc.qual_mode or "None").upper()
		units = (net_doc.qual_units or "mg/L").strip().lower()
		self.mass_units = MassUnits.ug if units.startswith(("ug", "µg")) else MassUnits.mg
		self.bulk_order = flt(net_doc.react_order_bulk) if net_doc.react_order_bulk not in (None, "") else 1.0
		self.tank_order = flt(net_doc.react_order_tank) if net_doc.react_order_tank not in (None, "") else 1.0
		self.wall_order = cint(net_doc.react_order_wall) if net_doc.react_order_wall not in (None, "") else 1

	def _si(self, value, param, order=None):
		from wntr.epanet.util import FlowUnits, to_si

		kwargs = {"mass_units": self.mass_units}
		if order is not None:
			kwargs["reaction_order"] = order
		# Flow units don't enter any quality conversion; LPS is just a placeholder.
		return to_si(FlowUnits.LPS, flt(value), param, **kwargs)

	def initial(self, value):
		"""Initial quality: mg/L for Chemical, hours for Age, % for Trace."""
		from wntr.epanet.util import QualParam

		if self.mode == "CHEMICAL":
			return self._si(value, QualParam.Concentration)
		if self.mode == "AGE":
			return flt(value) * 3600.0
		return flt(value)

	def source_strength(self, source_type, value):
		"""MASS sources are mg/min; every other source type is a concentration."""
		from wntr.epanet.util import QualParam

		param = QualParam.SourceMassInject if source_type == "MASS" else QualParam.Concentration
		return self._si(value, param)

	def concentration(self, value):
		from wntr.epanet.util import QualParam

		return self._si(value, QualParam.Concentration)

	def bulk(self, value, tank=False):
		"""Per day -> per second (scaled by concentration for non-first-order)."""
		from wntr.epanet.util import QualParam

		return self._si(value, QualParam.BulkReactionCoeff, self.tank_order if tank else self.bulk_order)

	def wall(self, value):
		"""First order m/day, zero order mg/m2/day -> SI."""
		from wntr.epanet.util import QualParam

		return self._si(value, QualParam.WallReactionCoeff, self.wall_order)


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
	# 0/blank means "not set" rather than "switch every demand off".
	h.demand_multiplier = flt(net_doc.hyd_demand_multiplier) or 1.0
	# (headloss itself is set in _build_model, before any pipe is added.)
	# 0/blank means "not set" for these too - none of them can be 0 physically.
	h.emitter_exponent = flt(net_doc.hyd_emitter_exponent) or 0.5
	h.specific_gravity = flt(net_doc.hyd_specific_gravity) or 1.0
	h.viscosity = flt(net_doc.hyd_viscosity) or 1.0
	# wntr solves in SI regardless; this is the units EPANET's .inp/.rpt
	# files are written in (wntr's own default is GPM, i.e. a report in feet).
	flow_units = (net_doc.hyd_flow_units or "LPS").upper()
	h.inpfile_units = flow_units if flow_units in VALID_FLOW_UNITS else "LPS"

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
	t.quality_timestep = int(flt(net_doc.time_quality_timestep_min or 5) * 60) or 300
	# Statistic is deliberately NOT handed to EPANET: with one set, EPANET's
	# binary output holds a single aggregated period that wntr's reader can't
	# parse (crashes). The solve keeps every timestep and run_simulation
	# applies the statistic itself - same numbers, and the time slider keeps
	# working. Only the exported .inp carries it (see export_inp).
	t.statistic = "NONE"

	quality_warning = None
	q = wn.options.quality
	q.parameter = (net_doc.qual_mode or "None").upper()
	q.diffusivity = flt(net_doc.qual_diffusivity) if net_doc.qual_diffusivity not in (None, "") else 1.0
	q.tolerance = flt(net_doc.qual_tolerance) or 0.01
	if q.parameter == "CHEMICAL":
		q.chemical_name = net_doc.qual_chemical_name or "Chemical"
		q.inpfile_units = "ug/L" if (net_doc.qual_units or "").strip().lower().startswith(("ug", "µg")) else "mg/L"
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

	# Coefficients are entered per day (as the form says) - wntr wants per
	# second. Before this went through _Quality they were passed straight
	# through, i.e. 86,400x too strong.
	qconv = _Quality(net_doc)
	r = wn.options.reaction
	r.bulk_order = qconv.bulk_order
	r.tank_order = qconv.tank_order
	r.wall_order = qconv.wall_order
	r.bulk_coeff = qconv.bulk(net_doc.react_bulk_coeff or 0)
	r.wall_coeff = qconv.wall(net_doc.react_wall_coeff or 0)
	# wntr writes these two into the .inp verbatim (no unit conversion), so
	# they stay in the user's own units: mg/L and the plain correlation.
	r.limiting_potential = flt(net_doc.react_limiting_potential) if net_doc.react_limiting_potential not in (None, "") else None
	r.roughness_correl = flt(net_doc.react_roughness_correlation) if net_doc.react_roughness_correlation not in (None, "", 0) else None

	rep = wn.options.report
	rep.status = (net_doc.report_status or "Yes").upper()
	rep.summary = (net_doc.report_summary or "Yes").upper()
	rep.energy = (net_doc.report_energy or "No").upper()

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


def _owner_of(network):
	"""(reference_doctype, reference_name) of the record whose own assets
	this network simulates, or (None, None) when its elements belong to the
	network itself."""
	ref_dt, ref_name = frappe.db.get_value("EPANET Network", network, ["reference_doctype", "reference_name"]) or (None, None)
	if ref_dt and ref_name and _owner_accepts(ref_dt, "Pipe", "LineString"):
		return ref_dt, ref_name
	return None, None


def _owner_accepts(doctype, role, geometry_type):
	"""True when doctype's Spatial Entity Config explicitly has a row for
	this role/geometry - an owner type has to opt in to holding water-network
	assets; "no config, anything goes" doesn't count here."""
	if not frappe.db.exists("Spatial Entity Config", doctype):
		return False
	config = frappe.get_cached_doc("Spatial Entity Config", doctype)
	return any(r.feature_role == role and r.geometry_type == geometry_type for r in config.allowed_geometries)


def _load_network_features(network):
	fields = ["name", "title", "feature_role", "geometry", "properties", "length_m"]
	rows = frappe.get_all(
		"Spatial Feature",
		filters={"reference_doctype": "EPANET Network", "reference_name": network},
		fields=fields,
	)
	owner_dt, owner_name = _owner_of(network)
	if owner_dt:
		rows += frappe.get_all(
			"Spatial Feature",
			filters={
				"reference_doctype": owner_dt,
				"reference_name": owner_name,
				"feature_role": ["in", NODE_ROLES + LINK_ROLES],
			},
			fields=fields,
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
	"""Which doctypes an EPANET Network can be scoped to. Thin re-export -
	this is a core "what's spatially trackable" capability, not specific
	to EPANET, so the real implementation lives in api/spatial.py (also
	used by the Map Viewer's own New Feature form) and this just keeps the
	EPANET plugin's existing API_EPANET-prefixed call working unchanged."""
	from upande_spatial.api.spatial import list_owner_doctypes as _list_owner_doctypes

	return _list_owner_doctypes()


@frappe.whitelist()
def list_owner_candidates(doctype):
	"""Thin re-export - see list_owner_doctypes above."""
	from upande_spatial.api.spatial import list_owner_candidates as _list_owner_candidates

	return _list_owner_candidates(doctype)


@frappe.whitelist()
def list_networks():
	"""Every EPANET Network with its element counts by role - drives the
	network picker in the Map Viewer EPANET plugin."""
	networks = frappe.get_all(
		"EPANET Network", fields=["name", "network_name", "reference_doctype", "reference_name"]
	)
	counts = frappe.db.sql(
		"""
		select reference_doctype, reference_name, feature_role, count(*) as n
		from `tabSpatial Feature`
		where feature_role in %(roles)s
		group by reference_doctype, reference_name, feature_role
		""",
		{"roles": NODE_ROLES + LINK_ROLES},
		as_dict=True,
	)
	by_owner = {}
	for row in counts:
		by_owner.setdefault((row.reference_doctype, row.reference_name), {})[row.feature_role] = row.n
	for n in networks:
		element_counts = dict(by_owner.get(("EPANET Network", n["name"]), {}))
		owner = _owner_of(n["name"])
		if owner[0]:
			for role, count in by_owner.get(owner, {}).items():
				element_counts[role] = element_counts.get(role, 0) + count
		n["element_counts"] = element_counts
	return networks


@frappe.whitelist()
def add_element(network, feature_role, geometry, title=None, properties=None):
	"""Creates one new network element (Junction/Tank/Reservoir/Pipe/Pump/
	Valve) as its own Spatial Feature, owned by the network's owner (e.g.
	the Farm) - see module docstring.

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
	owner_dt, owner_name = _owner_of(network)
	if not owner_dt:
		frappe.throw(_(
			"{0} has no owner that holds water-network assets. Set its Reference to e.g. a Farm "
			"(whose Spatial Entity Config has the Junction/Pipe/... roles) first."
		).format(network))
	if not _owner_accepts(owner_dt, feature_role, geometry_type):
		frappe.throw(_("{0} {1} isn't configured to hold {2} {3} features - check Spatial Entity Config.").format(
			owner_dt, owner_name, feature_role, geometry_type
		))
	_validate_against_config(owner_dt, feature_role, geometry_type)

	doc = frappe.new_doc("Spatial Feature")
	doc.reference_doctype = owner_dt
	doc.reference_name = owner_name
	doc.feature_role = feature_role
	if owner_dt == "Farm":
		doc.farm = owner_name
		doc.company = frappe.db.get_value("Farm", owner_name, "company")
	doc.geometry = _as_geojson_feature(geometry, properties)
	doc.properties = json.dumps(properties)
	doc.source_module = "Map Viewer EPANET Plugin"
	if title:
		doc.title = title
	doc.layer = _resolve_layer_name(owner_dt, feature_role, geometry_type)["layer_name"]
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
	if doc.feature_role not in NODE_ROLES + LINK_ROLES:
		frappe.throw(_("{0} is not a water-network element.").format(name))
	doc.delete()
	frappe.db.commit()


@frappe.whitelist()
def update_element(name, properties, title=None):
	"""Replaces one network element's EPANET/asset properties (and
	optionally its title) - the plugin's Edit form. Geometry is untouched;
	moving an element is the Map Viewer's own job."""
	if isinstance(properties, str):
		properties = json.loads(properties) if properties else {}
	doc = frappe.get_doc("Spatial Feature", name)
	if doc.feature_role not in NODE_ROLES + LINK_ROLES:
		frappe.throw(_("{0} is not a water-network element.").format(name))
	from upande_spatial.api.spatial import _as_geojson_feature

	geometry = _first_geometry(doc.geometry)
	doc.properties = json.dumps(properties or {})
	if geometry:
		doc.geometry = _as_geojson_feature(geometry, properties or {})
	if title:
		doc.title = title
	doc.save()
	frappe.db.commit()
	return {"name": doc.name}


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
	Features (plus the EPANET Patterns/Curves they reference and the
	network's own Controls), runs EPANET's hydraulic solver on it, saves an
	EPANET Simulation Run record either way (Success or Failed), and
	returns per-feature results ready for the map to color features by
	(pressure for nodes, flow/velocity for links).

	node_results/link_results are the LAST timestep, same as always, so the
	map keeps working unchanged; `timeseries` holds every report timestep
	for the reports and the map's time slider."""
	import wntr

	net_doc = frappe.get_doc("EPANET Network", network)
	model = _build_model(net_doc)
	wn, warnings, skipped_links, lib = model["wn"], model["warnings"], model["skipped_links"], model["lib"]
	headloss = model["headloss"]

	run_doc = frappe.new_doc("EPANET Simulation Run")
	run_doc.network = network
	run_doc.run_by = frappe.session.user
	run_doc.run_on = now_datetime()
	run_doc.node_count = model["node_count"]
	run_doc.link_count = model["link_count"]
	# Run record keeps skipped links + everything else in one text field;
	# the payload keeps them apart (skipped_links has its own UI line).
	other_warnings = warnings
	warnings = [f"{s['name']}: {s['reason']}" for s in skipped_links] + other_warnings
	if warnings:
		run_doc.warnings = "; ".join(warnings)

	started = dt.now()
	# EpanetSimulator writes <prefix>.inp/.rpt/.bin - into the bench's sites/
	# folder when given a bare prefix, which is where stray temp.* files came
	# from. A private temp dir keeps runs from colliding and cleans up after.
	tmpdir = tempfile.mkdtemp(prefix="epanet-")
	try:
		sim = wntr.sim.EpanetSimulator(wn)
		results = sim.run_sim(file_prefix=os.path.join(tmpdir, "run"))
		duration = (dt.now() - started).total_seconds()
		run_doc.report = _read_report(os.path.join(tmpdir, "run.rpt"))

		pressure = _last_row(results.node, "pressure")
		head = _last_row(results.node, "head")
		flow = _last_row(results.link, "flowrate")
		velocity = _last_row(results.link, "velocity")
		quality = _last_row(results.node, "quality") if wn.options.quality.parameter != "NONE" else {}

		statistic = (net_doc.time_statistic or "None").upper()
		if statistic != "NONE":
			pressure = _stat_row(results.node, "pressure", statistic)
			head = _stat_row(results.node, "head", statistic)
			flow = _stat_row(results.link, "flowrate", statistic)
			velocity = _stat_row(results.link, "velocity", statistic)
			if quality:
				quality = _stat_row(results.node, "quality", statistic)
		if quality:
			# wntr hands back SI (kg/m3, seconds); show it in the units it was entered in.
			factor = _quality_display_factor(wn)
			quality = {k: (v * factor if v is not None else None) for k, v in quality.items()}

		node_results = {
			name: {"pressure": pressure.get(name), "head": head.get(name), "quality": quality.get(name)}
			for name in pressure
		}
		link_results = {name: {"flow": flow.get(name), "velocity": velocity.get(name)} for name in flow}
		timeseries = _timeseries(wn, results)
		junctions = set(wn.junction_name_list)
		all_pressures = [v for name, series in timeseries["node"]["pressure"].items() if name in junctions for v in series if v is not None]
		# EPANET still "solves" a junction that a closed link has cut off
		# from every source, but its pressure comes out as a meaningless huge
		# negative number - flag it rather than let it pass as a result.
		cut_off = sorted(
			name for name, series in timeseries["node"]["pressure"].items()
			if name in junctions and any(v is not None and v < -100 for v in series)
		)
		if cut_off:
			msg = (
				f"{len(cut_off)} junction(s) cut off from supply at some timestep (check closed links/controls): "
				+ ", ".join(cut_off[:10]) + (" ..." if len(cut_off) > 10 else "")
			)
			other_warnings.append(msg)
			warnings.append(msg)
			run_doc.warnings = "; ".join(warnings)

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
				# Across every timestep, junctions only (a reservoir's
				# "pressure" is always 0 and would mask the real minimum).
				"min_junction_pressure_all_steps": min(all_pressures) if all_pressures else None,
				"max_junction_pressure_all_steps": max(all_pressures) if all_pressures else None,
				"pump_energy_kwh": timeseries.pop("pump_energy_kwh"),
				"pump_cost": timeseries.pop("pump_cost"),
			},
			"settings": {
				"duration_hours": flt(net_doc.time_duration_hours or 0),
				"demand_model": wn.options.hydraulic.demand_model,
				"headloss": headloss,
				"demand_multiplier": wn.options.hydraulic.demand_multiplier,
				"flow_units": wn.options.hydraulic.inpfile_units,
				"statistic": statistic,
				"quality_mode": wn.options.quality.parameter,
				"quality_units": _quality_units_label(wn),
				"patterns": sorted(lib.patterns),
				"curves": sorted(lib.curves),
				"controls": len(wn.control_name_list),
				"sources": len(wn.source_name_list),
				"excluded": model["excluded"],
				"start_clocktime_s": int(wn.options.time.start_clocktime or 0),
			},
			"element_types": {
				**{n: "Junction" for n in wn.junction_name_list},
				**{n: "Tank" for n in wn.tank_name_list},
				**{n: "Reservoir" for n in wn.reservoir_name_list},
				**{n: "Pipe" for n in wn.pipe_name_list},
				**{n: "Pump" for n in wn.pump_name_list},
				**{n: "Valve" for n in wn.valve_name_list},
			},
			"timeseries": timeseries,
			"skipped_links": skipped_links,
			"warnings": other_warnings,
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
		run_doc.report = run_doc.report or _read_report(os.path.join(tmpdir, "run.rpt"))
		run_doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.log_error(title="EPANET simulation failed", message=frappe.get_traceback())
		frappe.throw(_("Simulation failed: {0}").format(str(e)))
	finally:
		shutil.rmtree(tmpdir, ignore_errors=True)


@frappe.whitelist()
def export_inp(network):
	"""Downloads this network as a standard EPANET .inp file - opens in EPA's
	own EPANET desktop app, QGIS's QWater/"EPANET" plugins, WNTR etc. Written
	in the network's Flow Units, and (unlike the solve, see
	_apply_network_options) with its Statistic option included."""
	net_doc = frappe.get_doc("EPANET Network", network)
	model = _build_model(net_doc)
	wn = model["wn"]
	statistic = (net_doc.time_statistic or "None").upper()
	wn.options.time.statistic = statistic if statistic in {"AVERAGED", "MINIMUM", "MAXIMUM", "RANGE"} else "NONE"

	import wntr

	tmpdir = tempfile.mkdtemp(prefix="epanet-")
	try:
		path = os.path.join(tmpdir, "network.inp")
		wntr.network.write_inpfile(wn, path, units=wn.options.hydraulic.inpfile_units)
		with open(path, "rb") as fh:
			content = fh.read()
	finally:
		shutil.rmtree(tmpdir, ignore_errors=True)

	safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (net_doc.network_name or network))
	frappe.response["filename"] = f"{safe}.inp"
	frappe.response["filecontent"] = content
	frappe.response["type"] = "download"


def _read_report(path):
	try:
		with open(path, encoding="utf-8", errors="replace") as fh:
			# A Full status report on a long run can get big; keep the record sane.
			return fh.read()[:500_000]
	except OSError:
		return None


def _quality_display_factor(wn):
	"""SI quality -> the units it's entered/shown in: mg/L (or ug/L), hours, %."""
	mode = wn.options.quality.parameter
	if mode == "CHEMICAL":
		return 1e6 if wn.options.quality.inpfile_units == "ug/L" else 1e3
	if mode == "AGE":
		return 1 / 3600.0
	return 1.0


def _quality_units_label(wn):
	mode = wn.options.quality.parameter
	if mode == "CHEMICAL":
		return wn.options.quality.inpfile_units
	if mode == "AGE":
		return "hours"
	if mode == "TRACE":
		return "%"
	return ""


def _stat_row(result_set, attribute, statistic):
	"""Per-element statistic over every timestep, as {name: value} - what
	EPANET's own STATISTIC option would report."""
	try:
		df = result_set[attribute]
	except KeyError:
		return {}
	if df is None or df.empty:
		return {}
	if statistic == "AVERAGED":
		row = df.mean()
	elif statistic == "MINIMUM":
		row = df.min()
	elif statistic == "MAXIMUM":
		row = df.max()
	elif statistic == "RANGE":
		row = df.max() - df.min()
	else:
		row = df.iloc[-1]
	return row.to_dict()


def _in_model(props):
	"""WN's "Include in Model": blank/missing means yes, so every element
	saved before this existed stays in."""
	value = props.get("in_model")
	return value in (None, "") or bool(cint(value))


def _apply_node_quality(wn, node_id, p, qconv, lib, warnings):
	"""Initial quality + an optional quality source on any node."""
	if qconv.mode == "NONE":
		return
	node = wn.get_node(node_id)
	if p.get("initial_quality") not in (None, ""):
		node.initial_quality = qconv.initial(p.get("initial_quality"))
	source_type = str(p.get("source_type") or "").upper()
	if not source_type:
		return
	if qconv.mode != "CHEMICAL":
		warnings.append(f"{node_id}: quality source ignored - sources only apply to Chemical analysis")
		return
	if source_type not in VALID_SOURCE_TYPES:
		warnings.append(f"{node_id}: unknown source type {source_type!r} - ignored")
		return
	wn.add_source(
		f"SRC-{node_id}", node_id, source_type,
		qconv.source_strength(source_type, p.get("source_strength", 0)),
		lib.pattern(p.get("source_pattern"), node_id),
	)


def _build_model(net_doc):
	"""Builds the wntr WaterNetworkModel for one EPANET Network from its
	Spatial Features (plus the EPANET Patterns/Curves they reference and the
	network's own Controls). Shared by run_simulation and export_inp so the
	simulated and the exported network can never differ."""
	import wntr

	network = net_doc.name
	features = _load_network_features(network)
	excluded = sum(1 for f in features if not _in_model(f["_props"]))
	features = [f for f in features if _in_model(f["_props"])]
	nodes = [f for f in features if f["feature_role"] in NODE_ROLES and f["_geom"]]
	links = [f for f in features if f["feature_role"] in LINK_ROLES and f["_geom"]]

	if not nodes:
		frappe.throw(_("This network has no Junction/Tank/Reservoir features yet."))

	wn = wntr.network.WaterNetworkModel()
	# Headloss formula has to be set before any pipe exists - wntr converts
	# existing pipes' roughness when it changes afterwards.
	headloss = net_doc.hyd_headloss or "H-W"
	wn.options.hydraulic.headloss = headloss
	# Quality mode is needed while adding nodes (initial quality / sources);
	# _apply_network_options sets the rest of it afterwards.
	wn.options.quality.parameter = (net_doc.qual_mode or "None").upper()
	qconv = _Quality(net_doc)
	warnings = []
	lib = _ModelLibrary(wn, warnings, net_doc)
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
				demand_pattern=lib.pattern(p.get("demand_pattern"), node_id),
				elevation=flt(p.get("elevation_m", 0)),
				coordinates=(lon, lat),
				demand_category=p.get("demand_category") or None,
			)
			if flt(p.get("emitter_coeff")):
				# L/s per m^0.5 -> m3/s per m^0.5
				wn.get_node(node_id).emitter_coefficient = flt(p.get("emitter_coeff")) / 1000.0
		elif role == "Reservoir":
			wn.add_reservoir(
				node_id,
				base_head=flt(p.get("base_head_m", 0)),
				head_pattern=lib.pattern(p.get("head_pattern"), node_id),
				coordinates=(lon, lat),
			)
		elif role == "Tank":
			vol_curve = lib.curve(p.get("vol_curve"), "Volume", node_id)
			max_level = flt(p.get("max_level_m", 10))
			if vol_curve:
				# EPANET rejects a tank whose max level lies beyond its volume
				# curve (Error 225) - including by the rounding the INP file
				# writes curve points with, when max level == the curve's last
				# depth. Keep it just inside the curve.
				curve_top = wn.get_curve(vol_curve).points[-1][0]
				if max_level > curve_top + 0.01:
					warnings.append(f"{node_id}: max level {max_level:g} m is above its volume curve ({curve_top:g} m) - capped")
				max_level = min(max_level, curve_top - 0.001)
			wn.add_tank(
				node_id,
				elevation=flt(p.get("elevation_m", 0)),
				init_level=flt(p.get("init_level_m", 1)),
				min_level=flt(p.get("min_level_m", 0)),
				max_level=max_level,
				diameter=flt(p.get("diameter_m", 5)),
				min_vol=flt(p.get("min_vol_m3", 0)),
				vol_curve=vol_curve,
				overflow=bool(cint(p.get("overflow", 0))),
				coordinates=(lon, lat),
			)
			tank = wn.get_node(node_id)
			mixing = str(p.get("mixing_model") or "").upper()
			if mixing and mixing not in VALID_MIXING_MODELS:
				warnings.append(f"{node_id}: unknown mixing model {mixing!r} - using MIXED")
			elif mixing:
				tank.mixing_model = mixing
				if mixing == "2COMP":
					# Fraction of the tank's volume that's the inlet/outlet zone.
					tank.mixing_fraction = flt(p.get("mixing_fraction")) or 1.0
			if p.get("bulk_coeff") not in (None, ""):
				tank.bulk_coeff = qconv.bulk(p.get("bulk_coeff"), tank=True)
		_apply_node_quality(wn, node_id, p, qconv, lib, warnings)
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
		length_m = flt(p.get("length_override_m")) or flt(f.get("length_m")) or sum(
			_haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1)
		)
		status = str(p.get("initial_status") or "").upper()
		if role == "Pipe":
			roughness = flt(p.get("roughness", 100))
			if headloss == "D-W":
				roughness = roughness / 1000.0  # entered in mm, wntr wants m
			wn.add_pipe(
				link_id, start, end,
				length=length_m,
				diameter=flt(p.get("diameter_mm", 150)) / 1000.0,  # mm -> m
				roughness=roughness,
				minor_loss=flt(p.get("minor_loss", 0)),
				initial_status="CLOSED" if status == "CLOSED" else "OPEN",
				check_valve=status == "CV",
			)
			pipe = wn.get_link(link_id)
			# Blank = use the network's global coefficient (EPANET's own rule).
			if p.get("bulk_coeff") not in (None, ""):
				pipe.bulk_coeff = qconv.bulk(p.get("bulk_coeff"))
			if p.get("wall_coeff") not in (None, ""):
				pipe.wall_coeff = qconv.wall(p.get("wall_coeff"))
		elif role == "Pump":
			pump_curve = lib.curve(p.get("pump_curve"), "Pump", link_id)
			wn.add_pump(
				link_id, start, end,
				# A head curve when one's given (a real pump), otherwise the
				# constant-power approximation this plugin always used.
				pump_type="HEAD" if pump_curve else "POWER",
				pump_parameter=pump_curve or flt(p.get("power_kw", 5)) * 1000.0,  # kW -> W
				speed=flt(p.get("speed", 1)) or 1.0,
				pattern=lib.pattern(p.get("speed_pattern"), link_id),
				initial_status="CLOSED" if status == "CLOSED" else "OPEN",
			)
			pump = wn.get_link(link_id)
			efficiency_curve = lib.curve(p.get("efficiency_curve"), "Efficiency", link_id)
			if efficiency_curve:
				pump.efficiency_curve_name = efficiency_curve
			if flt(p.get("energy_price")):
				pump.energy_price = flt(p.get("energy_price"))
			energy_pattern = lib.pattern(p.get("energy_pattern"), link_id)
			if energy_pattern:
				pump.energy_pattern = energy_pattern
		elif role == "Valve":
			valve_type = str(p.get("valve_type") or "PRV").upper()
			if valve_type not in VALID_VALVE_TYPES:
				skipped_links.append({"name": link_id, "reason": f"unknown valve_type {valve_type!r}"})
				continue
			if valve_type == "GPV":
				# A GPV's "setting" is its headloss curve, not a number.
				setting = lib.curve(p.get("headloss_curve"), "Headloss", link_id)
				if not setting:
					skipped_links.append({"name": link_id, "reason": "GPV valve needs a headloss_curve"})
					continue
			else:
				setting = flt(p.get("initial_setting", 0))
				if valve_type == "FCV":
					setting = setting / 1000.0  # L/s -> m3/s
			wn.add_valve(
				link_id, start, end,
				diameter=flt(p.get("diameter_mm", 150)) / 1000.0,
				valve_type=valve_type,
				minor_loss=flt(p.get("minor_loss", 0)),
				initial_setting=setting,
				initial_status=status if status in ("OPEN", "CLOSED") else "ACTIVE",
			)
		usable_link_count += 1

	quality_warning = _apply_network_options(wn, net_doc)
	if quality_warning:
		warnings.append(quality_warning)
	_apply_controls(wn, net_doc, warnings)

	return {
		"wn": wn,
		"lib": lib,
		"warnings": warnings,
		"skipped_links": skipped_links,
		"headloss": headloss,
		"node_count": len(node_coords),
		"link_count": usable_link_count,
		"excluded": excluded,
	}


class _ModelLibrary:
	"""Adds EPANET Patterns / Curves to the model on first use, by name, so
	only the ones this network's elements actually reference get loaded. A
	reference to a pattern/curve that doesn't exist (or a curve of the wrong
	type) becomes a warning and is ignored - same "one bad element
	shouldn't block the rest" rule as skipped links.

	EPANET ids must be under 32 characters with no spaces, which a
	human-named "Kapkolia Irrigation Schedule" isn't - so each gets a short
	model id (PAT1, CUR1, ...) and pattern()/curve() return that id;
	`patterns`/`curves` keep the real names for the run's settings."""

	# Curve X is entered in L/s for flow-based curves; wntr wants m3/s.
	CURVES = {
		"Pump": ("HEAD", 1000.0),
		"Efficiency": ("EFFICIENCY", 1000.0),
		"Volume": ("VOLUME", 1.0),
		"Headloss": ("HEADLOSS", 1000.0),
	}

	def __init__(self, wn, warnings, net_doc):
		self.wn = wn
		self.warnings = warnings
		self.net_doc = net_doc
		self.patterns = {}  # EPANET Pattern name -> model id
		self.curves = {}  # EPANET Curve name -> model id

	def pattern(self, name, used_by):
		if not name:
			return None
		if name in self.patterns:
			return self.patterns[name]
		if not frappe.db.exists("EPANET Pattern", name):
			self.warnings.append(f"{used_by}: pattern {name!r} not found - ignored")
			return None
		doc = frappe.get_cached_doc("EPANET Pattern", name)
		model_id = f"PAT{len(self.patterns) + 1}"
		self.wn.add_pattern(model_id, self._resampled(doc, used_by))
		self.patterns[name] = model_id
		return model_id

	def _resampled(self, doc, used_by):
		"""EPANET has ONE pattern timestep for the whole network; a pattern
		with its own Time Step (WN-style) is stretched onto it - each
		multiplier repeated for as many network steps as it lasts. Called
		before _apply_network_options, so the step is read off the doc."""
		values = [flt(r.multiplier) for r in doc.multipliers]
		step_h = flt(doc.get("time_step_hours"))
		network_step_h = flt(self.net_doc.time_pattern_timestep_min or 60) / 60.0
		if not step_h or not values or abs(step_h - network_step_h) < 1e-9:
			return values
		ratio = step_h / network_step_h
		if ratio < 1 or abs(ratio - round(ratio)) > 1e-6:
			self.warnings.append(
				f"{used_by}: pattern {doc.name!r} step ({step_h:g} h) isn't a whole multiple of the network's "
				f"pattern timestep ({network_step_h:g} h) - used as if it were {network_step_h:g} h"
			)
			return values
		return [v for v in values for _ in range(int(round(ratio)))]

	def curve(self, name, curve_type, used_by):
		if not name:
			return None
		if name in self.curves:
			return self.curves[name]
		if not frappe.db.exists("EPANET Curve", name):
			self.warnings.append(f"{used_by}: curve {name!r} not found - ignored")
			return None
		doc = frappe.get_cached_doc("EPANET Curve", name)
		if doc.curve_type != curve_type:
			self.warnings.append(f"{used_by}: curve {name!r} is a {doc.curve_type} curve, needs {curve_type} - ignored")
			return None
		wntr_type, x_div = self.CURVES[curve_type]
		model_id = f"CUR{len(self.curves) + 1}"
		self.wn.add_curve(model_id, wntr_type, [(flt(r.x) / x_div, flt(r.y)) for r in doc.points])
		self.curves[name] = model_id
		return model_id


def _apply_controls(wn, net_doc, warnings):
	"""EPANET Network's Controls table -> wntr simple controls. Elements are
	referenced by their Spatial Feature name, which is also their id in
	the model; a row pointing at something not in this network is skipped
	with a warning."""
	import numpy as np
	from wntr.network.controls import Control, ControlAction, LinkStatus

	for row in net_doc.get("controls") or []:
		if not cint(row.enabled):
			continue
		label = f"Control row {row.idx}"
		if row.target_element not in wn.link_name_list:
			warnings.append(f"{label}: {row.target_element or '(blank)'} is not a pipe/pump/valve in this network - skipped")
			continue
		link = wn.get_link(row.target_element)
		if row.action == "Open":
			action = ControlAction(link, "status", LinkStatus.Open)
		elif row.action == "Closed":
			action = ControlAction(link, "status", LinkStatus.Closed)
		else:
			value = flt(row.setting)
			if link.link_type == "Valve" and link.valve_type == "FCV":
				value = value / 1000.0  # L/s -> m3/s
			action = ControlAction(link, "setting", value)

		if row.trigger == "At Clock Time":
			if row.clock_time is None:
				warnings.append(f"{label}: no clock time - skipped")
				continue
			seconds = _time_to_seconds(row.clock_time)
			control = Control._time_control(wn, seconds, "CLOCK_TIME", True, action)
		elif row.trigger == "After Elapsed Time":
			control = Control._time_control(wn, flt(row.elapsed_hours) * 3600, "SIM_TIME", False, action)
		else:
			if row.source_node not in wn.node_name_list or row.source_node in wn.reservoir_name_list:
				warnings.append(f"{label}: {row.source_node or '(blank)'} is not a junction/tank in this network - skipped")
				continue
			node = wn.get_node(row.source_node)
			attribute = "level" if row.source_node in wn.tank_name_list else "pressure"
			operation = np.less if row.trigger == "Node Below" else np.greater
			control = Control._conditional_control(node, attribute, operation, flt(row.threshold), action)
		wn.add_control(f"control-{row.idx}", control)


def _time_to_seconds(value):
	if hasattr(value, "hour"):
		return value.hour * 3600 + value.minute * 60 + value.second
	if hasattr(value, "total_seconds"):
		return value.total_seconds()
	h, m, *s = str(value).split(":")
	return int(h) * 3600 + int(m) * 60 + int(float(s[0]) if s else 0)


def _pump_power_kw(wn, results, name):
	"""Electrical power drawn per report timestep, kW. Done here rather than
	wntr.metrics.pump_power, which refuses pumps with an efficiency curve:
	power = rho*g*Q*head_gain / efficiency, efficiency read off the pump's
	own curve at that timestep's flow when it has one, else the network's
	global efficiency. A POWER pump just draws its fixed rating while on."""
	import numpy as np

	pump = wn.get_link(name)
	try:
		q = results.link["flowrate"][name]
	except KeyError:
		return None
	if pump.pump_type == "POWER":
		return (q.abs() > 1e-9) * (pump.power / 1000.0)
	head = results.node["head"]
	gain = (head[pump.end_node_name] - head[pump.start_node_name]).clip(lower=0)
	curve = pump.efficiency_curve
	if curve:
		xs, ys = zip(*curve.points)
		eff = np.interp(q.abs(), xs, ys) / 100.0
	else:
		eff = (wn.options.energy.global_efficiency or 75) / 100.0
	eff = np.maximum(eff, 0.05)  # guard against a curve dropping to ~0 at the ends
	return (1000.0 * 9.81 * q.clip(lower=0) * gain / eff / 1000.0).fillna(0)


def _timeseries(wn, results):
	"""Every report timestep, compact: {"times": [seconds...], "node":
	{attr: {name: [values...]}}, "link": {...}, "pump_power_kw": {...}},
	plus per-pump energy/cost totals (popped into the summary by the
	caller). Values are rounded - 5 decimals of m3/s is 0.01 L/s."""
	def table(result_set, attribute, digits=5):
		try:
			df = result_set[attribute]
		except KeyError:
			return {}
		if df is None or df.empty:
			return {}
		df = df.round(digits)
		return {name: [None if v != v else v for v in df[name].tolist()] for name in df.columns}

	pressure_df = results.node.get("pressure")
	times = [int(t) for t in pressure_df.index] if pressure_df is not None else []
	out = {
		"times": times,
		"node": {
			"pressure": table(results.node, "pressure", 3),
			"head": table(results.node, "head", 3),
			"demand": table(results.node, "demand"),
		},
		"link": {
			"flow": table(results.link, "flowrate"),
			"velocity": table(results.link, "velocity", 3),
			"headloss": table(results.link, "headloss", 6),
			"status": table(results.link, "status", 0),
		},
		"pump_power_kw": {},
		"pump_energy_kwh": {},
		"pump_cost": {},
	}
	if wn.options.quality.parameter != "NONE":
		out["node"]["quality"] = table(results.node, "quality", 9)
		factor = _quality_display_factor(wn)
		out["node"]["quality"] = {
			name: [None if v is None else round(v * factor, 4) for v in series]
			for name, series in out["node"]["quality"].items()
		}

	if wn.pump_name_list and times:
		step = wn.options.time.report_timestep or 3600
		global_price = wn.options.energy.global_price or 0
		for name in wn.pump_name_list:
			series = _pump_power_kw(wn, results, name)
			if series is None:
				continue
			out["pump_power_kw"][name] = [round(v, 3) for v in series.tolist()]
			kwh = float(series.sum()) * step / 3600.0 if len(times) > 1 else 0.0
			out["pump_energy_kwh"][name] = round(kwh, 3)
			price = wn.get_link(name).energy_price or global_price
			out["pump_cost"][name] = round(kwh * price, 2)
	return out
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
def list_library(network=None):
	"""EPANET Patterns and Curves this network can use - its own plus the
	shared ones (no network set) - for the plugin's element forms to offer
	as dropdowns. Curves are grouped by type."""
	# Unset network is NULL, which an "in" filter never matches - or_filters.
	scope = [["network", "=", network], ["network", "is", "not set"]] if network else None
	patterns = frappe.get_all("EPANET Pattern", or_filters=scope, pluck="name", order_by="name")
	curves = {t: [] for t in ("Pump", "Efficiency", "Volume", "Headloss")}
	for c in frappe.get_all("EPANET Curve", or_filters=scope, fields=["name", "curve_type"], order_by="name"):
		curves.setdefault(c.curve_type, []).append(c.name)
	return {"patterns": patterns, "curves": curves}


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
				"_props": f["_props"],
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


# ─────────────────────────────  Shared by the EPANET reports  ─────────────────────────────

LINK_STATUS_LABELS = {0: "Closed", 1: "Open", 2: "Active", 3: "CV"}


def load_run(network, run=None):
	"""(EPANET Simulation Run doc, parsed results payload) for `run`, or for
	the network's latest successful run when `run` isn't given."""
	if not run:
		run = frappe.db.get_value(
			"EPANET Simulation Run", {"network": network, "status": "Success"}, "name", order_by="run_on desc"
		)
		if not run:
			frappe.throw(_("{0} has no successful simulation run yet.").format(network))
	doc = frappe.get_doc("EPANET Simulation Run", run)
	if doc.network != network:
		frappe.throw(_("{0} is a run of {1}, not {2}.").format(run, doc.network, network))
	if doc.status != "Success":
		frappe.throw(_("{0} did not succeed: {1}").format(run, doc.error or ""))
	return doc, json.loads(doc.results or "{}")


def network_elements(network):
	"""{spatial_feature_name: row} for every element the network simulates,
	with its parsed properties under "_props"."""
	return {f["name"]: f for f in _load_network_features(network)}


def pick_step(payload, at_hour=None):
	"""Index into the timeseries for `at_hour` hours after the start (the
	nearest report step), or the last step when not given. None for runs
	saved before timeseries existed."""
	times = (payload.get("timeseries") or {}).get("times") or []
	if not times:
		return None
	if at_hour in (None, ""):
		return len(times) - 1
	target = flt(at_hour) * 3600
	return min(range(len(times)), key=lambda i: abs(times[i] - target))


def clock_label(payload, seconds):
	"""Elapsed seconds -> "HH:MM" wall-clock time, honouring the network's
	start clocktime; prefixed with the day once the run passes midnight."""
	start = (payload.get("settings") or {}).get("start_clocktime_s") or 0
	total = int(start + seconds)
	day, rem = divmod(total, 86400)
	label = f"{rem // 3600:02d}:{rem % 3600 // 60:02d}"
	return f"D{day + 1} {label}" if day else label
