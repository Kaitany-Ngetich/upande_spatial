# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from upande_spatial.api.inspection import validate_fail_rule


class FieldInspectionTemplate(Document):
	def validate(self):
		for q in self.questions:
			if q.answer_type == "Select" and not (q.options or "").strip():
				frappe.throw(_("Row {0}: a Select question needs its choices in Options.").format(q.idx))
			validate_fail_rule(q.fail_when)
