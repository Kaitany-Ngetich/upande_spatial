# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""Endpoints for the Upande Spatial QGIS plugin (qgis_plugin/ in this repo).

The plugin talks to ERP over the normal REST API with the user's own API
key/secret, so it works against a local bench and a Frappe Cloud site alike
(no database access needed) and every read/write gets exactly that user's
Spatial Feature permissions - same rule as upsert_feature.

QGIS works in layers of one geometry type, so the unit here is a
(layer, geometry_type) pair: catalog() lists them, get_layer() serves one as
GeoJSON with every feature's `properties` keys flattened into attributes,
and apply_changes() takes back one QGIS commit's worth of adds/edits/deletes
in a single round-trip. Writes go through spatial.upsert_feature /
delete_feature, so validation, layer resolution, area/length and check-out
locking behave exactly as they do from the Map Viewer.

Concurrency: every served feature carries `_modified`. An edit sent back
with a `_modified` older than the record's current one is refused as a
conflict (someone else saved it since it was loaded) unless `force` is set,
so a stale QGIS session can't silently overwrite newer ERP edits."""

import json

import frappe
from frappe import _
from frappe.utils import get_datetime

from upande_spatial.api.spatial import _geometry_type_of, delete_feature, upsert_feature

API_VERSION = 1

# Spatial Feature columns the plugin exposes as their own attributes. Anything
# else on a feature lives in its `properties` JSON and is flattened alongside.
META_FIELDS = (
	"name", "title", "feature_role", "reference_doctype", "reference_name",
	"farm", "company", "layer", "source_module", "area_sq_m", "length_m",
	"checked_out_by", "modified",
)

# Attribute names that are metadata, never written back into `properties`.
RESERVED_ATTRS = {
	"_name", "_title", "_feature_role", "_reference_doctype", "_reference_name",
	"_farm", "_company", "_layer", "_source_module", "_area_sq_m", "_length_m",
	"_checked_out_by", "_modified",
}


@frappe.whitelist()
def ping():
	"""Connection test for the plugin's settings dialog."""
	return {
		"user": frappe.session.user,
		"site": frappe.local.site,
		"api_version": API_VERSION,
		"can_write": frappe.has_permission("Spatial Feature", "write"),
		"can_create": frappe.has_permission("Spatial Feature", "create"),
	}


@frappe.whitelist()
def catalog(farm=None):
	"""Every (layer, geometry type) pair with features the user can read,
	with counts - the plugin's browser tree. Also the reference doctypes and
	roles in use, for the Publish dialog's dropdowns, and the EPANET networks
	(whose last results can be loaded as a layer)."""
	filters = {"farm": farm} if farm else {}
	rows = frappe.get_all(
		"Spatial Feature",
		filters=filters,
		fields=["layer", "geometry_type", "reference_doctype", "feature_role", {"COUNT": "name", "as": "n"}],
		group_by="layer, geometry_type, reference_doctype, feature_role",
		order_by="layer asc",
	)
	layers = {}
	owners = {}
	for r in rows:
		key = (r.layer or _("Uncategorized"), r.geometry_type)
		entry = layers.setdefault(key, {"layer": key[0], "geometry_type": key[1], "count": 0, "roles": set(), "reference_doctypes": set()})
		entry["count"] += r.n
		if r.feature_role:
			entry["roles"].add(r.feature_role)
		if r.reference_doctype:
			entry["reference_doctypes"].add(r.reference_doctype)
			owners.setdefault(r.reference_doctype, set())
			if r.feature_role:
				owners[r.reference_doctype].add(r.feature_role)

	# Every layer configured in Spatial Entity Config, even with no features
	# yet - so a QGIS user can load e.g. an empty "Water Tanks" layer and
	# digitise into it. The config row also says which owner doctype/role a
	# feature drawn there belongs to, and how the Map Viewer styles it.
	for row in frappe.get_all(
		"Spatial Entity Allowed Geometry",
		fields=["parent", "feature_role", "geometry_type", "layer_name", "color", "line_width", "fill_opacity"],
		filters={"parenttype": "Spatial Entity Config", "layer_name": ("is", "set")},
		order_by="parent asc, idx asc",
	):
		key = (row.layer_name, row.geometry_type)
		entry = layers.setdefault(key, {"layer": key[0], "geometry_type": key[1], "count": 0, "roles": set(), "reference_doctypes": set()})
		entry.setdefault("configs", []).append({"reference_doctype": row.parent, "feature_role": row.feature_role or ""})
		entry.setdefault("style", {
			"color": row.color or None,
			"line_width": row.line_width or None,
			"fill_opacity": row.fill_opacity or None,
		})
		owners.setdefault(row.parent, set())
		if row.feature_role:
			owners[row.parent].add(row.feature_role)

	for entry in layers.values():
		configs = entry.pop("configs", [])
		# Only a layer that one config row alone feeds has unambiguous defaults.
		entry["defaults"] = configs[0] if len(configs) == 1 else {}

	networks = []
	if frappe.has_permission("EPANET Network", "read"):
		networks = frappe.get_all("EPANET Network", fields=["name", "network_name", "reference_doctype", "reference_name"])

	return {
		"api_version": API_VERSION,
		"layers": [
			{**v, "roles": sorted(v["roles"]), "reference_doctypes": sorted(v["reference_doctypes"])}
			for v in sorted(layers.values(), key=lambda v: (v["layer"], v["geometry_type"] or ""))
		],
		"reference_doctypes": {k: sorted(v) for k, v in sorted(owners.items())},
		"farms": frappe.get_all("Farm", pluck="name", order_by="name asc") if frappe.db.exists("DocType", "Farm") else [],
		"epanet_networks": networks,
	}


def _serve(rows):
	features = []
	for r in rows:
		if not r.geometry:
			continue
		try:
			parsed = json.loads(r.geometry)
			props = json.loads(r.properties) if r.properties else {}
		except Exception:
			continue
		if not isinstance(props, dict):
			props = {}
		for f in parsed.get("features") or []:
			if not f.get("geometry"):
				continue
			attrs = {k: v for k, v in props.items() if k not in RESERVED_ATTRS}
			for field in META_FIELDS:
				value = r.get(field)
				attrs["_" + field] = str(value) if field == "modified" and value else value
			features.append({"type": "Feature", "geometry": f["geometry"], "properties": attrs})
	return {"type": "FeatureCollection", "features": features}


@frappe.whitelist()
def get_layer(layer, geometry_type=None, farm=None, reference_doctype=None):
	"""One layer as GeoJSON, `properties` flattened into attributes and the
	Spatial Feature's own columns as `_`-prefixed ones (see META_FIELDS)."""
	filters, or_filters = {"layer": layer}, None
	if layer == _("Uncategorized"):
		# catalog() files unset layers here; upsert_feature's own fallback
		# also literally names features "Uncategorized".
		filters, or_filters = {}, [["layer", "is", "not set"], ["layer", "=", layer]]
	if geometry_type:
		filters["geometry_type"] = geometry_type
	if farm:
		filters["farm"] = farm
	if reference_doctype:
		filters["reference_doctype"] = reference_doctype
	rows = frappe.get_all(
		"Spatial Feature",
		filters=filters,
		or_filters=or_filters,
		fields=["geometry", "properties", *META_FIELDS],
		limit_page_length=0,
	)
	return _serve(rows)


def _split(attrs):
	"""QGIS attributes -> (meta, properties). Blank/None attributes are
	dropped from properties rather than saved as nulls, matching how the Map
	Viewer forms treat an emptied field."""
	meta = {k[1:]: v for k, v in attrs.items() if k in RESERVED_ATTRS}
	props = {k: v for k, v in attrs.items() if k not in RESERVED_ATTRS and v not in (None, "")}
	return meta, props


def _is_stale(name, sent_modified):
	if not sent_modified:
		return False
	current = frappe.db.get_value("Spatial Feature", name, "modified")
	return current is not None and get_datetime(current) > get_datetime(sent_modified)


def _err(e):
	frappe.clear_last_message()
	if isinstance(e, frappe.PermissionError):
		return _("you don't have permission for this in ERP")
	return str(e) or e.__class__.__name__


@frappe.whitelist()
def apply_changes(changes, force=0):
	"""One QGIS commit, back to ERP in one call:
	{"added":   [{"client_id": .., "geometry": {..}, "attributes": {..}}],
	 "updated": [{"name": .., "geometry": {..}|null, "attributes": {..}|null}],
	 "deleted": [{"name": .., "modified": ..}]}

	`attributes` are the layer's attributes (meta ones `_`-prefixed). An
	added feature needs at least a reference (or is created standalone) and
	inherits the layer's role/owner from the plugin. An update with only
	geometry or only attributes keeps the other as it is in ERP.

	Each item succeeds or fails on its own - one bad feature never blocks
	the rest. Returns created names (client_id -> name), the new `_modified`
	of everything saved (so the plugin can refresh its conflict stamps),
	conflicts and errors."""
	if isinstance(changes, str):
		changes = json.loads(changes)
	force = frappe.utils.cint(force)
	out = {"created": {}, "modified": {}, "deleted": [], "conflicts": [], "errors": []}

	for item in changes.get("added") or []:
		try:
			meta, props = _split(item.get("attributes") or {})
			res = upsert_feature(
				geometry=item["geometry"],
				reference_doctype=meta.get("reference_doctype") or None,
				reference_name=meta.get("reference_name") or None,
				feature_role=meta.get("feature_role") or None,
				farm=meta.get("farm") or None,
				company=meta.get("company") or None,
				title=meta.get("title") or None,
				properties=props,
				source_module=meta.get("source_module") or "QGIS",
			)
			out["created"][str(item.get("client_id"))] = res["name"]
			out["modified"][res["name"]] = _modified_info(res["name"])
		except Exception as e:
			frappe.db.rollback()
			out["errors"].append({"client_id": item.get("client_id"), "error": _err(e)})

	for item in changes.get("updated") or []:
		name = item.get("name")
		try:
			if not name or not frappe.db.exists("Spatial Feature", name):
				raise frappe.DoesNotExistError(_("{0} no longer exists in ERP").format(name or "(blank)"))
			attrs = item.get("attributes")
			sent_modified = item.get("modified") or (attrs or {}).get("_modified")
			if not force and _is_stale(name, sent_modified):
				out["conflicts"].append({"name": name, "error": _("changed in ERP since it was loaded")})
				continue
			doc = frappe.get_doc("Spatial Feature", name)
			geometry = item.get("geometry")
			if not geometry:
				parsed = json.loads(doc.geometry) if doc.geometry else {}
				feats = parsed.get("features") or []
				geometry = feats[0]["geometry"] if feats else None
			if attrs is None:
				props = json.loads(doc.properties) if doc.properties else {}
				meta = {}
			else:
				meta, props = _split(attrs)
			res = upsert_feature(
				geometry=geometry,
				name=name,
				reference_doctype=doc.reference_doctype,
				reference_name=doc.reference_name,
				feature_role=doc.feature_role,
				title=meta.get("title") if "title" in meta else None,
				properties=props,
			)
			out["modified"][res["name"]] = _modified_info(res["name"])
		except Exception as e:
			frappe.db.rollback()
			out["errors"].append({"name": name, "error": _err(e)})

	for item in changes.get("deleted") or []:
		name = item.get("name") if isinstance(item, dict) else item
		try:
			if not force and isinstance(item, dict) and _is_stale(name, item.get("modified")):
				out["conflicts"].append({"name": name, "error": _("changed in ERP since it was loaded - not deleted")})
				continue
			doc = frappe.db.get_value("Spatial Feature", name, ["checked_out_by"], as_dict=True)
			if doc and doc.checked_out_by and doc.checked_out_by != frappe.session.user and "System Manager" not in frappe.get_roles():
				raise frappe.ValidationError(_("checked out by {0}").format(doc.checked_out_by))
			res = delete_feature(name=name)
			if res.get("deleted"):
				out["deleted"].append(name)
		except Exception as e:
			frappe.db.rollback()
			out["errors"].append({"name": name, "error": _err(e)})

	return out


def _modified_info(name):
	row = frappe.db.get_value("Spatial Feature", name, ["modified", "area_sq_m", "length_m", "layer"], as_dict=True)
	return {"modified": str(row.modified), "area_sq_m": row.area_sq_m, "length_m": row.length_m, "layer": row.layer}


@frappe.whitelist()
def epanet_results(network, run=None):
	"""A network's latest (or a given) successful run as two GeoJSON layers -
	nodes with pressure/head/demand/quality, links with flow/velocity/
	headloss - for analysis in QGIS. Read-only: these are results, not data."""
	from upande_spatial.api.epanet import _load_network_features, load_run

	frappe.has_permission("EPANET Network", "read", network, throw=True)
	run_doc, payload = load_run(network, run)
	nodes = payload.get("node_results") or {}
	links = payload.get("link_results") or {}
	types = payload.get("element_types") or {}
	ts = payload.get("timeseries") or {}

	def last(group, attribute, n):
		series = ((ts.get(group) or {}).get(attribute) or {}).get(n) or [None]
		return series[-1]

	flow_units = (payload.get("settings") or {}).get("flow_units")

	out = {"Point": [], "LineString": []}
	for f in _load_network_features(network):
		g = f["_geom"]
		if not g:
			continue
		n = f["name"]
		props = {"_name": n, "_title": f.get("title"), "element": types.get(n) or f["feature_role"], "run": run_doc.name}
		if n in nodes:
			r = nodes[n]
			props.update(pressure_m=r.get("pressure"), head_m=r.get("head"), quality=r.get("quality"),
				demand_m3s=last("node", "demand", n))
		elif n in links:
			r = links[n]
			props.update(flow_m3s=r.get("flow"), velocity_ms=r.get("velocity"), headloss=last("link", "headloss", n))
		else:
			props["excluded"] = 1
		kind = "Point" if g.get("type") == "Point" else "LineString"
		out[kind].append({"type": "Feature", "geometry": g, "properties": props})

	return {
		"run": run_doc.name,
		"run_on": str(run_doc.run_on),
		"flow_units": flow_units,
		"statistic": (payload.get("settings") or {}).get("statistic"),
		"nodes": {"type": "FeatureCollection", "features": out["Point"]},
		"links": {"type": "FeatureCollection", "features": out["LineString"]},
	}
