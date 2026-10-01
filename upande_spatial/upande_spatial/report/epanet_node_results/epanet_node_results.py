# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""EPANET Node Results - junction pressures, tank levels and reservoir
heads from one simulation run (the latest successful one by default), at a
chosen hour plus min/max over the whole run. The chart shows how pressure
(or, for tanks, water level) moves through the run."""

import frappe
from frappe import _
from frappe.utils import flt

from upande_spatial.api.epanet import clock_label, load_run, network_elements, pick_step

NODE_TYPES = ("Junction", "Tank", "Reservoir")
MAX_CHART_SERIES = 8


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.network:
		return [], []
	run_doc, payload = load_run(filters.network, filters.run)
	types = [filters.element_type] if filters.element_type else list(NODE_TYPES)
	min_ok = flt(filters.min_pressure) if filters.min_pressure not in (None, "") else None

	ts = payload.get("timeseries") or {}
	times = ts.get("times") or []
	step = pick_step(payload, filters.at_hour)
	at_label = clock_label(payload, times[step]) if step is not None else _("final")
	series = ts.get("node") or {}
	final = payload.get("node_results") or {}

	def value(attr, name):
		if step is not None:
			vals = (series.get(attr) or {}).get(name)
			return vals[step] if vals else None
		return (final.get(name) or {}).get(attr)

	def extreme(attr, name, fn):
		vals = [v for v in ((series.get(attr) or {}).get(name) or []) if v is not None]
		return fn(vals) if vals else value(attr, name)

	columns = [
		{"label": _("Element"), "fieldname": "element", "fieldtype": "Link", "options": "Spatial Feature", "width": 120},
		{"label": _("Title"), "fieldname": "title", "fieldtype": "Data", "width": 100},
		{"label": _("Type"), "fieldname": "element_type", "fieldtype": "Data", "width": 90},
		{"label": _("Elevation (m)"), "fieldname": "elevation", "fieldtype": "Float", "precision": 2, "width": 105},
		{"label": _("Base Demand (L/s)"), "fieldname": "base_demand", "fieldtype": "Float", "precision": 3, "width": 125},
		{"label": _("Pattern"), "fieldname": "pattern", "fieldtype": "Link", "options": "EPANET Pattern", "width": 120},
		{"label": _("Pressure / Level (m) @ {0}").format(at_label), "fieldname": "pressure", "fieldtype": "Float", "precision": 2, "width": 170},
		{"label": _("Head (m) @ {0}").format(at_label), "fieldname": "head", "fieldtype": "Float", "precision": 2, "width": 140},
		{"label": _("Demand (L/s) @ {0}").format(at_label), "fieldname": "demand", "fieldtype": "Float", "precision": 3, "width": 145},
		{"label": _("Min Pressure / Level (m)"), "fieldname": "min_pressure", "fieldtype": "Float", "precision": 2, "width": 165},
		{"label": _("Max Pressure / Level (m)"), "fieldname": "max_pressure", "fieldtype": "Float", "precision": 2, "width": 165},
	]
	if min_ok is not None:
		columns.append({"label": _("Below {0} m").format(min_ok), "fieldname": "below_min", "fieldtype": "Check", "width": 100})

	elements = network_elements(filters.network)
	types_by_name = payload.get("element_types") or {n: f["feature_role"] for n, f in elements.items()}
	data = []
	for name, role in types_by_name.items():
		if role not in types or role not in NODE_TYPES:
			continue
		f = elements.get(name) or {"_props": {}}
		p = f["_props"]
		demand = value("demand", name)
		row = {
			"element": name,
			"title": f.get("title"),
			"element_type": role,
			"elevation": p.get("elevation_m") if role != "Reservoir" else p.get("base_head_m"),
			"base_demand": p.get("base_demand_lps") if role == "Junction" else None,
			"pattern": p.get("demand_pattern") or p.get("head_pattern"),
			"pressure": value("pressure", name),
			"head": value("head", name),
			"demand": demand * 1000 if demand is not None else None,
			"min_pressure": extreme("pressure", name, min),
			"max_pressure": extreme("pressure", name, max),
		}
		if min_ok is not None and role == "Junction":
			row["below_min"] = 1 if (row["min_pressure"] is not None and row["min_pressure"] < min_ok) else 0
		data.append(row)
	data.sort(key=lambda r: (NODE_TYPES.index(r["element_type"]), r["title"] or r["element"]))

	message = _("Run {0} on {1}").format(run_doc.name, frappe.utils.format_datetime(run_doc.run_on))
	return columns, data, message, _chart(payload, series, data), _summary(data, min_ok)


def _chart(payload, series, data):
	times = (payload.get("timeseries") or {}).get("times") or []
	if len(times) < 2:
		return None
	labels = [clock_label(payload, t) for t in times]
	pressure = series.get("pressure") or {}
	tanks = [r for r in data if r["element_type"] == "Tank"]
	# Tank levels when there are tanks but no junctions to show (e.g. the
	# Element Type filter is Tank), otherwise the junction pressure band.
	if tanks and not any(r["element_type"] == "Junction" for r in data):
		datasets = [
			{"name": r["title"] or r["element"], "values": pressure.get(r["element"]) or []}
			for r in tanks[:MAX_CHART_SERIES]
		]
		title = _("Tank level (m)")
	else:
		junctions = [r["element"] for r in data if r["element_type"] == "Junction"]
		if not junctions:
			return None
		cols = [pressure.get(n) or [] for n in junctions]
		def agg(fn):
			return [round(fn([c[i] for c in cols if i < len(c) and c[i] is not None] or [0]), 2) for i in range(len(times))]
		datasets = [
			{"name": _("Min pressure"), "values": agg(min)},
			{"name": _("Average pressure"), "values": agg(lambda v: sum(v) / len(v))},
			{"name": _("Max pressure"), "values": agg(max)},
		]
		title = _("Junction pressure (m)")
	return {
		"title": title,
		"data": {"labels": labels, "datasets": datasets},
		"type": "line",
		"lineOptions": {"regionFill": 0, "hideDots": 1},
		"axisOptions": {"xIsSeries": 1},
	}


def _summary(data, min_ok):
	junctions = [r for r in data if r["element_type"] == "Junction" and r["min_pressure"] is not None]
	out = []
	if junctions:
		low = min(junctions, key=lambda r: r["min_pressure"])
		out += [
			{"label": _("Junctions"), "value": len(junctions), "datatype": "Int", "indicator": "Blue"},
			{"label": _("Lowest pressure (m)"), "value": round(low["min_pressure"], 2), "datatype": "Float",
				"indicator": "Red" if min_ok is not None and low["min_pressure"] < min_ok else "Green"},
			{"label": _("Highest pressure (m)"), "value": round(max(r["max_pressure"] for r in junctions), 2), "datatype": "Float", "indicator": "Blue"},
		]
		if min_ok is not None:
			n = sum(1 for r in junctions if r.get("below_min"))
			out.append({"label": _("Below {0} m").format(min_ok), "value": n, "datatype": "Int", "indicator": "Red" if n else "Green"})
	return out
