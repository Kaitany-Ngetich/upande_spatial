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
from frappe.utils import cint

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


def _resolve_layer_name(reference_doctype, feature_role, geometry_type):
	"""Look up the map "layer" a feature belongs to, from the same
	Spatial Entity Config / Spatial Entity Allowed Geometry row that
	_validate_against_config above matches on (reference_doctype +
	geometry_type + feature_role, blank feature_role on the config row
	meaning "any role"). Returns a (layer_name, color, marker_icon) tuple.

	Falls back to (reference_doctype, None, None) — or ("Uncategorized",
	None, None) when there's no reference_doctype at all — whenever there's
	no Spatial Entity Config row for this doctype, no matching
	allowed_geometries row, or the matching row exists but its layer_name is
	blank (a combo that's allowed but not assigned to a specific layer)."""
	fallback = (reference_doctype or _("Uncategorized"), None, None)
	if not reference_doctype:
		return fallback
	if not frappe.db.exists("Spatial Entity Config", reference_doctype):
		return fallback

	config = frappe.get_cached_doc("Spatial Entity Config", reference_doctype)
	if not config.allowed_geometries:
		return fallback

	role = feature_role or DEFAULT_ROLE
	for row in config.allowed_geometries:
		role_matches = (not row.feature_role) or row.feature_role == role
		if row.geometry_type == geometry_type and role_matches:
			if row.layer_name:
				return (row.layer_name, row.color, row.marker_icon)
			return fallback

	return fallback


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
	notes=None,
	name=None,
):
	"""Create or update the Spatial Feature for (reference_doctype,
	reference_name, feature_role) — calling modules don't need to check
	existence first, just always call this. If reference_doctype/
	reference_name aren't given, always creates a new standalone feature
	(e.g. a one-off drawn shape with no owning record) UNLESS `name` is
	also given (see below).

	feature_role distinguishes multiple features on the same record (e.g.
	a Farm's boundary Polygon vs. its gate Point) — omit it and this
	behaves exactly as it always has, one feature per reference.

	Pass `name` (the Spatial Feature's own record name — e.g. from a prior
	upsert_feature/get_feature call, or the `name` property
	get_features_geojson stamps onto every exported feature) to update that
	exact record directly, regardless of whether it has a reference_doctype/
	reference_name at all. This is the only reliable way to update a
	STANDALONE feature in place — one with no reference_doctype/
	reference_name to match on, so the reference-based lookup below can
	never find it — and it's also what makes an export
	(get_features_geojson) → reimport (import_features) round-trip update
	existing features instead of re-creating duplicates every time, for
	standalone and reference-linked features alike. Falls back to the
	reference-based lookup-or-create when `name` isn't given, or refers to
	a record that no longer exists."""
	if isinstance(properties, dict):
		properties = json.dumps(properties)

	geometry_type = _geometry_type_of(geometry)
	_validate_against_config(reference_doctype, feature_role, geometry_type)

	existing_name = None
	matched_by_name = False
	if name and frappe.db.exists("Spatial Feature", name):
		existing_name = name
		matched_by_name = True
	elif reference_doctype and reference_name:
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
		if matched_by_name:
			# Matched directly by record name rather than by the
			# (reference_doctype, reference_name, feature_role) triple, so
			# those aren't guaranteed to already match the doc - apply them
			# if given, same "None means don't touch" convention as
			# farm/company/source_module/title below.
			if reference_doctype is not None:
				doc.reference_doctype = reference_doctype
			if reference_name is not None:
				doc.reference_name = reference_name
			if feature_role is not None:
				doc.feature_role = feature_role
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
	if notes is not None:
		doc.notes = notes

	layer_name, _color, _marker_icon = _resolve_layer_name(reference_doctype, feature_role, geometry_type)
	doc.layer = layer_name

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
		"layer": doc.layer,
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
			"reference_doctype", "reference_name", "source_module", "layer",
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
			"name", "title", "geometry", "farm", "feature_role", "geometry_type", "layer",
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
		_layer_name, color, marker_icon = _resolve_layer_name(
			r.reference_doctype, r.feature_role, r.geometry_type
		)
		for f in parsed.get("features", []):
			f.setdefault("properties", {})
			f["properties"]["_spatial_feature_name"] = r.name
			f["properties"]["_title"] = r.title
			f["properties"]["_farm"] = r.farm
			f["properties"]["_feature_role"] = r.feature_role
			f["properties"]["_reference_doctype"] = r.reference_doctype
			f["properties"]["_reference_name"] = r.reference_name
			f["properties"]["_layer"] = r.layer
			f["properties"]["color"] = color
			f["properties"]["marker_icon"] = marker_icon
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


# --- Dashboard-facing helpers ---------------------------------------------
#
# The functions below don't fetch/write geometry themselves — they answer
# the "what's out there" questions a dashboard needs before it can even
# draw a map: which ERP doctypes actually have spatial data, how many
# features per layer/geometry type, and what layers exist at all (whether
# or not they're formally configured on a Spatial Entity Config yet).

@frappe.whitelist()
def get_referenced_doctypes():
	"""Which ERP doctypes actually have Spatial Feature records, with
	counts, permission-filtered. A Spatial Feature's own read permission is
	broad (see its doctype's "All" role), but that doesn't mean every user
	may read whatever it references (e.g. an HR-only doctype) — so each
	distinct reference_doctype is checked against the calling user's actual
	read permission on THAT doctype before being included."""
	rows = frappe.get_all(
		"Spatial Feature",
		filters={"reference_doctype": ("is", "set")},
		# Dict syntax required here — this frappe version's query builder
		# rejects "count(name) as x" as a raw SQL-function string in fields.
		fields=["reference_doctype as doctype", {"COUNT": "name", "as": "feature_count"}],
		group_by="reference_doctype",
		order_by="feature_count desc",
	)
	return [
		r for r in rows
		if frappe.db.exists("DocType", r.doctype) and frappe.has_permission(r.doctype, "read")
	]


@frappe.whitelist()
def get_feature_summary(farm=None, layer=None):
	"""Counts grouped by layer and by geometry_type (two breakdowns in one
	call), plus a grand total — the numbers behind a dashboard's summary
	cards/legend. Filterable by farm and/or layer so a farm-scoped or
	layer-scoped dashboard view gets its own summary rather than the global
	one."""
	filters = {}
	if farm:
		filters["farm"] = farm
	if layer:
		filters["layer"] = layer

	by_layer = frappe.get_all(
		"Spatial Feature", filters=filters, fields=["layer", {"COUNT": "name", "as": "n"}],
		group_by="layer", order_by="n desc",
	)
	by_type = frappe.get_all(
		"Spatial Feature", filters=filters, fields=["geometry_type", {"COUNT": "name", "as": "n"}],
		group_by="geometry_type", order_by="n desc",
	)
	total = frappe.db.count("Spatial Feature", filters)
	return {"total": total, "by_layer": by_layer, "by_geometry_type": by_type}


@frappe.whitelist()
def get_layers():
	"""The distinct set of browsable map layers: every configured layer
	(a Spatial Entity Allowed Geometry row with a non-blank layer_name,
	deduped by layer_name, carrying its color/marker_icon) PLUS any `layer`
	value actually present on real Spatial Feature records that isn't
	already in that configured set — features saved before layer_name
	existed, or under a reference_doctype with no Spatial Entity Config row
	at all, still need to show up as a browsable layer, just without custom
	styling.

	When two config rows (on different reference_doctypes) use the same
	layer_name with different color/marker_icon, one is picked
	deterministically: the alphabetically-first reference_doctype for that
	layer_name wins (see ORDER BY below) — arbitrary, but stable, and a
	dashboard is free to override styling per-doctype itself if that ever
	matters."""
	configured = frappe.db.sql(
		"""
		SELECT g.layer_name AS layer_name, g.color AS color, g.marker_icon AS marker_icon
		FROM `tabSpatial Entity Allowed Geometry` g
		WHERE g.layer_name IS NOT NULL AND g.layer_name != ''
		ORDER BY g.layer_name ASC, g.parent ASC
		""",
		as_dict=True,
	)

	by_name = {}
	for row in configured:
		if row.layer_name not in by_name:
			by_name[row.layer_name] = {
				"layer_name": row.layer_name,
				"color": row.color,
				"marker_icon": row.marker_icon,
				"configured": True,
			}

	feature_layers = frappe.get_all(
		"Spatial Feature",
		filters={"layer": ("is", "set")},
		fields=["layer"],
		group_by="layer",
	)
	for row in feature_layers:
		if row.layer and row.layer not in by_name:
			by_name[row.layer] = {
				"layer_name": row.layer,
				"color": None,
				"marker_icon": None,
				"configured": False,
			}

	return sorted(by_name.values(), key=lambda r: r["layer_name"])


# Property keys get_features_geojson prefixes with "_" purely for
# round-tripping ownership back through import_features below — these
# aren't real feature metadata, so they're stripped back out before
# whatever's left gets saved as a feature's own `properties` JSON again.
_ROUNDTRIP_PROPERTY_KEYS = {
	"_spatial_feature_name", "_title", "_farm", "_company",
	"_feature_role", "_reference_doctype", "_reference_name", "_layer",
	"color", "marker_icon",
}


@frappe.whitelist()
def import_features(
	features,
	default_reference_doctype=None,
	default_farm=None,
	default_company=None,
	source_module=None,
):
	"""Batch import GeoJSON Features via upsert_feature (reused, not
	duplicated) — accepts a JSON string or already-parsed list/dict, and
	either a bare list of Features or a whole FeatureCollection.

	For each feature, reference_doctype/reference_name/feature_role/farm/
	company/title are read off the Feature's own `properties` first — the
	exact _reference_doctype/_reference_name/_feature_role/_farm/_title
	(and _company) keys get_features_geojson already writes — falling back
	to this call's default_* args when a property is absent. That means an
	export (get_features_geojson) → reimport (this function) round-trip
	preserves ownership with no extra mapping required by the caller.

	One bad feature never aborts the batch: each feature's upsert runs in
	its own try/except, logged via frappe.log_error and skipped, same
	soft-fail spirit as upande_security's gate_dispatch.py. Returns
	{"imported": n, "failed": [{"index": i, "error": "..."}, ...]}."""
	if isinstance(features, str):
		features = json.loads(features)

	if isinstance(features, dict) and features.get("type") == "FeatureCollection":
		feature_list = features.get("features") or []
	elif isinstance(features, dict) and features.get("type") == "Feature":
		feature_list = [features]
	elif isinstance(features, list):
		feature_list = features
	else:
		frappe.throw(_("features must be a GeoJSON Feature, a FeatureCollection, or a list of Features."))

	imported = 0
	failed = []

	for i, feat in enumerate(feature_list):
		try:
			if not isinstance(feat, dict):
				raise ValueError("Not a GeoJSON Feature object.")
			geometry = feat.get("geometry")
			if not geometry:
				raise ValueError("Feature has no geometry.")

			props = feat.get("properties") or {}
			extra_properties = {k: v for k, v in props.items() if k not in _ROUNDTRIP_PROPERTY_KEYS}

			upsert_feature(
				geometry=geometry,
				# Matching by the feature's own record name (round-tripped via
				# _spatial_feature_name, the same key get_features_geojson
				# already stamps into every exported feature's properties)
				# takes priority inside upsert_feature whenever it still
				# resolves to a real record - this is what makes a re-import
				# of a previous export update existing features in place
				# instead of duplicating them, for standalone features (which
				# have no reference_doctype/reference_name to match on at
				# all) exactly as much as reference-linked ones.
				name=props.get("_spatial_feature_name"),
				reference_doctype=props.get("_reference_doctype") or default_reference_doctype,
				reference_name=props.get("_reference_name"),
				feature_role=props.get("_feature_role"),
				farm=props.get("_farm") or default_farm,
				company=props.get("_company") or default_company,
				source_module=source_module,
				title=props.get("_title"),
				properties=extra_properties or None,
			)
			imported += 1
		except Exception as e:
			frappe.log_error("Spatial import_features", f"index {i}: {e}")
			failed.append({"index": i, "error": str(e)})

	return {"imported": imported, "failed": failed}


# --- Sharing -----------------------------------------------------------
#
# Thin wrappers around Frappe's native DocShare so a dashboard (Spatial
# Studio) can let a user share one feature with a colleague without going
# to the Desk UI. frappe.share.add() checks the calling user's own "share"
# permission on the target doc internally (see check_share_permission in
# frappe/share.py) before doing anything, and get_feature_shares/
# unshare_feature each gate on doc.check_permission(...) below explicitly -
# so nothing here is a blanket ignore_permissions bypass of OUR permission
# model. The one narrow exception is documented on unshare_feature itself:
# the actual DocShare row deletion has to bypass DocShare's OWN doctype
# permissions (System Manager only), same as Frappe's Desk un-share flow
# does, but only after we've independently verified the caller may share
# the Spatial Feature being un-shared.

@frappe.whitelist()
def share_feature(name, user, read=1, write=0, submit=0, share=0):
	"""Give `user` read/write access to Spatial Feature `name`. Idempotent —
	sharing the same user again just updates their existing share rather
	than erroring or duplicating it (frappe.share.add's native behaviour)."""
	doc = frappe.get_doc("Spatial Feature", name)
	doc.check_permission("read")
	frappe.share.add(
		"Spatial Feature",
		name,
		user,
		read=cint(read),
		write=cint(write),
		submit=cint(submit),
		share=cint(share),
		notify=1,
	)
	return get_feature_shares(name)


@frappe.whitelist()
def get_feature_shares(name):
	"""Who a feature is currently shared with, and what access they have."""
	doc = frappe.get_doc("Spatial Feature", name)
	doc.check_permission("read")
	return frappe.share.get_users("Spatial Feature", name)


@frappe.whitelist()
def unshare_feature(name, user):
	"""Remove `user`'s share on Spatial Feature `name`. Safe to call even if
	nothing was shared with them — frappe.share.remove() no-ops in that
	case rather than throwing.

	Unlike frappe.share.add(), frappe.share.remove() does a plain
	frappe.delete_doc("DocShare", ...) with NO permission check of its own
	- and DocShare's own doctype permissions only grant delete to System
	Manager, so a caller who is fully entitled to manage this Spatial
	Feature's shares (has "share" on it) would otherwise hit PermissionError
	just from DocShare's unrelated doctype-level lockdown. Same fix Frappe's
	own Desk un-share flow uses: check the caller's actual "share"
	permission on the Spatial Feature ourselves first, then bypass
	DocShare's doctype permissions for the delete itself (flags=
	{"ignore_permissions": True} - frappe.share.remove forwards `flags`
	straight into frappe.delete_doc, which applies it to the DocShare doc's
	own .flags.ignore_permissions, the same mechanism the Desk flow uses
	directly). This never bypasses OUR permission model - only DocShare's,
	and only after doc.check_permission("share") above has already passed."""
	doc = frappe.get_doc("Spatial Feature", name)
	doc.check_permission("share")
	frappe.share.remove("Spatial Feature", name, user, flags={"ignore_permissions": True})
	return get_feature_shares(name)
