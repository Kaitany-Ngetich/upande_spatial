# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""EPANET Link Results - pipe / pump / valve flows, velocities, headloss
and status from one simulation run (the latest successful one by default),
at a chosen hour plus maxima over the whole run, and each pump's energy use
and cost. The chart shows velocity (or, for pumps, power) through the run."""

import frappe
from frappe import _
from frappe.utils import flt

from upande_spatial.api.epanet import LINK_STATUS_LABELS, clock_label, load_run, network_elements, pick_step

LINK_TYPES = ("Pipe", "Pump", "Valve")
MAX_CHART_SERIES = 8


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.network:
		return [], []
	run_doc, payload = load_run(filters.network, filters.run)
	types = [filters.element_type] if filters.element_type else list(LINK_TYPES)
	max_v = flt(filters.max_velocity) if filters.max_velocity not in (None, "") else None

	ts = payload.get("timeseries") or {}
	times = ts.get("times") or []
	step = pick_step(payload, filters.at_hour)
	at_label = clock_label(payload, times[step]) if step is not None else _("final")
	series = ts.get("link") or {}
	final = payload.get("link_results") or {}
	summary = payload.get("summary") or {}
	power = ts.get("pump_power_kw") or {}

	def value(attr, name):
		if step is not None:
			vals = (series.get(attr) or {}).get(name)
			return vals[step] if vals else None
		return (final.get(name) or {}).get(attr)

	def peak(attr, name):
		vals = [abs(v) for v in ((series.get(attr) or {}).get(name) or []) if v is not None]
		if vals:
			return max(vals)
		v = value(attr, name)
		return abs(v) if v is not None else None

	columns = [
		{"label": _("Element"), "fieldname": "element", "fieldtype": "Link", "options": "Spatial Feature", "width": 120},
		{"label": _("Title"), "fieldname": "title", "fieldtype": "Data", "width": 100},
		{"label": _("Type"), "fieldname": "element_type", "fieldtype": "Data", "width": 80},
		{"label": _("Class"), "fieldname": "pipe_class", "fieldtype": "Data", "width": 80},
		{"label": _("Diameter (mm)"), "fieldname": "diameter", "fieldtype": "Float", "precision": 0, "width": 105},
		{"label": _("Length (m)"), "fieldname": "length", "fieldtype": "Float", "precision": 1, "width": 95},
		{"label": _("Flow (L/s) @ {0}").format(at_label), "fieldname": "flow", "fieldtype": "Float", "precision": 2, "width": 135},
		{"label": _("Velocity (m/s) @ {0}").format(at_label), "fieldname": "velocity", "fieldtype": "Float", "precision": 3, "width": 150},
		{"label": _("Headloss (m/km) @ {0}").format(at_label), "fieldname": "headloss", "fieldtype": "Float", "precision": 2, "width": 160},
		{"label": _("Status @ {0}").format(at_label), "fieldname": "status", "fieldtype": "Data", "width": 110},
		{"label": _("Peak Flow (L/s)"), "fieldname": "peak_flow", "fieldtype": "Float", "precision": 2, "width": 120},
		{"label": _("Peak Velocity (m/s)"), "fieldname": "peak_velocity", "fieldtype": "Float", "precision": 3, "width": 140},
	]

	elements = network_elements(filters.network)
	types_by_name = payload.get("element_types") or {n: f["feature_role"] for n, f in elements.items()}
	data = []
	for name, role in types_by_name.items():
		if role not in types or role not in LINK_TYPES:
			continue
		f = elements.get(name) or {"_props": {}}
		p = f["_props"]
		flow = value("flow", name)
		headloss = value("headloss", name)
		status = value("status", name)
		peak_flow = peak("flow", name)
		row = {
			"element": name,
			"title": f.get("title"),
			"element_type": role,
			"pipe_class": p.get("pipe_class") or p.get("valve_type"),
			"diameter": p.get("diameter_mm"),
			"length": flt(f.get("length_m")) or None,
			"flow": flow * 1000 if flow is not None else None,
			"velocity": value("velocity", name),
			# wntr reports pipe headloss per metre of pipe; pumps/valves are
			# a head change across the element, not per length.
			"headloss": headloss * 1000 if (headloss is not None and role == "Pipe") else None,
			"status": LINK_STATUS_LABELS.get(int(status)) if status is not None else None,
			"peak_flow": peak_flow * 1000 if peak_flow is not None else None,
			"peak_velocity": peak("velocity", name),
		}
		if role == "Pump":
			kw = [v for v in power.get(name) or [] if v is not None]
			row["avg_power"] = sum(kw) / len(kw) if kw else None
			row["energy"] = (summary.get("pump_energy_kwh") or {}).get(name)
			row["cost"] = (summary.get("pump_cost") or {}).get(name)
		if max_v is not None:
			row["over_velocity"] = 1 if (row["peak_velocity"] or 0) > max_v else 0
		data.append(row)
	data.sort(key=lambda r: (LINK_TYPES.index(r["element_type"]), r["title"] or r["element"]))

	if any(r["element_type"] == "Pump" for r in data):
		columns += [
			{"label": _("Avg Power (kW)"), "fieldname": "avg_power", "fieldtype": "Float", "precision": 2, "width": 115},
			{"label": _("Energy (kWh)"), "fieldname": "energy", "fieldtype": "Float", "precision": 2, "width": 110},
			{"label": _("Energy Cost"), "fieldname": "cost", "fieldtype": "Float", "precision": 2, "width": 105},
		]
	if max_v is not None:
		columns.append({"label": _("Over {0} m/s").format(max_v), "fieldname": "over_velocity", "fieldtype": "Check", "width": 100})

	message = _("Run {0} on {1}").format(run_doc.name, frappe.utils.format_datetime(run_doc.run_on))
	return columns, data, message, _chart(payload, series, power, data), _summary(data, max_v)


def _chart(payload, series, power, data):
	times = (payload.get("timeseries") or {}).get("times") or []
	if len(times) < 2:
		return None
	labels = [clock_label(payload, t) for t in times]
	pumps = [r for r in data if r["element_type"] == "Pump"]
	if pumps and all(r["element_type"] == "Pump" for r in data):
		datasets = [{"name": r["title"] or r["element"], "values": power.get(r["element"]) or []} for r in pumps[:MAX_CHART_SERIES]]
		title = _("Pump power (kW)")
	else:
		# The links that ever run fastest - the ones worth watching.
		fastest = sorted(data, key=lambda r: -(r["peak_velocity"] or 0))[:5]
		velocity = series.get("velocity") or {}
		datasets = [
			{"name": r["title"] or r["element"], "values": [abs(v or 0) for v in velocity.get(r["element"]) or []]}
			for r in fastest if velocity.get(r["element"])
		]
		title = _("Velocity of the 5 fastest links (m/s)")
	if not datasets:
		return None
	return {
		"title": title,
		"data": {"labels": labels, "datasets": datasets},
		"type": "line",
		"lineOptions": {"regionFill": 0, "hideDots": 1},
		"axisOptions": {"xIsSeries": 1},
	}


def _summary(data, max_v):
	out = []
	for t in LINK_TYPES:
		n = sum(1 for r in data if r["element_type"] == t)
		if n:
			out.append({"label": _(t + "s"), "value": n, "datatype": "Int", "indicator": "Blue"})
	peaks = [r["peak_velocity"] for r in data if r["peak_velocity"] is not None]
	if peaks:
		out.append({"label": _("Peak velocity (m/s)"), "value": round(max(peaks), 3), "datatype": "Float",
			"indicator": "Red" if max_v is not None and max(peaks) > max_v else "Green"})
	if max_v is not None:
		n = sum(1 for r in data if r.get("over_velocity"))
		out.append({"label": _("Over {0} m/s").format(max_v), "value": n, "datatype": "Int", "indicator": "Red" if n else "Green"})
	energy = sum(flt(r.get("energy")) for r in data if r["element_type"] == "Pump")
	if energy:
		out.append({"label": _("Pump energy (kWh)"), "value": round(energy, 2), "datatype": "Float", "indicator": "Orange"})
		out.append({"label": _("Energy cost"), "value": round(sum(flt(r.get("cost")) for r in data), 2), "datatype": "Float", "indicator": "Orange"})
	return out
