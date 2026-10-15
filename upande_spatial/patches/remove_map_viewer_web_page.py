"""Map Viewer moved from a "map-viewer" Web Page record into the app
(www/map-viewer.html). Remove the old record so the code page serves
/map-viewer and the viewer's source no longer shows under Website > Web Page."""

import frappe


def execute():
	if frappe.db.exists("Web Page", "map-viewer"):
		frappe.delete_doc("Web Page", "map-viewer", force=True, ignore_permissions=True)
