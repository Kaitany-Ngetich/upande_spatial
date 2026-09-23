# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

"""Map Viewer's plugin loader - the Desk-managed "Map Viewer Plugin" list
is what makes plugins genuinely installable (enable/disable/reorder from
Desk) rather than hardcoded <script> tags baked into the page.

Deliberately uses frappe.get_all (no permission check) rather than
frappe.get_list: any visitor who can load Map Viewer at all should see the
same plugin toolbar, same as get_layers()/get_referenced_doctypes() elsewhere
in this app - Map Viewer Plugin's own Desk permissions (System Manager only)
still gate who can add/edit/disable plugins, just not who can see the
resulting toolbar."""

import frappe


@frappe.whitelist()
def get_enabled_plugins():
	return frappe.get_all(
		"Map Viewer Plugin",
		filters={"enabled": 1},
		fields=["name", "title", "icon", "script_url", "description"],
		order_by="sort_order asc, title asc",
	)
