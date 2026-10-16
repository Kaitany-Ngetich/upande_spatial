"""Bring simulation results computed elsewhere (QGIS + Gusnet) into ERP as
an EPANET Simulation Run, so they get the same Overview, Node/Link Results
reports, Map Viewer / QGIS result layers and "Plan inspection" as runs
computed in ERP.

Gusnet keeps ERP's element names (SPF-...) when it imports the network's
.inp export, so results map straight back onto the network's Spatial
Features. Gusnet writes results in the run's flow units (pressure/head in
m for SI flow units, psi/ft for US ones); everything is stored here in
ERP's units: m, m3/s, m/s."""

import json

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

# flow unit -> m3/s
FLOW_TO_M3S = {
	"LPS": 1e-3, "LPM": 1e-3 / 60, "MLD": 1e3 / 86400, "CMH": 1 / 3600, "CMD": 1 / 86400, "CMS": 1.0,
	"CFS": 0.0283168, "GPM": 6.30902e-5, "MGD": 0.0438126, "IMGD": 0.0526168, "AFD": 0.0142764,
}
US_UNITS = {"CFS", "GPM", "MGD", "IMGD", "AFD"}
PSI_TO_M, FT_TO_M = 0.70307, 0.3048
ROLE_TYPES = {"Junction": "Junction", "Tank": "Tank", "Reservoir": "Reservoir", "Pipe": "Pipe", "Pump": "Pump", "Valve": "Valve"}


def _series(values, factor=1.0):
	if values is None:
		return []
	if not isinstance(values, (list, tuple)):
		values = [values]
	return [None if v is None else flt(v) * factor for v in values]


@frappe.whitelist()
def import_results(network, nodes, links=None, flow_units="LPS", source="QGIS (Gusnet)", note=None):
	"""Save results as a new Success run of `network`.

	nodes = {element_name: {"pressure": [...], "head": [...], "demand": [...]}}
	links = {element_name: {"flow": [...], "velocity": [...]}}
	one value per reported timestep (a single value = steady state).
	Elements that aren't part of the network are skipped and listed in the
	run's warnings. Returns {"run", "matched_nodes", "matched_links", "skipped"}."""
	from upande_spatial.api.epanet import network_elements

	frappe.has_permission("EPANET Network", "write", network, throw=True)
	if isinstance(nodes, str):
		nodes = json.loads(nodes or "{}")
	if isinstance(links, str):
		links = json.loads(links or "{}")
	nodes, links = nodes or {}, links or {}
	units = (flow_units or "LPS").upper()
	if units not in FLOW_TO_M3S:
		frappe.throw(_("Unknown flow units {0} - use one of {1}.").format(units, ", ".join(FLOW_TO_M3S)))
	qf = FLOW_TO_M3S[units]
	pf, hf = (PSI_TO_M, FT_TO_M) if units in US_UNITS else (1.0, 1.0)

	elements = network_elements(network)
	types = {n: ROLE_TYPES[e["feature_role"]] for n, e in elements.items() if e.get("feature_role") in ROLE_TYPES}
	node_names = [n for n in nodes if types.get(n) in ("Junction", "Tank", "Reservoir")]
	link_names = [n for n in links if types.get(n) in ("Pipe", "Pump", "Valve")]
	skipped = sorted(set(nodes) - set(node_names)) + sorted(set(links) - set(link_names))
	if not node_names:
		frappe.throw(_("None of the uploaded nodes belong to {0}. Import the network's own .inp export into Gusnet so the element names (SPF-...) are kept.").format(network))

	ts_node = {"pressure": {}, "head": {}, "demand": {}}
	ts_link = {"flow": {}, "velocity": {}}
	for n in node_names:
		r = nodes[n] or {}
		ts_node["pressure"][n] = _series(r.get("pressure"), pf)
		ts_node["head"][n] = _series(r.get("head"), hf)
		ts_node["demand"][n] = _series(r.get("demand"), qf)
	for n in link_names:
		r = links[n] or {}
		ts_link["flow"][n] = _series(r.get("flow"), qf)
		ts_link["velocity"][n] = _series(r.get("velocity"), FT_TO_M if units in US_UNITS else 1.0)

	steps = max([len(v) for d in (*ts_node.values(), *ts_link.values()) for v in d.values()] or [1])
	net = frappe.get_doc("EPANET Network", network)
	duration_h = flt(net.time_duration_hours or 0)
	step_s = (duration_h * 3600 / (steps - 1)) if steps > 1 and duration_h else flt(net.time_report_timestep_min or 60) * 60
	times = [int(round(i * step_s)) for i in range(steps)]

	last = lambda s: next((v for v in reversed(s or []) if v is not None), None)  # noqa: E731
	node_results = {n: {"pressure": last(ts_node["pressure"][n]), "head": last(ts_node["head"][n]), "quality": None} for n in node_names}
	link_results = {n: {"flow": last(ts_link["flow"][n]), "velocity": last(ts_link["velocity"][n])} for n in link_names}
	junction_p = [v for n in node_names if types[n] == "Junction" for v in ts_node["pressure"][n] if v is not None]
	final_p = [r["pressure"] for r in node_results.values() if r["pressure"] is not None]
	final_q = [r["flow"] for r in link_results.values() if r["flow"] is not None]

	warnings = []
	if skipped:
		warnings.append(f"{len(skipped)} uploaded element(s) are not in {network} and were skipped: " + ", ".join(skipped[:15]) + ("…" if len(skipped) > 15 else ""))
	missing = sorted(n for n, t in types.items() if t in ("Junction", "Tank", "Reservoir") and n not in nodes)
	if missing:
		warnings.append(f"{len(missing)} network node(s) had no results in the upload: " + ", ".join(missing[:15]) + ("…" if len(missing) > 15 else ""))
	warnings.append("Headloss and water quality are not part of an upload from QGIS.")

	payload = {
		"node_count": len(node_names),
		"link_count": len(link_names),
		"node_results": node_results,
		"link_results": link_results,
		"summary": {
			"min_pressure": min(final_p) if final_p else None,
			"max_pressure": max(final_p) if final_p else None,
			"min_flow": min(final_q) if final_q else None,
			"max_flow": max(final_q) if final_q else None,
			"min_junction_pressure_all_steps": min(junction_p) if junction_p else None,
			"max_junction_pressure_all_steps": max(junction_p) if junction_p else None,
			"pump_energy_kwh": {},
			"pump_cost": {},
		},
		"settings": {
			"duration_hours": duration_h if steps > 1 else 0,
			"demand_model": net.get("hyd_demand_model") or "",
			"headloss": net.get("hyd_headloss") or "",
			"flow_units": units,
			"statistic": "NONE",
			"quality_mode": "NONE",
			"quality_units": "",
			"source": source,
		},
		"element_types": {n: types[n] for n in node_names + link_names},
		"timeseries": {"times": times, "node": ts_node, "link": ts_link, "pump_power_kw": {}} if steps > 1 else {},
		"skipped_links": [],
		"warnings": warnings,
	}
	run = frappe.new_doc("EPANET Simulation Run")
	run.update({
		"network": network, "status": "Success", "run_by": frappe.session.user, "run_on": now_datetime(),
		"node_count": len(node_names), "link_count": len(link_names), "duration_seconds": 0,
		"warnings": "; ".join(warnings), "results": json.dumps(payload),
		"report": f"Results uploaded from {source} by {frappe.session.user}."
			+ (f"\nNote: {note}" if note else "")
			+ f"\n{len(node_names)} nodes, {len(link_names)} links, {steps} timestep(s), flow units {units}.",
	})
	if run.meta.has_field("source"):
		run.source = source
	run.insert(ignore_permissions=True)
	return {"run": run.name, "matched_nodes": len(node_names), "matched_links": len(link_names), "skipped": len(skipped)}
