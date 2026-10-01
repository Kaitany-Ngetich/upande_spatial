# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EPANETCurve(Document):
	"""An X-Y curve: pump head, pump efficiency, tank volume or GPV headloss.
	Units are the ones the Curve Type description lists (flow in L/s, so
	they read the same as everywhere else in this app) - api/epanet.py
	converts to SI when building the model. Elements reference it by name
	from their properties JSON (pump_curve, efficiency_curve, vol_curve,
	headloss_curve)."""

	def validate(self):
		if not self.points:
			frappe.throw(_("A curve needs at least one point."))
		self.points.sort(key=lambda p: p.x)
		for i, row in enumerate(self.points, start=1):
			row.idx = i
		xs = [p.x for p in self.points]
		if len(set(xs)) != len(xs):
			frappe.throw(_("Curve X values must all be different."))
		if self.curve_type == "Pump":
			if len(self.points) == 2:
				# EPANET only accepts 1-point (design point), 3-point or
				# multi-point (>3) pump curves - 2 points has no defined shape.
				frappe.throw(_("A pump curve needs 1 point (design flow/head), 3 points, or more than 3 - not 2."))
			ys = [p.y for p in self.points]
			if any(b > a for a, b in zip(ys, ys[1:])):
				frappe.throw(_("A pump curve's head must fall (or stay level) as flow rises."))
