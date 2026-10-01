"""QGIS plugin entry point: menu/toolbar, the ERP browser dock, and wiring
every loaded ERP layer up to live sync."""

import os
import tempfile

from qgis.core import Qgis, QgsApplication, QgsProject, QgsTask
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.PyQt.QtWidgets import (
	QApplication,
	QComboBox,
	QDockWidget,
	QHBoxLayout,
	QLabel,
	QMessageBox,
	QPushButton,
	QTreeWidget,
	QTreeWidgetItem,
	QVBoxLayout,
	QWidget,
)

from . import layers as L
from .client import SessionExpired
from .dialogs import LoginDialog, PublishDialog

HERE = os.path.dirname(__file__)
ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_DATA = Qt.ItemDataRole.UserRole + 1


class BusyCursor:
	def __enter__(self):
		QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

	def __exit__(self, *exc):
		QApplication.restoreOverrideCursor()


class UpandeSpatialPlugin:
	def __init__(self, iface):
		self.iface = iface
		self.client = None
		self.catalog = {}
		self.syncs = {}  # layer id -> LayerSync
		self.actions = []
		self.dock = None
		self.tasks = set()  # running QgsTasks - held so they aren't garbage-collected

	# ── lifecycle ──────────────────────────────────────────────
	def initGui(self):
		icon = QIcon(os.path.join(HERE, "icon.svg"))
		self.toggle_action = self._action(icon, "Upande Spatial", self.toggle_dock, toolbar=True)
		self._action(None, "Log in to ERP…", self.login)
		self._action(None, "Log out", self.logout)
		self._action(None, "Publish layer to ERP…", self.publish)
		self._action(None, "Reload active ERP layer", self.reload_active)
		self._action(None, "Push unsynced features", self.push_unsynced)
		self._action(None, "Force-push selected features", self.force_push_selected)
		self._action(None, "Check out selected features", lambda: self.checkout_selected(True))
		self._action(None, "Release selected features", lambda: self.checkout_selected(False))
		QgsProject.instance().readProject.connect(self._reattach_project_layers)
		QgsProject.instance().layerWillBeRemoved.connect(self._forget_layer)
		QgsApplication.instance().aboutToQuit.connect(self.logout)

	def unload(self):
		self.logout()
		for a in self.actions:
			self.iface.removePluginVectorMenu("&Upande Spatial", a)
			self.iface.removeToolBarIcon(a)
		try:
			QgsProject.instance().readProject.disconnect(self._reattach_project_layers)
			QgsProject.instance().layerWillBeRemoved.disconnect(self._forget_layer)
			QgsApplication.instance().aboutToQuit.disconnect(self.logout)
		except TypeError:
			pass
		if self.dock:
			self.iface.removeDockWidget(self.dock)
			self.dock.deleteLater()
		self.syncs.clear()

	def _action(self, icon, text, slot, toolbar=False):
		a = QAction(icon, text, self.iface.mainWindow()) if icon else QAction(text, self.iface.mainWindow())
		a.triggered.connect(slot)
		self.iface.addPluginToVectorMenu("&Upande Spatial", a)
		if toolbar:
			self.iface.addToolBarIcon(a)
		self.actions.append(a)
		return a

	# ── helpers ────────────────────────────────────────────────
	def msg(self, text, level=Qgis.MessageLevel.Info, duration=6):
		self.iface.messageBar().pushMessage("Upande Spatial", text, level=level, duration=duration)

	def ensure_client(self, message=None):
		"""A logged-in client, asking the user to log in when there isn't one."""
		if self.client is None or not self.client.logged_in:
			self.login(message)
		return self.client if self.client is not None and self.client.logged_in else None

	def login(self, message=None):
		dlg = LoginDialog(self.iface.mainWindow(), message=message if isinstance(message, str) else None)
		if not dlg.exec():
			return False
		self.client = dlg.client
		for sync in self.syncs.values():
			sync.client = self.client
		self._update_conn_label()
		self.msg(f"Logged in to {self.client.url} as {self.client.user}.", Qgis.MessageLevel.Success, 4)
		resent = sum(sync.retry_pending() for sync in self.syncs.values())
		if resent:
			self.msg(f"Sent {resent} held change set(s) to ERP.", Qgis.MessageLevel.Success)
		if self.dock and self.dock.isVisible():
			self.refresh_catalog()
		return True

	def logout(self, *_):
		if self.client is not None:
			self.client.logout()
		self._update_conn_label()

	def _update_conn_label(self):
		if not self.dock:
			return
		if self.client is not None and self.client.logged_in:
			self.conn_label.setText(f"<b>{self.client.url}</b> · {self.client.user}")
		else:
			self.conn_label.setText("Not logged in")

	def erp_async(self, description, fn, on_done, _retried=False):
		"""Run fn(client) in a QGIS background task (shown in the task bar,
		QGIS stays responsive), then on_done(result) back on the main thread.
		A session that expired meanwhile gets one log-in-and-retry."""
		client = self.ensure_client()
		if client is None:
			return

		def work(task):
			return fn(client)

		def finished(exception, result=None):
			self.tasks.discard(task)
			if exception is None:
				on_done(result)
			elif isinstance(exception, SessionExpired) and not _retried:
				if self.login("Your ERP session has expired - please log in again."):
					self.erp_async(description, fn, on_done, _retried=True)
			else:
				self.msg(f"{description} failed: {exception}", Qgis.MessageLevel.Critical)

		task = QgsTask.fromFunction(description, work, on_finished=finished)
		self.tasks.add(task)
		QgsApplication.taskManager().addTask(task)
		return task

	def erp(self, fn):
		"""Run fn(client); if the ERP session has expired, ask the user to
		log in again and retry once."""
		client = self.ensure_client()
		if client is None:
			raise SessionExpired("Not logged in to ERP.")
		try:
			with BusyCursor():
				return fn(client)
		except SessionExpired:
			if not self.login("Your ERP session has expired - please log in again."):
				raise
			with BusyCursor():
				return fn(self.client)

	def _active_erp_layer(self):
		layer = self.iface.activeLayer()
		if not L.is_erp_layer(layer):
			self.msg("Select an ERP layer (loaded from Upande Spatial) in the Layers panel first.", Qgis.MessageLevel.Warning)
			return None
		return layer

	# ── dock ───────────────────────────────────────────────────
	def toggle_dock(self):
		if self.dock is None:
			self._build_dock()
		elif self.dock.isVisible():
			self.dock.hide()
			return
		self.dock.show()
		self.refresh_catalog()

	def _build_dock(self):
		self.dock = QDockWidget("Upande Spatial", self.iface.mainWindow())
		self.dock.setObjectName("UpandeSpatialDock")
		w = QWidget()
		v = QVBoxLayout(w)
		self.conn_label = QLabel("Not logged in")
		self.conn_label.setWordWrap(True)
		self.conn_label.linkActivated.connect(lambda *_: self.logout())
		v.addWidget(self.conn_label)

		row = QHBoxLayout()
		row.addWidget(QLabel("Farm"))
		self.farm_combo = QComboBox()
		self.farm_combo.addItem("All farms", "")
		self.farm_combo.currentIndexChanged.connect(lambda *_: self.refresh_catalog(keep_farms=True))
		row.addWidget(self.farm_combo, 1)
		v.addLayout(row)

		self.tree = QTreeWidget()
		self.tree.setHeaderLabels(["Layer", "Features"])
		self.tree.setColumnWidth(0, 210)
		self.tree.itemDoubleClicked.connect(lambda item, _col: self.load_item(item))
		v.addWidget(self.tree, 1)

		buttons = QHBoxLayout()
		for text, slot in (("Load", self.load_selected), ("Refresh", self.refresh_catalog), ("Publish…", self.publish), ("Log in", self.login)):
			b = QPushButton(text)
			b.clicked.connect(lambda *_, s=slot: s())
			buttons.addWidget(b)
		v.addLayout(buttons)
		hint = QLabel("Double-click a layer to load it. Saving edits on an ERP layer sends them to ERP straight away.")
		hint.setWordWrap(True)
		hint.setStyleSheet("color: gray")
		v.addWidget(hint)

		self.dock.setWidget(w)
		self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)

	def refresh_catalog(self, keep_farms=False):
		farm = self.farm_combo.currentData() if self.dock else ""
		try:
			info = self.erp(lambda c: c.ping())
			self.catalog = self.erp(lambda c: c.catalog(farm=farm or None))
		except Exception as e:
			self.conn_label.setText(f"<span style='color:#b3261e'>{e}</span>")
			return
		self.conn_label.setText(f"<b>{info['site']}</b> · {info['user']}" + ("" if info.get("can_write") else " · read-only")
			+ " · <a href='logout'>log out</a>")
		if not keep_farms:
			self.farm_combo.blockSignals(True)
			current = self.farm_combo.currentData()
			self.farm_combo.clear()
			self.farm_combo.addItem("All farms", "")
			for f in self.catalog.get("farms") or []:
				self.farm_combo.addItem(f, f)
			i = self.farm_combo.findData(current)
			self.farm_combo.setCurrentIndex(max(i, 0))
			self.farm_combo.blockSignals(False)

		self.tree.clear()
		data_root = QTreeWidgetItem(self.tree, ["Spatial layers", ""])
		by_layer = {}
		for entry in self.catalog.get("layers") or []:
			parent = by_layer.get(entry["layer"])
			if parent is None:
				parent = by_layer[entry["layer"]] = QTreeWidgetItem(data_root, [entry["layer"], ""])
			item = QTreeWidgetItem(parent, [entry["geometry_type"] or "?", str(entry["count"])])
			item.setData(0, ROLE_KIND, "layer")
			item.setData(0, ROLE_DATA, entry)
			roles = ", ".join(entry.get("roles") or [])
			item.setToolTip(0, f"{entry['layer']} · {entry['geometry_type']}" + (f"\nRoles: {roles}" if roles else ""))
		for parent in by_layer.values():
			parent.setText(1, str(sum(int(parent.child(i).text(1)) for i in range(parent.childCount()))))
		nets = self.catalog.get("epanet_networks") or []
		if nets:
			net_root = QTreeWidgetItem(self.tree, ["EPANET results", ""])
			for n in nets:
				item = QTreeWidgetItem(net_root, [n["network_name"], "results"])
				item.setData(0, ROLE_KIND, "epanet")
				item.setData(0, ROLE_DATA, n)
				inp = QTreeWidgetItem(net_root, [f"{n['network_name']} (.inp)", "model"])
				inp.setData(0, ROLE_KIND, "inp")
				inp.setData(0, ROLE_DATA, n)
		self.tree.expandAll()

	def load_selected(self):
		for item in self.tree.selectedItems():
			self.load_item(item)

	def load_item(self, item):
		kind = item.data(0, ROLE_KIND)
		data = item.data(0, ROLE_DATA)
		if kind == "layer":
			self.load_layer(data["layer"], data["geometry_type"], data)
		elif kind == "epanet":
			self.load_epanet_results(data)
		elif kind == "inp":
			self.download_inp(data)

	# ── loading ────────────────────────────────────────────────
	def load_layer(self, layer_name, geometry_type, entry=None):
		farm = self.farm_combo.currentData() if self.dock else ""
		title = layer_name if not farm else f"{layer_name} — {farm}"
		if geometry_type and geometry_type not in ("Point", "LineString", "Polygon"):
			title += f" ({geometry_type})"

		def done(fc):
			vl = L.build_layer(fc, layer_name, geometry_type, self.client.url, farm=farm, entry=entry, title=title)
			QgsProject.instance().addMapLayer(vl)
			self.attach(vl)
			n = vl.featureCount()
			self.msg(f"Loaded {n} features from {layer_name}." if n else
				f"{layer_name} is empty - toggle editing to draw its first features.")

		self.erp_async(f"Loading {title} from ERP", lambda c: c.get_layer(layer_name, geometry_type, farm=farm or None), done)

	def attach(self, vl):
		if vl.id() in self.syncs:
			return self.syncs[vl.id()]
		sync = L.LayerSync(vl, self.client)
		sync.pushed.connect(self._report_push)
		self.syncs[vl.id()] = sync
		return sync

	def _forget_layer(self, layer_id):
		self.syncs.pop(layer_id, None)

	def _reattach_project_layers(self, *_):
		"""Memory layers come back empty when a project is reopened - refill
		every ERP layer from ERP and switch live sync back on."""
		erp_layers = [l for l in QgsProject.instance().mapLayers().values() if L.is_erp_layer(l)]
		if not erp_layers or not self.ensure_client("This project has ERP layers - log in to load them."):
			return
		for vl in erp_layers:
			try:
				fc = self.erp(lambda c, vl=vl: c.get_layer(vl.customProperty(L.PROP + "layer"), vl.customProperty(L.PROP + "geometry_type") or None,
					farm=vl.customProperty(L.PROP + "farm") or None))
				L.fill_layer(vl, fc)
				self.attach(vl)
			except Exception as e:
				self.msg(f"Could not reload {vl.name()}: {e}", Qgis.MessageLevel.Warning)

	def reload_active(self):
		vl = self._active_erp_layer()
		if not vl:
			return
		if vl.isEditable():
			self.msg("Save or discard your edits on this layer first.", Qgis.MessageLevel.Warning)
			return
		try:
			fc = self.erp(lambda c: c.get_layer(vl.customProperty(L.PROP + "layer"), vl.customProperty(L.PROP + "geometry_type") or None,
				farm=vl.customProperty(L.PROP + "farm") or None))
			n = L.fill_layer(vl, fc)
		except Exception as e:
			self.msg(f"Reload failed: {e}", Qgis.MessageLevel.Critical)
			return
		self.attach(vl)
		self.msg(f"Reloaded {n} features.")

	def load_epanet_results(self, net):
		self.erp_async(f"Loading EPANET results for {net['network_name']}", lambda c: c.epanet_results(net["name"]),
			lambda res: self._add_epanet_layers(net, res))

	def _add_epanet_layers(self, net, res):
		suffix = f" · {res['run']}" + (f" · {res['statistic'].lower()}" if res.get("statistic") not in (None, "NONE") else "")
		group = QgsProject.instance().layerTreeRoot().insertGroup(0, f"EPANET {net['network_name']}{suffix}")
		for key, gtype, field in (("links", "LineString", "flow_m3s"), ("nodes", "Point", "pressure_m")):
			vl = L.build_results_layer(res[key], f"{net['network_name']} {key}", gtype, self.client.url)
			self._graduate(vl, field)
			QgsProject.instance().addMapLayer(vl, False)
			group.addLayer(vl)
		self.msg(f"Loaded EPANET results {res['run']} (flows in m³/s, pressures in m).")

	def _graduate(self, vl, field):
		"""Colour results by pressure/flow - best effort, plain style if the
		renderer API differs in this QGIS version."""
		try:
			from qgis.core import QgsClassificationJenks, QgsGraduatedSymbolRenderer, QgsStyle

			renderer = QgsGraduatedSymbolRenderer(f'abs("{field}")')
			renderer.setClassificationMethod(QgsClassificationJenks())
			renderer.updateClasses(vl, 5)
			ramp = QgsStyle().defaultStyle().colorRamp("Spectral")
			if ramp:
				ramp.invert()
				renderer.updateColorRamp(ramp)
			vl.setRenderer(renderer)
		except Exception:
			pass

	def download_inp(self, net):
		try:
			data = self.erp(lambda c: c.export_inp(net["name"]))
		except Exception as e:
			self.msg(f"Export failed: {e}", Qgis.MessageLevel.Critical)
			return
		path = os.path.join(tempfile.gettempdir(), f"{net['network_name'].replace(' ', '_')}.inp")
		with open(path, "wb") as fh:
			fh.write(data)
		self.msg(f"Saved EPANET model to {path} (open it with the QGIS EPANET/QWater plugins or EPANET).", duration=12)

	# ── writing ────────────────────────────────────────────────
	def _report_push(self, layer, s):
		parts = [f"{n} {k}" for k, n in (("added", s["added"]), ("updated", s["updated"]), ("deleted", s["deleted"])) if n]
		if parts:
			self.msg(f"{layer.name()}: " + ", ".join(parts) + " in ERP.", Qgis.MessageLevel.Success, 4)
		if s.get("held"):
			self.msg(f"{layer.name()}: ERP didn't receive this save ({s['errors'][0]['error']}). Your edits are kept and will be "
				"sent when you log in again (Upande Spatial → Log in).", Qgis.MessageLevel.Warning, 0)
			if self.client is None or not self.client.logged_in or "log in" in s["errors"][0]["error"].lower() or "session" in s["errors"][0]["error"].lower():
				self.login("Your ERP session has expired - log in to send your saved edits.")
			return
		problems = [f"{c.get('name')}: {c.get('error')}" for c in s["conflicts"]]
		problems += [f"{e.get('name') or ('new feature ' + str(e.get('client_id', '')))}: {e.get('error')}" for e in s["errors"]]
		if problems:
			hint = " Reload the layer to see ERP's version, or select them and Force-push." if s["conflicts"] else ""
			QMessageBox.warning(
				self.iface.mainWindow(), f"{layer.name()} - not saved to ERP",
				"These changes were kept in QGIS but not saved to ERP:\n\n" + "\n".join(problems[:30])
				+ ("\n…" if len(problems) > 30 else "") + "\n" + hint,
			)

	def push_unsynced(self):
		vl = self._active_erp_layer()
		if vl and self.ensure_client():
			sync = self.attach(vl)
			with BusyCursor():
				sync.retry_pending()
				res = sync.push_unsynced()
			if res is None:
				self.msg("Every feature in this layer is already in ERP.")

	def force_push_selected(self):
		vl = self._active_erp_layer()
		if not vl or not self.ensure_client():
			return
		fids = list(vl.selectedFeatureIds())
		if not fids:
			self.msg("Select the features to push first.", Qgis.MessageLevel.Warning)
			return
		if QMessageBox.question(self.iface.mainWindow(), "Force-push",
				f"Overwrite {len(fids)} feature(s) in ERP with the QGIS version, even if someone changed them since?") != QMessageBox.StandardButton.Yes:
			return
		with BusyCursor():
			self.attach(vl).force_push(fids)

	def checkout_selected(self, checkout):
		vl = self._active_erp_layer()
		if not vl or not self.ensure_client():
			return
		feats = [f for f in vl.getSelectedFeatures() if f["_name"]]
		if not feats:
			self.msg("Select features that are already in ERP first.", Qgis.MessageLevel.Warning)
			return
		idx = vl.fields().indexOf("_checked_out_by")
		writes, errors = {}, []
		with BusyCursor():
			for f in feats:
				try:
					r = self.erp(lambda c, n=f["_name"]: (c.checkout if checkout else c.release)(n))
					writes[f.id()] = {idx: r.get("checked_out_by")}
				except Exception as e:
					errors.append(f"{f['_name']}: {e}")
		if writes:
			vl.dataProvider().changeAttributeValues(writes)
		verb = "Checked out" if checkout else "Released"
		self.msg(f"{verb} {len(writes)} feature(s)." + (f" {len(errors)} failed: " + "; ".join(errors[:3]) if errors else ""),
			Qgis.MessageLevel.Success if not errors else Qgis.MessageLevel.Warning)

	def publish(self):
		if not self.ensure_client():
			return
		if not self.catalog:
			try:
				self.catalog = self.erp(lambda c: c.catalog())
			except Exception as e:
				self.msg(str(e), Qgis.MessageLevel.Critical)
				return
		dlg = PublishDialog(self.client, self.catalog, self.iface, self.iface.mainWindow())
		if not dlg.exec():
			return
		source = dlg.layer.currentLayer()
		if source is None:
			return
		features = list(source.getSelectedFeatures()) if dlg.selected_only.isChecked() else None
		try:
			res = self.erp(lambda c: L.publish_features(c, source, dlg.target(), features))
		except Exception as e:
			self.msg(f"Publish failed: {e}", Qgis.MessageLevel.Critical)
			return
		if res["errors"]:
			QMessageBox.warning(self.iface.mainWindow(), "Publish", f"{res['created']} published, {len(res['errors'])} refused:\n\n"
				+ "\n".join(f"#{e.get('client_id')}: {e.get('error')}" for e in res["errors"][:30]))
		else:
			self.msg(f"Published {res['created']} features to ERP.", Qgis.MessageLevel.Success)
		if self.dock:
			self.refresh_catalog(keep_farms=True)
