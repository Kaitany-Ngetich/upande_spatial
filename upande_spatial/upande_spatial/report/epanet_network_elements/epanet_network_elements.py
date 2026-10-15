# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""EPANET Network Elements - the network's asset inventory, one row per
junction / reservoir / tank / pipe / pump / valve, with its hydraulic
properties pulled out of the Spatial Feature's `properties` JSON into real
columns (sortable, filterable, exportable). Pick an Element Type to get the
equivalent of a "Pipe List", "Junction List" and so on."""

import frappe
from frappe import _
from frappe.utils import flt

from upande_spatial.api.epanet import network_elements

TYPES = ("Junction", "Reservoir", "Tank", "Pipe", "Pump", "Valve")

# (property key, label, fieldtype, width) per element type, in display order.
PROPERTIES = {
	"Junction": [
		("elevation_m", "Elevation (m)", "Float", 110),
		("base_demand_lps", "Base Demand (L/s)", "Float", 130),
		("demand_pattern", "Demand Pattern", "Link:EPANET Pattern", 150),
		("emitter_coeff", "Emitter Coeff.", "Float", 110),
	],
	"Reservoir": [
		("base_head_m", "Base Head (m)", "Float", 110),
		("head_pattern", "Head Pattern", "Link:EPANET Pattern", 150),
	],
	"Tank": [
		("elevation_m", "Elevation (m)", "Float", 110),
		("init_level_m", "Initial Level (m)", "Float", 120),
		("min_level_m", "Min Level (m)", "Float", 110),
		("max_level_m", "Max Level (m)", "Float", 110),
		("diameter_m", "Diameter (m)", "Float", 100),
		("min_vol_m3", "Min Volume (m³)", "Float", 120),
		("vol_curve", "Volume Curve", "Link:EPANET Curve", 140),
		("overflow", "Overflow", "Check", 80),
	],
	"Pipe": [
		("pipe_class", "Class", "Data", 90),
		("diameter_mm", "Diameter (mm)", "Float", 110),
		("roughness", "Roughness", "Float", 100),
		("minor_loss", "Minor Loss", "Float", 100),
		("initial_status", "Initial Status", "Data", 110),
	],
	"Pump": [
		("pump_curve", "Pump Curve", "Link:EPANET Curve", 140),
		("power_kw", "Power (kW)", "Float", 100),
		("speed", "Speed", "Float", 80),
		("speed_pattern", "Speed Pattern", "Link:EPANET Pattern", 140),
		("efficiency_curve", "Efficiency Curve", "Link:EPANET Curve", 140),
		("energy_price", "Energy Price", "Float", 110),
		("energy_pattern", "Energy Pattern", "Link:EPANET Pattern", 140),
		("initial_status", "Initial Status", "Data", 110),
	],
	"Valve": [
		("valve_type", "Valve Type", "Data", 90),
		("diameter_mm", "Diameter (mm)", "Float", 110),
		("initial_setting", "Setting", "Float", 90),
		("headloss_curve", "Headloss Curve", "Link:EPANET Curve", 140),
		("minor_loss", "Minor Loss", "Float", 100),
		("initial_status", "Initial Status", "Data", 110),
	],
}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.network:
		return [], []
	types = [filters.element_type] if filters.element_type else list(TYPES)

	columns = [
		{"label": _("Element"), "fieldname": "element", "fieldtype": "Link", "options": "Spatial Feature", "width": 120},
		{"label": _("Title"), "fieldname": "title", "fieldtype": "Data", "width": 110},
		{"label": _("Type"), "fieldname": "element_type", "fieldtype": "Data", "width": 90},
		{"label": _("Layer"), "fieldname": "layer", "fieldtype": "Data", "width": 130},
		{"label": _("Owner"), "fieldname": "owner_record", "fieldtype": "Data", "width": 150},
	]
	if any(t in ("Pipe", "Pump", "Valve") for t in types):
		columns.append({"label": _("Length (m)"), "fieldname": "length_m", "fieldtype": "Float", "precision": 1, "width": 100})
	seen = {c["fieldname"] for c in columns}
	for t in types:
		for key, label, fieldtype, width in PROPERTIES[t]:
			if key in seen:
				continue
			seen.add(key)
			col = {"label": _(label), "fieldname": key, "width": width}
			if fieldtype.startswith("Link:"):
				col.update(fieldtype="Link", options=fieldtype[5:])
			else:
				col["fieldtype"] = fieldtype
			columns.append(col)

	elements = network_elements(filters.network)
	meta = {
		r.name: r
		for r in frappe.get_all(
			"Spatial Feature",
			filters={"name": ["in", list(elements) or [""]]},
			fields=["name", "reference_doctype", "reference_name", "layer"],
		)
	}
	data = []
	for name, f in elements.items():
		role = f["feature_role"]
		if role not in types:
			continue
		m = meta.get(name) or frappe._dict()
		row = {
			"element": name,
			"title": f.get("title"),
			"element_type": role,
			"layer": m.layer,
			"owner_record": f"{m.reference_doctype}: {m.reference_name}" if m.reference_doctype else None,
			"length_m": flt(f.get("length_m")) if role in ("Pipe", "Pump", "Valve") else None,
		}
		for key, *_rest in PROPERTIES[role]:
			row[key] = f["_props"].get(key)
		data.append(row)
	data.sort(key=lambda r: (TYPES.index(r["element_type"]), r["title"] or r["element"]))

	return columns, data, None, _chart(data), _summary(data)


def _chart(data):
	"""Pipe length by diameter - the usual first question about a network."""
	by_dia = {}
	for r in data:
		if r["element_type"] == "Pipe" and r.get("diameter_mm"):
			by_dia[flt(r["diameter_mm"])] = by_dia.get(flt(r["diameter_mm"]), 0) + flt(r.get("length_m"))
	if not by_dia:
		return None
	sizes = sorted(by_dia)
	return {
		"data": {
			"labels": [f"{d:g} mm" for d in sizes],
			"datasets": [{"name": _("Pipe length (m)"), "values": [round(by_dia[d]) for d in sizes]}],
		},
		"type": "bar",
		"colors": ["#185FA5"],
	}


def _summary(data):
	out = []
	for t in TYPES:
		n = sum(1 for r in data if r["element_type"] == t)
		if n:
			out.append({"label": _(t + "s"), "value": n, "datatype": "Int", "indicator": "Blue"})
	pipe_len = sum(flt(r.get("length_m")) for r in data if r["element_type"] == "Pipe")
	if pipe_len:
		out.append({"label": _("Pipe length (km)"), "value": round(pipe_len / 1000, 2), "datatype": "Float", "indicator": "Green"})
	return out
