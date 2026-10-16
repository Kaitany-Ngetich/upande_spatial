# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from upande_spatial.api.sensors import MODEL_UNITS, QUANTITY_ROLES


class NetworkSensorLink(Document):
	def validate(self):
		self.model_unit = MODEL_UNITS.get(self.quantity, "")
		self.factor = self.factor if self.factor not in (None, 0) else 1
		role = frappe.db.get_value("Spatial Feature", self.element, "feature_role") if self.element else None
		allowed = QUANTITY_ROLES.get(self.quantity)
		if role and allowed and role not in allowed:
			frappe.msgprint(
				_("{0} is usually measured on a {1}, but {2} is a {3}.").format(
					self.quantity, " / ".join(allowed), self.element, role),
				indicator="orange", alert=True)
