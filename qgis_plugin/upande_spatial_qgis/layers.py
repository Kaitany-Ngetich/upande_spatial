"""ERP layers in QGIS, and pushing their edits back to ERP live.

An ERP layer is a QGIS memory layer (EPSG:4326, one geometry type) holding
one Upande Spatial layer. Its attributes are the Spatial Feature's own
columns (`_`-prefixed, mostly read-only) plus every key of the features'
`properties` JSON. Custom layer properties remember where it came from, so
it can be reloaded, and re-attached after a project is reopened.

Live sync: when the user saves edits (commits), LayerSync collects that
commit's adds/attribute edits/geometry edits/deletes from QGIS's commit
signals and sends them to ERP in ONE apply_changes call once the commit has
finished, then writes ERP's answers (new record names, new modified stamps,
recomputed area/length) back into the layer at provider level - outside any
edit session, so that never triggers another sync. A feature ERP refused
keeps an empty `_name` (for adds) and is listed in the message bar; "Push
unsynced" retries every feature without a `_name`."""

import json

from qgis.core import (
	QgsCoordinateReferenceSystem,
	QgsCoordinateTransform,
	QgsFeature,
	QgsFeatureRequest,
	QgsField,
	QgsGeometry,
	QgsJsonUtils,
	QgsProject,
	QgsVectorLayer,
	QgsWkbTypes,
)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType, QObject, QTime, pyqtSignal

PROP = "upande_spatial/"  # custom layer property prefix

META_FIELDS = [
	("_name", QMetaType.Type.QString),
	("_title", QMetaType.Type.QString),
	("_feature_role", QMetaType.Type.QString),
	("_reference_doctype", QMetaType.Type.QString),
	("_reference_name", QMetaType.Type.QString),
	("_farm", QMetaType.Type.QString),
	("_company", QMetaType.Type.QString),
	("_layer", QMetaType.Type.QString),
	("_source_module", QMetaType.Type.QString),
	("_area_sq_m", QMetaType.Type.Double),
	("_length_m", QMetaType.Type.Double),
	("_checked_out_by", QMetaType.Type.QString),
	("_modified", QMetaType.Type.QString),
]
# Set by ERP, never by the user.
READ_ONLY = {"_name", "_layer", "_area_sq_m", "_length_m", "_checked_out_by", "_modified", "_source_module"}
META_NAMES = {n for n, _ in META_FIELDS}

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def is_erp_layer(layer):
	return isinstance(layer, QgsVectorLayer) and bool(layer.customProperty(PROP + "layer"))


def _py(value):
	"""QVariant-ish attribute value -> plain JSON-able Python."""
	if value is None:
		return None
	try:
		from qgis.PyQt.QtCore import QVariant

		if isinstance(value, QVariant) and value.isNull():
			return None
	except ImportError:
		pass
	if isinstance(value, (QDate, QDateTime, QTime)):
		return value.toString("yyyy-MM-dd" if isinstance(value, QDate) else "yyyy-MM-dd HH:mm:ss")
	if isinstance(value, float) and value != value:  # NaN
		return None
	return value


def _field_type(values):
	"""Attribute type for one property key: Double when every non-blank
	value is a number (bools excluded), else String."""
	seen = [v for v in values if v not in (None, "")]
	if seen and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
		return QMetaType.Type.Double
	return QMetaType.Type.QString


def _attr_value(value, json_keys, key):
	if isinstance(value, (dict, list)):
		json_keys.add(key)
		return json.dumps(value)
	if isinstance(value, bool):
		return int(value)
	return value


def geometry_type_uri(geometry_type):
	return {
		"Point": "Point", "MultiPoint": "MultiPoint",
		"LineString": "LineString", "MultiLineString": "MultiLineString",
		"Polygon": "Polygon", "MultiPolygon": "MultiPolygon",
	}.get(geometry_type or "", "Unknown")


def build_layer(fc, layer_name, geometry_type, site, farm=None, entry=None, title=None):
	"""A new memory layer from get_layer()'s FeatureCollection. `entry` is
	the layer's catalog() row: its Spatial Entity Config style and the
	owner type/role features drawn in it belong to."""
	entry = entry or {}
	vl = QgsVectorLayer(f"{geometry_type_uri(geometry_type)}?crs=EPSG:4326", title or layer_name, "memory")
	vl.setCustomProperty(PROP + "layer", layer_name)
	vl.setCustomProperty(PROP + "geometry_type", geometry_type or "")
	vl.setCustomProperty(PROP + "site", site)
	vl.setCustomProperty(PROP + "farm", farm or "")
	base = {}
	cfg = entry.get("defaults") or {}
	if cfg.get("reference_doctype"):
		base["_reference_doctype"] = cfg["reference_doctype"]
		if cfg.get("feature_role"):
			base["_feature_role"] = cfg["feature_role"]
		if farm and cfg["reference_doctype"] == "Farm":
			base["_reference_name"] = farm
	if farm:
		base["_farm"] = farm
	vl.setCustomProperty(PROP + "base_defaults", json.dumps(base))
	fill_layer(vl, fc)
	apply_style(vl, entry.get("style") or {})
	return vl


def apply_style(vl, style):
	"""The Map Viewer's colour / line width / fill opacity for this layer
	(Spatial Entity Config) - so QGIS and ERP look alike. Unset values keep
	QGIS's defaults."""
	from qgis.PyQt.QtGui import QColor

	color = style.get("color")
	if not color or not QColor(color).isValid():
		return
	renderer = vl.renderer()
	symbol = renderer.symbol() if renderer is not None and hasattr(renderer, "symbol") else None
	if symbol is None:
		return
	c = QColor(color)
	gtype = QgsWkbTypes.geometryType(vl.wkbType())
	if gtype == QgsWkbTypes.GeometryType.PolygonGeometry:
		fill = QColor(c)
		fill.setAlphaF(float(style.get("fill_opacity") or 0.25))
		symbol.setColor(fill)
		layer = symbol.symbolLayer(0)
		if hasattr(layer, "setStrokeColor"):
			layer.setStrokeColor(c)
			layer.setStrokeWidth(float(style.get("line_width") or 0.6))
	elif gtype == QgsWkbTypes.GeometryType.LineGeometry:
		symbol.setColor(c)
		symbol.setWidth(float(style.get("line_width") or 0.8))
	else:
		symbol.setColor(c)
		symbol.setSize(3)
	vl.triggerRepaint()


def fill_layer(vl, fc):
	"""(Re)load a layer's features from a FeatureCollection, adding any new
	property keys as attributes. Works on a fresh or an existing ERP layer
	(Reload keeps the layer's styling, labels and position in the tree)."""
	features = fc.get("features") or []
	pr = vl.dataProvider()
	existing = {f.name() for f in vl.fields()}
	new_fields = [QgsField(n, t) for n, t in META_FIELDS if n not in existing]

	prop_keys = []
	for f in features:
		for k in (f.get("properties") or {}):
			if k not in META_NAMES and k not in prop_keys:
				prop_keys.append(k)
	for k in prop_keys:
		if k not in existing:
			new_fields.append(QgsField(k, _field_type([(f.get("properties") or {}).get(k) for f in features])))
	if new_fields:
		pr.addAttributes(new_fields)
		vl.updateFields()

	json_keys = set(json.loads(vl.customProperty(PROP + "json_keys") or "[]"))
	fields = vl.fields()
	qfeats = []
	for f in features:
		geom = QgsJsonUtils.geometryFromGeoJson(json.dumps(f["geometry"]))
		if geom.isNull():
			continue
		qf = QgsFeature(fields)
		qf.setGeometry(geom)
		for k, v in (f.get("properties") or {}).items():
			idx = fields.indexOf(k)
			if idx >= 0:
				qf.setAttribute(idx, _attr_value(v, json_keys, k))
		qfeats.append(qf)

	pr.truncate()
	pr.addFeatures(qfeats)
	vl.setCustomProperty(PROP + "json_keys", json.dumps(sorted(json_keys)))
	_set_layer_defaults(vl, features)
	_lock_meta_fields(vl)
	vl.updateExtents()
	vl.triggerRepaint()
	return len(qfeats)


def _set_layer_defaults(vl, features):
	"""New features drawn in QGIS inherit an owner/role/farm: the layer's
	config (owner type + role, plus the farm filter - see build_layer), then
	whatever every loaded feature has in common (e.g. one Farm's pipes).
	Anything still missing the user fills into `_reference_*` themselves."""
	defaults = json.loads(vl.customProperty(PROP + "base_defaults") or "{}")
	for key in ("_reference_doctype", "_reference_name", "_feature_role", "_farm", "_company"):
		values = {(f.get("properties") or {}).get(key) for f in features}
		values.discard(None)
		if len(values) == 1:
			defaults[key] = values.pop()
	vl.setCustomProperty(PROP + "defaults", json.dumps(defaults))
	fields = vl.fields()
	from qgis.core import QgsDefaultValue

	for key in ("_reference_doctype", "_reference_name", "_feature_role", "_farm", "_company"):
		idx = fields.indexOf(key)
		if idx >= 0 and key not in defaults:
			vl.setDefaultValueDefinition(idx, QgsDefaultValue())  # clear a stale default from an earlier load
	for key, value in defaults.items():
		idx = fields.indexOf(key)
		if idx >= 0:
			literal = "'" + str(value).replace("'", "''") + "'"  # QGIS expression string literal
			vl.setDefaultValueDefinition(idx, QgsDefaultValue(literal))


def _lock_meta_fields(vl):
	cfg = vl.editFormConfig()
	for i, f in enumerate(vl.fields()):
		if f.name() in READ_ONLY:
			cfg.setReadOnly(i, True)
	vl.setEditFormConfig(cfg)


def feature_payload(vl, feature, with_geometry=True):
	"""One QGIS feature -> {"geometry", "attributes"} for apply_changes."""
	json_keys = set(json.loads(vl.customProperty(PROP + "json_keys") or "[]"))
	attrs = {}
	for field in vl.fields():
		name = field.name()
		value = _py(feature[name])
		if name in json_keys and isinstance(value, str):
			try:
				value = json.loads(value)
			except ValueError:
				pass
		attrs[name] = value
	out = {"attributes": attrs}
	if with_geometry:
		out["geometry"] = geometry_json(feature.geometry(), vl.crs())
	return out


def geometry_json(geom, crs):
	g = QgsGeometry(geom)
	if crs.isValid() and crs != WGS84:
		g.transform(QgsCoordinateTransform(crs, WGS84, QgsProject.instance()))
	return json.loads(g.asJson(8))


class LayerSync(QObject):
	"""Watches one ERP layer and pushes each commit to ERP.

	`client` is an ErpClient; results are announced through `pushed`
	(summary dict) so the UI can show them without this class knowing
	about iface/message bars - which keeps it testable headlessly."""

	pushed = pyqtSignal(object, dict)  # (layer, summary)

	def __init__(self, layer, client):
		super().__init__(layer)
		self.layer = layer
		self.client = client
		self.paused = False
		self.pending = []  # change sets ERP never received (offline / logged out)
		self._reset()
		layer.beforeCommitChanges.connect(self._before_commit)
		layer.committedFeaturesAdded.connect(self._added)
		layer.committedAttributeValuesChanges.connect(self._attrs_changed)
		layer.committedGeometriesChanges.connect(self._geoms_changed)
		layer.committedFeaturesRemoved.connect(self._removed)
		layer.afterCommitChanges.connect(self._flush)

	def _reset(self):
		self._new_fids, self._changed_attr, self._changed_geom, self._deleted = [], set(), set(), {}

	def _before_commit(self, *_):
		if self.paused:
			return
		self._reset()
		ids = list(self.layer.editBuffer().deletedFeatureIds()) if self.layer.editBuffer() else []
		if ids:
			req = QgsFeatureRequest().setFilterFids(ids)
			for f in self.layer.dataProvider().getFeatures(req):
				if f["_name"]:
					self._deleted[f.id()] = {"name": f["_name"], "modified": _py(f["_modified"])}

	def _added(self, _layer_id, features):
		self._new_fids += [f.id() for f in features]

	def _attrs_changed(self, _layer_id, changes):
		self._changed_attr |= set(changes.keys())

	def _geoms_changed(self, _layer_id, changes):
		self._changed_geom |= set(changes.keys())

	def _removed(self, _layer_id, fids):
		pass  # names already captured in _before_commit, while still readable

	def _flush(self):
		if self.paused:
			return
		changes = {"added": [], "updated": [], "deleted": list(self._deleted.values())}
		for fid in self._new_fids:
			f = self._feature(fid)
			if f is not None and not f["_name"]:
				changes["added"].append({"client_id": str(fid), **feature_payload(self.layer, f)})
		for fid in (self._changed_attr | self._changed_geom) - set(self._new_fids):
			f = self._feature(fid)
			if f is None:
				continue
			if not f["_name"]:
				# Never made it to ERP - its edit is really still an add.
				changes["added"].append({"client_id": str(fid), **feature_payload(self.layer, f)})
				continue
			payload = feature_payload(self.layer, f, with_geometry=fid in self._changed_geom)
			changes["updated"].append({
				"name": f["_name"],
				"modified": _py(f["_modified"]),
				"geometry": payload.get("geometry"),
				"attributes": payload["attributes"] if fid in self._changed_attr else None,
			})
		self._reset()
		if any(changes.values()):
			self.push(changes)

	def _feature(self, fid):
		return next(self.layer.dataProvider().getFeatures(QgsFeatureRequest(fid)), None)

	def push(self, changes, force=False):
		"""Send a change set and write ERP's answers back into the layer."""
		summary = {"added": 0, "updated": 0, "deleted": 0, "conflicts": [], "errors": []}
		try:
			res = self.client.apply_changes(changes, force=force)
		except Exception as e:
			# Nothing reached ERP - keep the whole change set to resend (e.g.
			# after logging in again) rather than losing the user's edits.
			self.pending.append((changes, force))
			summary["errors"].append({"error": str(e)})
			summary["held"] = True
			self.pushed.emit(self.layer, summary)
			return summary

		fields = self.layer.fields()
		idx = {n: fields.indexOf(n) for n in ("_name", "_modified", "_area_sq_m", "_length_m", "_layer")}
		by_name = {}
		for f in self.layer.dataProvider().getFeatures():
			if f["_name"]:
				by_name[f["_name"]] = f.id()
		writes = {}
		for client_id, name in (res.get("created") or {}).items():
			fid = int(client_id)
			writes.setdefault(fid, {})[idx["_name"]] = name
			by_name[name] = fid
		for name, info in (res.get("modified") or {}).items():
			fid = by_name.get(name)
			if fid is None:
				continue
			w = writes.setdefault(fid, {})
			w[idx["_modified"]] = info.get("modified")
			w[idx["_area_sq_m"]] = info.get("area_sq_m")
			w[idx["_length_m"]] = info.get("length_m")
			w[idx["_layer"]] = info.get("layer")
		if writes:
			self.layer.dataProvider().changeAttributeValues(writes)
			self.layer.triggerRepaint()

		summary["added"] = len(res.get("created") or {})
		summary["updated"] = len(res.get("modified") or {}) - summary["added"]
		summary["deleted"] = len(res.get("deleted") or [])
		summary["conflicts"] = res.get("conflicts") or []
		summary["errors"] = res.get("errors") or []
		self.pushed.emit(self.layer, summary)
		return summary

	def retry_pending(self):
		"""Resend change sets that never reached ERP. Returns how many went."""
		pending, self.pending = self.pending, []
		for changes, force in pending:
			self.push(changes, force=force)
		return len(pending) - len(self.pending)

	def push_unsynced(self):
		"""Retry every feature that never got a `_name` (refused or offline)."""
		added = [
			{"client_id": str(f.id()), **feature_payload(self.layer, f)}
			for f in self.layer.dataProvider().getFeatures() if not f["_name"]
		]
		if not added:
			return None
		return self.push({"added": added})

	def force_push(self, fids):
		"""Overwrite ERP with these features as they are in QGIS, ignoring
		the "changed in ERP since loaded" conflict check."""
		updated, added = [], []
		for fid in fids:
			f = self._feature(fid)
			if f is None:
				continue
			p = feature_payload(self.layer, f)
			if f["_name"]:
				updated.append({"name": f["_name"], "geometry": p["geometry"], "attributes": p["attributes"]})
			else:
				added.append({"client_id": str(fid), **p})
		return self.push({"added": added, "updated": updated}, force=True)


def publish_features(client, source_layer, target, features=None, chunk=200):
	"""Send another layer's features (an analysis result, a shapefile, ...)
	to ERP as new Spatial Features. `target` = {reference_doctype,
	reference_name, feature_role, farm, title_field}; every other attribute
	becomes a property. Reprojected to WGS84; nothing in the source layer
	changes. Returns totals and per-feature errors."""
	crs = source_layer.crs()
	title_field = target.get("title_field")
	feats = list(features if features is not None else source_layer.getFeatures())
	total = {"created": 0, "errors": []}
	batch = []

	def send():
		if not batch:
			return
		res = client.apply_changes({"added": list(batch)})
		total["created"] += len(res.get("created") or {})
		total["errors"] += res.get("errors") or []
		batch.clear()

	for f in feats:
		if not f.hasGeometry() or f.geometry().isEmpty():
			continue
		attrs = {}
		for field in source_layer.fields():
			value = _py(f[field.name()])
			if value is not None and not field.name().startswith("_"):
				attrs[field.name()] = value
		attrs["_reference_doctype"] = target.get("reference_doctype") or None
		attrs["_reference_name"] = target.get("reference_name") or None
		attrs["_feature_role"] = target.get("feature_role") or None
		attrs["_farm"] = target.get("farm") or None
		attrs["_source_module"] = "QGIS"
		if title_field:
			attrs["_title"] = _py(f[title_field])
			attrs.pop(title_field, None)
		geom = f.geometry()
		if QgsWkbTypes.hasZ(geom.wkbType()) or QgsWkbTypes.hasM(geom.wkbType()):
			geom = QgsGeometry(geom)
			geom.get().dropZValue()
			geom.get().dropMValue()
		batch.append({"client_id": str(f.id()), "geometry": geometry_json(geom, crs), "attributes": attrs})
		if len(batch) >= chunk:
			send()
	send()
	return total


def build_results_layer(fc, name, geometry_type, site):
	"""Read-only EPANET results layer (nodes or links) for analysis."""
	vl = QgsVectorLayer(f"{geometry_type}?crs=EPSG:4326", name, "memory")
	vl.setCustomProperty(PROP + "results", 1)
	vl.setCustomProperty(PROP + "site", site)
	feats = fc.get("features") or []
	keys = []
	for f in feats:
		for k in f.get("properties") or {}:
			if k not in keys:
				keys.append(k)
	pr = vl.dataProvider()
	pr.addAttributes([QgsField(k, _field_type([(f.get("properties") or {}).get(k) for f in feats])) for k in keys])
	vl.updateFields()
	out = []
	for f in feats:
		qf = QgsFeature(vl.fields())
		qf.setGeometry(QgsJsonUtils.geometryFromGeoJson(json.dumps(f["geometry"])))
		for k, v in (f.get("properties") or {}).items():
			qf[k] = v
		out.append(qf)
	pr.addFeatures(out)
	vl.updateExtents()
	vl.setReadOnly(True)
	return vl
