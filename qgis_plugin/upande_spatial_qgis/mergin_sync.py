"""ERP layers <-> a GeoPackage that Mergin Maps syncs to the field app.

Mergin Maps only syncs files (GeoPackages), not the plugin's in-memory ERP
layers, so field work goes through a GeoPackage in the Mergin project:

    ERP  <->  sync_gpkg()  <->  project .gpkg  <->  Mergin sync  <->  phones

export_to_gpkg() writes chosen ERP layers into the GeoPackage, one table per
ERP layer, with the same columns as an ERP layer in QGIS (`_name` is the
Spatial Feature, `_modified` its ERP version stamp, every property its own
column). Two bookkeeping tables sit beside them and travel with the file:

    upande_sync        which table holds which ERP layer (+ farm filter)
    upande_sync_state  per feature: a fingerprint of the row as last synced,
                       and its ERP `_modified` at that time

sync_gpkg() then works out, per row, who changed what since the last sync:

    row changed here, ERP not        -> send to ERP (update)
    row unchanged here, ERP changed  -> refresh the row from ERP
    both changed                     -> conflict: ERP refuses it, row kept
    row without `_name`              -> new in the field -> created in ERP
    row gone here, still in ERP      -> deleted in the field -> deleted in ERP
                                        (only when allowed, see `deletes`)
    row unchanged, gone from ERP     -> removed here
    new in ERP                       -> added here

ERP's own conflict check (`_modified`) guards every update and delete, so a
field edit never silently overwrites an office edit. No QGIS GUI here -
testable headlessly; plugin.py adds the menu actions."""

import datetime
import hashlib
import json
import os
import re

from qgis.core import (
	QgsCoordinateTransformContext,
	QgsFeature,
	QgsFeatureRequest,
	QgsField,
	QgsJsonUtils,
	QgsVectorFileWriter,
	QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from . import layers as L

META_TABLE = "upande_sync"
STATE_TABLE = "upande_sync_state"
# Set by ERP - never part of "did the field change this row?".
COMPUTED = {"_name", "_modified", "_area_sq_m", "_length_m", "_layer", "_checked_out_by", "_source_module"}
CHUNK = 200


def table_name(layer_name, geometry_type, farm=None):
	base = re.sub(r"[^a-z0-9]+", "_", f"{layer_name} {geometry_type or ''} {farm or ''}".lower()).strip("_")
	return base or "erp_layer"


def _now():
	return datetime.datetime.now().isoformat(timespec="seconds")


# ── bookkeeping tables ────────────────────────────────────────────
def _table(gpkg, name):
	if not os.path.exists(gpkg):
		return None
	vl = QgsVectorLayer(f"{gpkg}|layername={name}", name, "ogr")
	return vl if vl.isValid() else None


def _ensure_table(gpkg, name, fields):
	vl = _table(gpkg, name)
	if vl is not None:
		return vl
	mem = QgsVectorLayer("None", name, "memory")
	mem.dataProvider().addAttributes(fields)
	mem.updateFields()
	_write(mem, gpkg, name)
	return _table(gpkg, name)


def _meta_table(gpkg):
	return _ensure_table(gpkg, META_TABLE, [
		QgsField("table_name", QMetaType.Type.QString),
		QgsField("erp_layer", QMetaType.Type.QString),
		QgsField("geometry_type", QMetaType.Type.QString),
		QgsField("farm", QMetaType.Type.QString),
		QgsField("site", QMetaType.Type.QString),
		QgsField("json_keys", QMetaType.Type.QString),
		QgsField("last_sync", QMetaType.Type.QString),
	])


def _state_table(gpkg):
	return _ensure_table(gpkg, STATE_TABLE, [
		QgsField("table_name", QMetaType.Type.QString),
		QgsField("name", QMetaType.Type.QString),
		QgsField("row_hash", QMetaType.Type.QString),
		QgsField("modified", QMetaType.Type.QString),
	])


def synced_tables(gpkg):
	"""[{table_name, erp_layer, geometry_type, farm, site, json_keys}] of a GeoPackage."""
	meta = _table(gpkg, META_TABLE)
	if meta is None:
		return []
	return [{f.name(): L._py(ft[f.name()]) for f in meta.fields() if f.name() != "fid"} for ft in meta.getFeatures()]


def _set_meta(gpkg, info):
	meta = _meta_table(gpkg)
	existing = [ft.id() for ft in meta.getFeatures() if ft["table_name"] == info["table_name"]]
	pr = meta.dataProvider()
	if existing:
		pr.deleteFeatures(existing)
	ft = QgsFeature(meta.fields())
	for k, v in info.items():
		if meta.fields().indexOf(k) >= 0:
			ft[k] = v
	pr.addFeatures([ft])


def _load_state(gpkg, table):
	st = _state_table(gpkg)
	req = QgsFeatureRequest().setFilterExpression(f"\"table_name\" = '{table}'")
	return {ft["name"]: {"hash": ft["row_hash"], "modified": L._py(ft["modified"])} for ft in st.getFeatures(req)}


def _save_state(gpkg, table, state):
	st = _state_table(gpkg)
	pr = st.dataProvider()
	req = QgsFeatureRequest().setFilterExpression(f"\"table_name\" = '{table}'")
	pr.deleteFeatures([ft.id() for ft in st.getFeatures(req)])
	rows = []
	for name, s in state.items():
		ft = QgsFeature(st.fields())
		ft["table_name"], ft["name"], ft["row_hash"], ft["modified"] = table, name, s["hash"], s.get("modified")
		rows.append(ft)
	pr.addFeatures(rows)


# ── rows ──────────────────────────────────────────────────────────
def row_hash(feature, fields):
	"""Fingerprint of what a field user can change: geometry + every
	attribute ERP doesn't compute itself."""
	attrs = {f.name(): L._py(feature[f.name()]) for f in fields if f.name() not in COMPUTED and f.name() != "fid"}
	geom = feature.geometry()
	g = "" if geom is None or geom.isNull() else geom.asJson(7)
	return hashlib.sha1(json.dumps([attrs, g], sort_keys=True, default=str).encode()).hexdigest()


def _write(layer, gpkg, name):
	o = QgsVectorFileWriter.SaveVectorOptions()
	o.driverName = "GPKG"
	o.layerName = name
	o.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer if os.path.exists(gpkg)
		else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
	err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(layer, gpkg, QgsCoordinateTransformContext(), o)
	if err != QgsVectorFileWriter.WriterError.NoError:
		raise RuntimeError(f"Could not write {name} to {gpkg}: {msg}")


def _payload(gl, ft, json_keys):
	"""A GeoPackage row -> apply_changes item body (attributes + geometry)."""
	attrs = {}
	for f in gl.fields():
		n = f.name()
		if n == "fid":
			continue
		v = L._py(ft[n])
		if n in json_keys and isinstance(v, str):
			try:
				v = json.loads(v)
			except ValueError:
				pass
		attrs[n] = v
	return {"attributes": attrs, "geometry": L.geometry_json(ft.geometry(), gl.crs())}


def _erp_values(gl, feature, json_keys):
	"""ERP GeoJSON feature -> {field index: value} for this table, adding
	columns for property keys the table doesn't have yet."""
	props = feature.get("properties") or {}
	missing = [k for k in props if gl.fields().indexOf(k) < 0]
	if missing:
		gl.dataProvider().addAttributes([QgsField(k, L._field_type([props[k]])) for k in missing])
		gl.updateFields()
	jk = set(json_keys)
	out = {}
	for k, v in props.items():
		out[gl.fields().indexOf(k)] = L._attr_value(v, jk, k)
	json_keys[:] = sorted(jk)
	return out


# ── export ────────────────────────────────────────────────────────
def export_to_gpkg(client, gpkg, entries, farm=None, site=None):
	"""Write ERP layers into the GeoPackage (replacing those tables) and
	mark every row as in sync. `entries` = catalog rows ({layer,
	geometry_type, ...}). Returns {table_name: feature count}."""
	out = {}
	for entry in entries:
		fc = client.get_layer(entry["layer"], entry.get("geometry_type"), farm=farm or None)
		mem = L.build_layer(fc, entry["layer"], entry.get("geometry_type"), site or getattr(client, "url", ""), farm=farm, entry=entry)
		name = table_name(entry["layer"], entry.get("geometry_type"), farm)
		_write(mem, gpkg, name)
		gl = _table(gpkg, name)
		state = {ft["_name"]: {"hash": row_hash(ft, gl.fields()), "modified": L._py(ft["_modified"])}
			for ft in gl.getFeatures() if ft["_name"]}
		_save_state(gpkg, name, state)
		_set_meta(gpkg, {
			"table_name": name, "erp_layer": entry["layer"], "geometry_type": entry.get("geometry_type") or "",
			"farm": farm or "", "site": site or getattr(client, "url", ""),
			"json_keys": mem.customProperty(L.PROP + "json_keys") or "[]", "last_sync": _now(),
		})
		out[name] = gl.featureCount()
	return out


# ── sync ──────────────────────────────────────────────────────────
def plan_sync(client, gpkg, table):
	"""Compare one table with ERP. Returns a plan dict (nothing changed yet)."""
	info = next((t for t in synced_tables(gpkg) if t["table_name"] == table), None)
	if info is None:
		raise ValueError(f"{table} is not an ERP table in {gpkg}")
	gl = _table(gpkg, table)
	json_keys = json.loads(info.get("json_keys") or "[]")
	fc = client.get_layer(info["erp_layer"], info.get("geometry_type") or None, farm=info.get("farm") or None)
	erp = {f["properties"]["_name"]: f for f in fc.get("features") or [] if (f.get("properties") or {}).get("_name")}
	state = _load_state(gpkg, table)

	plan = {"info": info, "added": [], "updated": [], "deleted": [], "refresh": [], "remove_local": [], "add_local": [],
		"json_keys": json_keys, "erp": erp, "state": state}
	seen = set()
	for ft in gl.getFeatures():
		name = L._py(ft["_name"])
		if not name:
			if ft.hasGeometry() and not ft.geometry().isEmpty():
				plan["added"].append(ft.id())
			continue
		seen.add(name)
		changed_here = state.get(name, {}).get("hash") != row_hash(ft, gl.fields())
		in_erp = erp.get(name)
		if changed_here:
			plan["updated"].append(ft.id())
		elif in_erp is None:
			plan["remove_local"].append(ft.id())
		elif (in_erp["properties"].get("_modified") or None) != (L._py(ft["_modified"]) or None):
			plan["refresh"].append(ft.id())
	for name, s in state.items():
		if name not in seen and name in erp:
			plan["deleted"].append({"name": name, "modified": s.get("modified")})
	plan["add_local"] = [n for n in erp if n not in seen and n not in state]
	return plan


def summarize(plan):
	return {k: len(plan[k]) for k in ("added", "updated", "deleted", "refresh", "remove_local", "add_local")}


def apply_sync(client, gpkg, plan, deletes=True, force=False):
	"""Carry out a plan_sync() plan: push field changes to ERP, then bring
	ERP's changes into the GeoPackage. Returns a summary with conflicts and
	errors; rows ERP refused stay as they are (and are retried next time)."""
	info = plan["info"]
	table = info["table_name"]
	gl = _table(gpkg, table)
	jk = plan["json_keys"]
	erp, state = plan["erp"], dict(plan["state"])
	pr = gl.dataProvider()
	result = {"created": 0, "updated": 0, "deleted": 0, "refreshed": 0, "added_here": 0, "removed_here": 0,
		"conflicts": [], "errors": [], "skipped_deletes": 0 if deletes else len(plan["deleted"])}

	def feature(fid):
		return next(gl.getFeatures(QgsFeatureRequest(fid)), None)

	# 1. field -> ERP
	added = [{"client_id": str(fid), **_payload(gl, feature(fid), jk)} for fid in plan["added"]]
	updated = []
	for fid in plan["updated"]:
		ft = feature(fid)
		p = _payload(gl, ft, jk)
		updated.append({"name": ft["_name"], "modified": L._py(ft["_modified"]), **p, "_fid": fid})
	deleted = plan["deleted"] if deletes else []
	name_to_fid = {L._py(ft["_name"]): ft.id() for ft in gl.getFeatures() if ft["_name"]}
	sent_ok = set()  # fids ERP accepted -> mark in sync

	items = [("added", a) for a in added] + [("updated", u) for u in updated] + [("deleted", d) for d in deleted]
	for i in range(0, len(items), CHUNK):
		chunk = items[i:i + CHUNK]
		changes = {"added": [], "updated": [], "deleted": []}
		for kind, item in chunk:
			changes[kind].append({k: v for k, v in item.items() if k != "_fid"})
		res = client.apply_changes(changes, force=force)
		writes = {}
		idx = {n: gl.fields().indexOf(n) for n in ("_name", "_modified", "_area_sq_m", "_length_m", "_layer")}
		for client_id, name in (res.get("created") or {}).items():
			fid = int(client_id)
			writes.setdefault(fid, {})[idx["_name"]] = name
			name_to_fid[name] = fid
			sent_ok.add(fid)
			result["created"] += 1
		for name, m in (res.get("modified") or {}).items():
			fid = name_to_fid.get(name)
			if fid is None:
				continue
			w = writes.setdefault(fid, {})
			w[idx["_modified"]] = m.get("modified")
			for key, col in (("area_sq_m", "_area_sq_m"), ("length_m", "_length_m"), ("layer", "_layer")):
				if idx[col] >= 0:
					w[idx[col]] = m.get(key)
			sent_ok.add(fid)
		result["updated"] += sum(1 for kind, u in chunk if kind == "updated" and u["_fid"] in sent_ok)
		for name in res.get("deleted") or []:
			state.pop(name, None)
			result["deleted"] += 1
		result["conflicts"] += res.get("conflicts") or []
		result["errors"] += res.get("errors") or []
		if writes:
			pr.changeAttributeValues({fid: {k: v for k, v in w.items() if k >= 0} for fid, w in writes.items()})

	# 2. ERP -> field
	for fid in plan["refresh"]:
		ft = feature(fid)
		src = erp[ft["_name"]]
		pr.changeAttributeValues({fid: _erp_values(gl, src, jk)})
		pr.changeGeometryValues({fid: QgsJsonUtils.geometryFromGeoJson(json.dumps(src["geometry"]))})
		sent_ok.add(fid)
		result["refreshed"] += 1
	if plan["remove_local"]:
		for fid in plan["remove_local"]:
			state.pop(L._py(feature(fid)["_name"]), None)
		pr.deleteFeatures(plan["remove_local"])
		result["removed_here"] = len(plan["remove_local"])
	new_rows = []
	for name in plan["add_local"]:
		src = erp[name]
		vals = _erp_values(gl, src, jk)
		ft = QgsFeature(gl.fields())
		for i, v in vals.items():
			ft.setAttribute(i, v)
		ft.setGeometry(QgsJsonUtils.geometryFromGeoJson(json.dumps(src["geometry"])))
		new_rows.append(ft)
	if new_rows:
		ok, added_feats = pr.addFeatures(new_rows)
		for ft in added_feats:
			sent_ok.add(ft.id())
		result["added_here"] = len(new_rows)

	# 3. fingerprints of every row now in step with ERP
	gl.reload()
	for ft in gl.getFeatures():
		if ft["_name"] and (ft.id() in sent_ok or ft["_name"] not in state):
			state[ft["_name"]] = {"hash": row_hash(ft, gl.fields()), "modified": L._py(ft["_modified"])}
	_save_state(gpkg, table, state)
	info = dict(info, json_keys=json.dumps(jk), last_sync=_now())
	_set_meta(gpkg, info)
	return result


def sync_gpkg(client, gpkg, deletes=True, force=False):
	"""Plan and apply every ERP table in a GeoPackage. {table: result}."""
	return {t["table_name"]: apply_sync(client, gpkg, plan_sync(client, gpkg, t["table_name"]), deletes=deletes, force=force)
		for t in synced_tables(gpkg)}


def resolve_conflicts(client, gpkg, table, names, keep):
	"""Settle rows ERP refused as conflicts. keep="field" overwrites ERP with
	the GeoPackage row; keep="erp" throws the field edit away and reloads the
	row from ERP. Returns the apply_sync summary."""
	if keep not in ("field", "erp"):
		raise ValueError("keep must be 'field' or 'erp'")
	plan = plan_sync(client, gpkg, table)
	gl = _table(gpkg, table)
	wanted = {fid for fid in plan["updated"] if L._py(next(gl.getFeatures(QgsFeatureRequest(fid)))["_name"]) in set(names)}
	focused = dict(plan, added=[], deleted=[], remove_local=[], add_local=[], refresh=[], updated=[])
	if keep == "field":
		focused["updated"] = sorted(wanted)
		return apply_sync(client, gpkg, focused, force=True)
	focused["refresh"] = sorted(fid for fid in wanted if L._py(next(gl.getFeatures(QgsFeatureRequest(fid)))["_name"]) in plan["erp"])
	return apply_sync(client, gpkg, focused)
