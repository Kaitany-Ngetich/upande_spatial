import frappe
import json


def execute():
	name = frappe.db.get_value("Farm Block", {}, "name")
	if not name:
		fb = frappe.new_doc("Farm Block")
		fb.block_name = "Test Block A"
		fb.acreage = 12.5
		fb.insert()
		name = fb.name
		frappe.db.commit()

	from upande_spatial.api.spatial import upsert_feature, list_features

	geometry = {
		"type": "Polygon",
		"coordinates": [[[36.80, -1.28], [36.81, -1.28], [36.81, -1.29], [36.80, -1.29], [36.80, -1.28]]],
	}
	result = upsert_feature(
		geometry=json.dumps(geometry),
		reference_doctype="Farm Block",
		reference_name=name,
		feature_role="Boundary",
		title="Block boundary for " + name,
	)
	print("upserted:", result)
	print("list:", list_features(reference_doctype="Farm Block"))

	# confirm a disallowed geometry/role is rejected
	try:
		upsert_feature(
			geometry=json.dumps({"type": "Point", "coordinates": [36.80, -1.28]}),
			reference_doctype="Farm Block",
			reference_name=name,
			feature_role="Boundary",
		)
		print("ERROR: should have been rejected")
	except frappe.ValidationError as e:
		print("correctly rejected:", str(e))
