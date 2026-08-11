# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""Shared spatial data API — the single point of contact any module uses to
store or fetch geometry, so nobody needs to reach into the Spatial Feature
doctype's internals or duplicate this logic per app. See the "Upande
Spatial" project brief: one place for all farms' points/lines/polygons,
readable and writable by any team/module rather than siloed per app.
"""

import json

import frappe
from frappe import _


def _as_geojson_feature(geometry, properties=None):
	"""Accepts either a bare geometry dict ({"type": "Polygon", ...}) or an
	already-wrapped Feature/FeatureCollection, and normalizes to the
	FeatureCollection string Frappe's Geolocation field expects."""
	if isinstance(geometry, str):
		geometry = json.loads(geometry)

	if geometry.get("type") == "FeatureCollection":
		return json.dumps(geometry)

	if geometry.get("type") == "Feature":
		feature = geometry
	else:
		feature = {"type": "Feature", "properties": properties or {}, "geometry": geometry}

	return json.dumps({"type": "FeatureCollection", "features": [feature]})


@frappe.whitelist()
def upsert_feature(
	geometry,
	reference_doctype=None,
	reference_name=None,
	farm=None,
	company=None,
	source_module=None,
	title=None,
	properties=None,
):
	"""Create or update the Spatial Feature for (reference_doctype,
	reference_name) — calling modules don't need to check existence first,
	just always call this. If reference_doctype/reference_name aren't
	given, always creates a new standalone feature (e.g. a one-off drawn
	shape with no owning record)."""
	if isinstance(properties, dict):
		properties = json.dumps(properties)

	existing_name = None
	if reference_doctype and reference_name:
		existing_name = frappe.db.get_value(
			"Spatial Feature",
			{"reference_doctype": reference_doctype, "reference_name": reference_name},
			"name",
		)

	if existing_name:
		doc = frappe.get_doc("Spatial Feature", existing_name)
	else:
		doc = frappe.new_doc("Spatial Feature")
		doc.reference_doctype = reference_doctype
		doc.reference_name = reference_name

	doc.geometry = _as_geojson_feature(geometry, json.loads(properties) if properties else None)
	if farm is not None:
		doc.farm = farm
	if company is not None:
		doc.company = company
	if source_module is not None:
		doc.source_module = source_module
	if title is not None:
		doc.title = title
	if properties is not None:
		doc.properties = properties

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"name": doc.name,
		"geometry_type": doc.geometry_type,
		"area_sq_m": doc.area_sq_m,
		"length_m": doc.length_m,
	}


@frappe.whitelist()
def get_feature(name=None, reference_doctype=None, reference_name=None):
	"""Fetch one feature, either by its own name or by the (doctype, name)
	of whatever it belongs to."""
	if not name and reference_doctype and reference_name:
		name = frappe.db.get_value(
			"Spatial Feature",
			{"reference_doctype": reference_doctype, "reference_name": reference_name},
			"name",
		)
	if not name:
		return None

	doc = frappe.get_doc("Spatial Feature", name)
	return {
		"name": doc.name,
		"title": doc.title,
		"geometry_type": doc.geometry_type,
		"geometry": json.loads(doc.geometry) if doc.geometry else None,
		"farm": doc.farm,
		"company": doc.company,
		"reference_doctype": doc.reference_doctype,
		"reference_name": doc.reference_name,
		"source_module": doc.source_module,
		"area_sq_m": doc.area_sq_m,
		"length_m": doc.length_m,
		"properties": json.loads(doc.properties) if doc.properties else None,
	}


@frappe.whitelist()
def list_features(farm=None, reference_doctype=None, geometry_type=None, source_module=None, limit=500):
	"""List features by any combination of the common filters — none
	required, so `list_features()` with no args just returns everything
	(capped at `limit`)."""
	filters = {}
	if farm:
		filters["farm"] = farm
	if reference_doctype:
		filters["reference_doctype"] = reference_doctype
	if geometry_type:
		filters["geometry_type"] = geometry_type
	if source_module:
		filters["source_module"] = source_module

	rows = frappe.get_all(
		"Spatial Feature",
		filters=filters,
		fields=[
			"name", "title", "geometry_type", "farm", "company",
			"reference_doctype", "reference_name", "source_module",
			"area_sq_m", "length_m",
		],
		limit_page_length=int(limit) if limit else 500,
		order_by="modified desc",
	)
	return rows


@frappe.whitelist()
def get_features_geojson(farm=None, reference_doctype=None, source_module=None):
	"""Same filters as list_features, but returns one ready-to-render
	GeoJSON FeatureCollection — this is what a map viewer (Leaflet,
	OpenLayers, whatever) should call directly rather than list_features +
	get_feature in a loop."""
	filters = {}
	if farm:
		filters["farm"] = farm
	if reference_doctype:
		filters["reference_doctype"] = reference_doctype
	if source_module:
		filters["source_module"] = source_module

	rows = frappe.get_all(
		"Spatial Feature",
		filters=filters,
		fields=["name", "title", "geometry", "farm", "reference_doctype", "reference_name", "properties"],
		limit_page_length=0,
	)

	features = []
	for r in rows:
		if not r.geometry:
			continue
		try:
			parsed = json.loads(r.geometry)
		except Exception:
			continue
		for f in parsed.get("features", []):
			f.setdefault("properties", {})
			f["properties"]["_spatial_feature_name"] = r.name
			f["properties"]["_title"] = r.title
			f["properties"]["_farm"] = r.farm
			f["properties"]["_reference_doctype"] = r.reference_doctype
			f["properties"]["_reference_name"] = r.reference_name
			features.append(f)

	return {"type": "FeatureCollection", "features": features}


@frappe.whitelist()
def delete_feature(reference_doctype=None, reference_name=None, name=None):
	"""Clean up when the owning record is deleted. Safe to call even if
	nothing exists yet — returns deleted=False rather than throwing, so a
	calling module's on_trash hook can call this unconditionally."""
	if not name and reference_doctype and reference_name:
		name = frappe.db.get_value(
			"Spatial Feature",
			{"reference_doctype": reference_doctype, "reference_name": reference_name},
			"name",
		)
	if not name or not frappe.db.exists("Spatial Feature", name):
		return {"deleted": False}

	frappe.delete_doc("Spatial Feature", name, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": True, "name": name}
