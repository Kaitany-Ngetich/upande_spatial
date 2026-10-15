"""Row-level access for field inspections: a Field Inspector sees only the
runs assigned to them and those runs' visits. Managers see everything."""

import frappe

from upande_spatial.api.inspection import is_manager


def _restricted(user):
	user = user or frappe.session.user
	return user != "Administrator" and not is_manager(user)


def run_query(user=None):
	user = user or frappe.session.user
	if not _restricted(user):
		return ""
	return f"`tabField Inspection Run`.`inspector` = {frappe.db.escape(user)}"


def visit_query(user=None):
	user = user or frappe.session.user
	if not _restricted(user):
		return ""
	return f"`tabField Inspection Visit`.`inspector` = {frappe.db.escape(user)}"


def has_run_permission(doc, ptype=None, user=None):
	user = user or frappe.session.user
	return not _restricted(user) or doc.inspector == user


def has_visit_permission(doc, ptype=None, user=None):
	user = user or frappe.session.user
	return not _restricted(user) or doc.inspector == user
