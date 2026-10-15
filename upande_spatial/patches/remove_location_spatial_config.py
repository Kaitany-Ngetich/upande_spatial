"""Drop the Location Spatial Entity Config: its "Site" / "Site Boundary"
layers were never used (ERPNext Location records aren't drawn on the map)
and only showed up as empty, unexplained layers in the Spatial Data
Catalog, the Map Viewer and QGIS. Kept if any Location feature exists."""

import frappe


def execute():
	if not frappe.db.exists("Spatial Entity Config", "Location"):
		return
	if frappe.db.exists("Spatial Feature", {"reference_doctype": "Location"}):
		return
	frappe.delete_doc("Spatial Entity Config", "Location", ignore_permissions=True, force=True)
