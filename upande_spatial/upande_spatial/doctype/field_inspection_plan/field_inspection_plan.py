# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from upande_spatial.api.inspection import WAYPOINT_ROLE, _feature_point


class FieldInspectionPlan(Document):
	def validate(self):
		used = [cint(w.sequence) for w in self.waypoints if cint(w.sequence)]
		nxt = max(used or [0])
		for w in self.waypoints:
			if not cint(w.sequence):
				nxt += 1
				w.sequence = nxt
			if not (w.latitude or w.longitude):
				pos = _feature_point(w.point) or _feature_point(w.asset)
				if pos:
					w.latitude, w.longitude = pos
			if not (w.latitude or w.longitude):
				frappe.throw(_("Waypoint {0}: give its latitude/longitude (or a point asset).").format(w.sequence))
		if self.frequency != "One-off" and not self.next_run_date:
			self.next_run_date = self.start_date

	def on_update(self):
		"""Each waypoint's position as a Spatial Feature (role Inspection
		Waypoint, owned by this plan) - so the route shows in the Map Viewer
		and QGIS - kept in step with the table."""
		from upande_spatial.api.spatial import upsert_feature

		keep = set()
		for w in self.waypoints:
			res = upsert_feature(
				geometry={"type": "Point", "coordinates": [w.longitude, w.latitude]},
				name=w.point if w.point and frappe.db.exists("Spatial Feature", w.point) else None,
				reference_doctype=self.doctype, reference_name=self.name, feature_role=WAYPOINT_ROLE,
				farm=self.farm, company=self.company, title=w.waypoint_title or f"Waypoint {w.sequence}",
				properties={"sequence": cint(w.sequence), "asset": w.asset, "template": w.template},
				source_module="Field Inspection",
			)
			keep.add(res["name"])
			if w.point != res["name"]:
				w.db_set("point", res["name"], update_modified=False)
		for orphan in frappe.get_all("Spatial Feature", pluck="name", filters={
				"reference_doctype": self.doctype, "reference_name": self.name, "feature_role": WAYPOINT_ROLE}):
			if orphan not in keep:
				frappe.delete_doc("Spatial Feature", orphan, ignore_permissions=True)

	def on_trash(self):
		for name in frappe.get_all("Spatial Feature", pluck="name",
				filters={"reference_doctype": self.doctype, "reference_name": self.name}):
			frappe.delete_doc("Spatial Feature", name, ignore_permissions=True, force=True)
