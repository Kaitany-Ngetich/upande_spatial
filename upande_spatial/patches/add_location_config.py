import frappe
import json


def execute():
	if not frappe.db.exists("Spatial Entity Config", "Location"):
		doc = frappe.new_doc("Spatial Entity Config")
		doc.reference_doctype = "Location"
		doc.description = (
			"Core ERPNext Location (Assets module) - tree-structured land/plot "
			"records. Location already carries its own Geolocation field; this "
			"config registers it in the central catalog too, same pattern as "
			"Farm and Tank And Valve, so it's queryable alongside every other "
			"spatial asset rather than only reachable through its own doctype."
		)
		doc.append("allowed_geometries", {"geometry_type": "Point", "feature_role": "Location"})
		doc.append("allowed_geometries", {"geometry_type": "Polygon", "feature_role": "Boundary"})
		doc.insert()
		frappe.db.commit()
		print("created Spatial Entity Config: Location")
	else:
		print("already exists")

	# end-to-end verification with a real Location record
	from upande_spatial.api.spatial import upsert_feature, list_features

	name = frappe.db.get_value("Location", {}, "name")
	if not name:
		loc = frappe.new_doc("Location")
		loc.location_name = "Test Plot A"
		loc.insert()
		name = loc.name
		frappe.db.commit()

	geometry = {
		"type": "Polygon",
		"coordinates": [[[36.70, -1.20], [36.71, -1.20], [36.71, -1.21], [36.70, -1.21], [36.70, -1.20]]],
	}
	result = upsert_feature(
		geometry=json.dumps(geometry),
		reference_doctype="Location",
		reference_name=name,
		feature_role="Boundary",
		title="Boundary for " + name,
	)
	print("upserted:", result)
	print("list:", list_features(reference_doctype="Location"))
