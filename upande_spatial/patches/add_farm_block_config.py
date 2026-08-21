import frappe


def execute():
	if frappe.db.exists("Spatial Entity Config", "Farm Block"):
		print("already exists")
		return

	doc = frappe.new_doc("Spatial Entity Config")
	doc.reference_doctype = "Farm Block"
	doc.description = (
		"Farm Block boundary - Farm Block itself carries no geometry field, "
		"the boundary is stored externally as a Spatial Feature keyed to it."
	)
	doc.append("allowed_geometries", {"geometry_type": "Polygon", "feature_role": "Boundary"})
	doc.insert()
	frappe.db.commit()
	print("created Spatial Entity Config: Farm Block")
