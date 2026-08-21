# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""Shared spatial data API — the single point of contact any module uses to
store or fetch geometry, so nobody needs to reach into the Spatial Feature
doctype's internals or duplicate this logic per app. See the "Upande
Spatial" project brief: one place for all farms' points/lines/polygons,
readable and writable by any team/module rather than siloed per app.

One record can now own more than one feature at once — a Farm can have a
boundary Polygon AND a gate Point, an Asset can have a Location point AND a
Footprint polygon — distinguished by `feature_role`, not just
(reference_doctype, reference_name) alone. Callers that never pass a role
keep behaving exactly as before (one feature per reference, under a blank
role), so this is additive, not a breaking change to whatever already calls
this API.
"""

import json

import frappe
from frappe import _

DEFAULT_ROLE = ""


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


def _geometry_type_of(geometry):
	if isinstance(geometry, str):
		geometry = json.loads(geometry)
	if geometry.get("type") == "FeatureCollection":
		feats = geometry.get("features") or []
		geometry = feats[0].get("geometry") if feats else {}
	elif geometry.get("type") == "Feature":
		geometry = geometry.get("geometry") or {}
	return geometry.get("type")


def _validate_against_config(reference_doctype, feature_role, geometry_type):
	"""Opt-in restriction: only enforced for doctypes that actually have a
	Spatial Entity Config row. No config row for this doctype (the default,
	for every doctype nobody has configured yet) means no restriction at
	all — this can be adopted gradually, doctype by doctype, without
	touching Farm/Asset/Security's own schemas or breaking any existing
	caller that predates this."""
	if not reference_doctype:
		return
	if not frappe.db.exists("Spatial Entity Config", reference_doctype):
		return

	config = frappe.get_cached_doc("Spatial Entity Config", reference_doctype)
	if not config.allowed_geometries:
		return  # a config row with no rows configured also means "anything goes"

	role = feature_role or DEFAULT_ROLE
	for row in config.allowed_geometries:
		role_matches = (not row.feature_role) or row.feature_role == role
		if row.geometry_type == geometry_type and role_matches:
			return

	frappe.throw(
		_(
			"{0} geometry under role {1} is not allowed for {2} — check Spatial Entity Config."
		).format(geometry_type, role or _("(blank)"), reference_doctype)
	)


@frappe.whitelist()
def upsert_feature(
	geometry,
	reference_doctype=None,
	reference_name=None,
	feature_role=None,
	farm=None,
	company=None,
	source_module=None,
	title=None,
	properties=None,
):
	"""Create or update the Spatial Feature for (reference_doctype,
	reference_name, feature_role) — calling modules don't need to check
	existence first, just always call this. If reference_doctype/
	reference_name aren't given, always creates a new standalone feature
	(e.g. a one-off drawn shape with no owning record).

	feature_role distinguishes multiple features on the same record (e.g.
	a Farm's boundary Polygon vs. its gate Point) — omit it and this
	behaves exactly as it always has, one feature per reference."""
	if isinstance(properties, dict):
		properties = json.dumps(properties)

	geometry_type = _geometry_type_of(geometry)
	_validate_against_config(reference_doctype, feature_role, geometry_type)

	existing_name = None
	if reference_doctype and reference_name:
		existing_name = frappe.db.get_value(
			"Spatial Feature",
			{
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"feature_role": feature_role or DEFAULT_ROLE,
			},
			"name",
		)

	if existing_name:
		doc = frappe.get_doc("Spatial Feature", existing_name)
	else:
		doc = frappe.new_doc("Spatial Feature")
		doc.reference_doctype = reference_doctype
		doc.reference_name = reference_name
		doc.feature_role = feature_role or DEFAULT_ROLE

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

	# No ignore_permissions here on purpose - a caller authenticating as a
	# real Frappe user (their own API key, not a shared/admin one) should
	# get exactly that user's actual Spatial Feature permissions, same as
	# anywhere else in ERP. Throws frappe.PermissionError if they lack
	# create/write rights, same as any other doctype.
	doc.save()
	frappe.db.commit()

	return {
		"name": doc.name,
		"geometry_type": doc.geometry_type,
		"feature_role": doc.feature_role,
		"area_sq_m": doc.area_sq_m,
		"length_m": doc.length_m,
	}


@frappe.whitelist()
def get_feature(name=None, reference_doctype=None, reference_name=None, feature_role=None):
	"""Fetch one feature, either by its own name or by the (doctype, name)
	of whatever it belongs to. Pass feature_role when a record has more
	than one feature and you want a specific one — omitted, this returns
	the blank-role (default) feature, matching pre-feature_role callers."""
	if not name and reference_doctype and reference_name:
		name = frappe.db.get_value(
			"Spatial Feature",
			{
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"feature_role": feature_role or DEFAULT_ROLE,
			},
			"name",
		)
	if not name:
		return None

	doc = frappe.get_doc("Spatial Feature", name)
	# frappe.get_doc() alone doesn't gate access the way frappe.get_all()
	# does for list_features/get_features_geojson below - has to be
	# checked explicitly here, or a caller could fetch any named feature
	# by guessing/enumerating names regardless of their real read rights.
	doc.check_permission("read")
	return {
		"name": doc.name,
		"title": doc.title,
		"geometry_type": doc.geometry_type,
		"feature_role": doc.feature_role,
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
def list_features(
	farm=None,
	reference_doctype=None,
	geometry_type=None,
	feature_role=None,
	source_module=None,
	limit=500,
):
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
	if feature_role is not None:
		filters["feature_role"] = feature_role
	if source_module:
		filters["source_module"] = source_module

	rows = frappe.get_all(
		"Spatial Feature",
		filters=filters,
		fields=[
			"name", "title", "geometry_type", "feature_role", "farm", "company",
			"reference_doctype", "reference_name", "source_module",
			"area_sq_m", "length_m",
		],
		limit_page_length=int(limit) if limit else 500,
		order_by="modified desc",
	)
	return rows


@frappe.whitelist()
def get_features_geojson(farm=None, reference_doctype=None, feature_role=None, source_module=None):
	"""Same filters as list_features, but returns one ready-to-render
	GeoJSON FeatureCollection — this is what a map viewer (Leaflet,
	OpenLayers, whatever) should call directly rather than list_features +
	get_feature in a loop."""
	filters = {}
	if farm:
		filters["farm"] = farm
	if reference_doctype:
		filters["reference_doctype"] = reference_doctype
	if feature_role is not None:
		filters["feature_role"] = feature_role
	if source_module:
		filters["source_module"] = source_module

	rows = frappe.get_all(
		"Spatial Feature",
		filters=filters,
		fields=[
			"name", "title", "geometry", "farm", "feature_role",
			"reference_doctype", "reference_name", "properties",
		],
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
			f["properties"]["_feature_role"] = r.feature_role
			f["properties"]["_reference_doctype"] = r.reference_doctype
			f["properties"]["_reference_name"] = r.reference_name
			features.append(f)

	return {"type": "FeatureCollection", "features": features}


@frappe.whitelist()
def delete_feature(reference_doctype=None, reference_name=None, feature_role=None, name=None):
	"""Clean up when the owning record is deleted. Safe to call even if
	nothing exists yet — returns deleted=False rather than throwing, so a
	calling module's on_trash hook can call this unconditionally. Pass
	feature_role to delete one specific feature off a record that has
	several; omitted, targets the blank-role (default) one."""
	if not name and reference_doctype and reference_name:
		name = frappe.db.get_value(
			"Spatial Feature",
			{
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"feature_role": feature_role or DEFAULT_ROLE,
			},
			"name",
		)
	if not name or not frappe.db.exists("Spatial Feature", name):
		return {"deleted": False}

	# Same reasoning as upsert_feature above - respects the calling user's
	# actual delete permission instead of bypassing it.
	frappe.delete_doc("Spatial Feature", name)
	frappe.db.commit()
	return {"deleted": True, "name": name}
