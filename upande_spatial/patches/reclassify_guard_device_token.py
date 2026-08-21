import frappe

def execute():
	out = []
	# Guard Device Token behaves like Patrol GPS Log: a live/overwritten
	# device-location ping, not a durable reference geometry - excluded
	# from the centralized GIS store the same way.
	if frappe.db.exists("Spatial Entity Config", "Guard Device Token"):
		frappe.delete_doc("Spatial Entity Config", "Guard Device Token", force=True)
		out.append("deleted Spatial Entity Config: Guard Device Token")

	# clean up the test Spatial Feature created against it during
	# verification - no longer in scope
	existing = frappe.db.get_value(
		"Spatial Feature",
		{"reference_doctype": "Guard Device Token"},
		"name",
	)
	if existing:
		frappe.delete_doc("Spatial Feature", existing, force=True)
		out.append("deleted test Spatial Feature: " + existing)

	frappe.db.commit()
	print("\n".join(out) if out else "nothing to clean up")
