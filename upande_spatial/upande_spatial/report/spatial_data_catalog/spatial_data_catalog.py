# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""Spatial Data Catalog — a read-only inventory of every spatial dataset
(map layer) in the system, one row per distinct `layer` rather than per
reference_doctype (a single doctype like Location can own more than one
layer, e.g. "Site" point + "Site Boundary" polygon).

Mirrors the same layer-discovery logic as
upande_spatial.api.spatial.get_layers(): every layer configured on a
Spatial Entity Config's Allowed Geometry child table, PLUS any `layer`
value actually present on real Spatial Feature records that isn't already
in that configured set (ad-hoc / legacy / unconfigured data). Deliberately
does not import from api/spatial.py — that module is being actively
extended concurrently elsewhere, so this report re-derives what it needs
directly against the DB instead of taking a dependency on it.
"""

import re

import frappe
from frappe import _

UNCATEGORIZED = "Uncategorized"
DEFAULT_CRS = "EPSG:4326"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{"label": _("Layer"), "fieldname": "layer", "fieldtype": "Data", "width": 170},
		{"label": _("Reference Doctype"), "fieldname": "reference_doctype", "fieldtype": "Data", "width": 150},
		{"label": _("Feature Role"), "fieldname": "feature_role", "fieldtype": "Data", "width": 120},
		{"label": _("Geometry Type"), "fieldname": "geometry_type", "fieldtype": "Data", "width": 110},
		{"label": _("Color"), "fieldname": "color", "fieldtype": "HTML", "width": 90},
		{"label": _("Feature Count"), "fieldname": "feature_count", "fieldtype": "Int", "width": 110},
		{"label": _("Farms Covered"), "fieldname": "farms_covered", "fieldtype": "Int", "width": 110},
		{"label": _("CRS"), "fieldname": "crs", "fieldtype": "Data", "width": 100},
		{"label": _("Last Updated"), "fieldname": "last_updated", "fieldtype": "Datetime", "width": 160},
		{"label": _("Source Modules"), "fieldname": "source_modules", "fieldtype": "Data", "width": 220},
	]


def get_configured_layers():
	"""One entry per distinct layer_name configured on any Spatial Entity
	Config's Allowed Geometry rows. Spatial Entity Config is autonamed
	`field:reference_doctype`, so the child table's `parent` IS the
	reference_doctype directly — no extra join needed. When two configs
	(different reference_doctypes) share the same layer_name, the
	alphabetically-first reference_doctype wins, same tie-break
	api.spatial.get_layers() uses, for consistency with the rest of the
	app."""
	rows = frappe.db.sql(
		"""
		SELECT
			g.layer_name AS layer_name,
			g.parent AS reference_doctype,
			g.feature_role AS feature_role,
			g.geometry_type AS geometry_type,
			g.color AS color
		FROM `tabSpatial Entity Allowed Geometry` g
		WHERE g.layer_name IS NOT NULL AND g.layer_name != ''
		ORDER BY g.layer_name ASC, g.parent ASC
		""",
		as_dict=True,
	)

	by_layer = {}
	for row in rows:
		by_layer.setdefault(row.layer_name, row)
	return by_layer


def get_adhoc_layer_attrs(layer_names):
	"""For layers with no Spatial Entity Config row at all (features saved
	under a `layer` value that isn't in the configured set), derive display
	attributes straight from the Spatial Feature rows that actually carry
	that layer: distinct reference_doctype / feature_role / geometry_type,
	comma-joined when more than one value is present, "—" when none."""
	if not layer_names:
		return {}

	rows = frappe.get_all(
		"Spatial Feature",
		filters={"layer": ("in", layer_names)},
		fields=["layer", "reference_doctype", "feature_role", "geometry_type"],
	)

	by_layer = {}
	for row in rows:
		bucket = by_layer.setdefault(
			row.layer, {"reference_doctype": set(), "feature_role": set(), "geometry_type": set()}
		)
		if row.reference_doctype:
			bucket["reference_doctype"].add(row.reference_doctype)
		if row.feature_role:
			bucket["feature_role"].add(row.feature_role)
		if row.geometry_type:
			bucket["geometry_type"].add(row.geometry_type)

	attrs = {}
	for layer, bucket in by_layer.items():
		attrs[layer] = {
			"reference_doctype": ", ".join(sorted(bucket["reference_doctype"])) or "—",
			"feature_role": ", ".join(sorted(bucket["feature_role"])) or "—",
			"geometry_type": ", ".join(sorted(bucket["geometry_type"])) or "—",
			"color": None,
		}
	return attrs


def get_feature_stats(layer_names, farm=None, has_crs_column=False):
	"""Per-layer aggregates read straight off Spatial Feature: live feature
	count, distinct non-null farms covered, distinct source_module values,
	and the most recent `modified`. Optionally scoped to one farm (the
	report's only filter) — narrows every one of these numbers to that
	farm's features rather than just feature_count, so a farm-filtered view
	is internally consistent."""
	if not layer_names:
		return {}

	conditions = ["layer IN %(layers)s"]
	values = {"layers": layer_names}
	if farm:
		conditions.append("farm = %(farm)s")
		values["farm"] = farm
	where_clause = " AND ".join(conditions)

	crs_select = "MAX(crs) AS crs" if has_crs_column else "NULL AS crs"
	# GROUP_CONCAT is MariaDB-only; Postgres's equivalent is STRING_AGG and
	# requires an explicit separator argument (no implicit default).
	group_concat = (
		"STRING_AGG(DISTINCT NULLIF(source_module, ''), ',')"
		if frappe.db.db_type == "postgres"
		else "GROUP_CONCAT(DISTINCT NULLIF(source_module, ''))"
	)

	rows = frappe.db.sql(
		f"""
		SELECT
			layer,
			COUNT(name) AS feature_count,
			COUNT(DISTINCT CASE WHEN farm IS NOT NULL AND farm != '' THEN farm END) AS farms_covered,
			MAX(modified) AS last_updated,
			{group_concat} AS source_modules,
			{crs_select}
		FROM `tabSpatial Feature`
		WHERE {where_clause}
		GROUP BY layer
		""",
		values,
		as_dict=True,
	)
	return {row.layer: row for row in rows}


def get_data(filters):
	farm = filters.get("farm")
	has_crs_column = frappe.db.has_column("Spatial Feature", "crs")

	configured = get_configured_layers()

	# Any `layer` value present on real Spatial Feature records that isn't
	# already covered by a Spatial Entity Config row - ad-hoc/uncategorized
	# data, same "still show up as a browsable layer" rule as
	# api.spatial.get_layers().
	feature_layer_filters = {"layer": ("is", "set")}
	if farm:
		feature_layer_filters["farm"] = farm
	feature_layers = frappe.get_all(
		"Spatial Feature", filters=feature_layer_filters, fields=["layer"], group_by="layer", pluck="layer"
	)
	adhoc_layer_names = [l for l in feature_layers if l and l not in configured]
	adhoc = get_adhoc_layer_attrs(adhoc_layer_names)

	all_layer_names = sorted(set(configured.keys()) | set(adhoc.keys()))
	stats = get_feature_stats(all_layer_names, farm=farm, has_crs_column=has_crs_column)

	data = []
	for layer_name in all_layer_names:
		stat = stats.get(layer_name)

		# A layer with zero features under the active farm filter has
		# nothing meaningful to show for that farm - skip it rather than
		# padding the table with all-zero rows.
		if farm and not stat:
			continue

		if layer_name in configured:
			cfg = configured[layer_name]
			reference_doctype = cfg.reference_doctype or "—"
			feature_role = cfg.feature_role or "—"
			geometry_type = cfg.geometry_type or "—"
			color = cfg.color
		else:
			cfg = adhoc.get(layer_name, {})
			reference_doctype = cfg.get("reference_doctype") or "—"
			feature_role = cfg.get("feature_role") or "—"
			geometry_type = cfg.get("geometry_type") or "—"
			color = cfg.get("color")

		# color is admin-writable (Spatial Entity Config), not user input off
		# the street, but this report renders it as raw HTML - validate it's
		# actually a hex color before interpolating, same convention Map
		# Viewer's own frontend uses (safeColor()) for the same reason.
		safe_color = color if color and re.match(r"^#[0-9a-fA-F]{3,8}$", color) else None
		color_html = (
			f'<div style="display:flex;align-items:center;gap:6px;">'
			f'<span style="display:inline-block;width:14px;height:14px;border-radius:3px;'
			f'background:{safe_color};border:1px solid rgba(0,0,0,0.2);"></span>{frappe.utils.escape_html(color)}</div>'
			if safe_color
			else "—"
		)

		if stat:
			feature_count = stat.feature_count or 0
			farms_covered = stat.farms_covered or 0
			last_updated = stat.last_updated or None
			source_modules = stat.source_modules or "—"
			crs = stat.crs if (has_crs_column and stat.crs) else DEFAULT_CRS
		else:
			feature_count = 0
			farms_covered = 0
			last_updated = None
			source_modules = "—"
			crs = DEFAULT_CRS

		data.append(
			{
				"layer": layer_name or UNCATEGORIZED,
				"reference_doctype": reference_doctype,
				"feature_role": feature_role,
				"geometry_type": geometry_type,
				"color": color_html,
				"feature_count": feature_count,
				"farms_covered": farms_covered,
				"crs": crs,
				"last_updated": last_updated,
				"source_modules": source_modules or "—",
			}
		)

	return data
