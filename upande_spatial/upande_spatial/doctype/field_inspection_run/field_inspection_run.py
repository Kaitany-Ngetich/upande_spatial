# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FieldInspectionRun(Document):
	def on_trash(self):
		for name in frappe.get_all("Field Inspection Visit", pluck="name", filters={"run": self.name}):
			frappe.delete_doc("Field Inspection Visit", name, ignore_permissions=True, force=True)
		if self.track and frappe.db.exists("Spatial Feature", self.track):
			frappe.delete_doc("Spatial Feature", self.track, ignore_permissions=True, force=True)
