import frappe


def execute():
	if frappe.db.exists("Spatial Entity Config", "Tank And Valve"):
		print("already exists")
		return

	doc = frappe.new_doc("Spatial Entity Config")
	doc.reference_doctype = "Tank And Valve"
	doc.description = (
		"Irrigation infrastructure (tanks/valves) - Tank And Valve already carries "
		"its own location_geojson field, this config just registers it in the "
		"central catalog so it's discoverable alongside every other spatial asset."
	)
	doc.append("allowed_geometries", {"geometry_type": "Point", "feature_role": "Location"})
	doc.insert()
	frappe.db.commit()
	print("created Spatial Entity Config: Tank And Valve")
