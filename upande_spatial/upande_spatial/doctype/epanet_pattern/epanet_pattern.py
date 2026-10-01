# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EPANETPattern(Document):
	"""A time pattern (demand, reservoir head, pump speed or energy price) -
	one multiplier per Pattern Timestep of the network it's used on,
	repeating once it runs out. Elements reference it by name from their
	properties JSON (demand_pattern, head_pattern, speed_pattern,
	energy_pattern); see api/epanet.py."""

	def validate(self):
		if not self.multipliers:
			frappe.throw(_("A pattern needs at least one multiplier."))
		for row in self.multipliers:
			if row.multiplier is None or row.multiplier < 0:
				frappe.throw(_("Row {0}: multipliers can't be negative.").format(row.idx))
