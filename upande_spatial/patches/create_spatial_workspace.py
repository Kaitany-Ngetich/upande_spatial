import frappe


def execute():
	frappe.set_user("Administrator")
	if frappe.db.exists("Workspace", "Upande Spatial"):
		print("already exists")
		return
	doc = frappe.get_doc({
		"doctype": "Workspace",
		"name": "Upande Spatial",
		"label": "Upande Spatial",
		"title": "Upande Spatial",
		"module": "Upande Spatial",
		"public": 1,
		"is_hidden": 0,
		"for_user": "",
		"parent_page": "",
		"content": frappe.as_json([
			{"id": "header", "type": "header", "data": {"text": "<span class=\"h4\"><b>Upande Spatial</b></span>", "col": 12}},
			{"id": "para", "type": "paragraph", "data": {"text": "Shared spatial data module — one place for geometry (points/lines/polygons) referenced by other doctypes (Farm, Warehouse, Guard Device Token, ...) rather than each module storing its own.", "col": 12}},
			{"id": "shortcuts_header", "type": "header", "data": {"text": "Shortcuts", "col": 12}},
			{"id": "shortcuts", "type": "shortcut", "data": {"shortcut_name": "Spatial Feature", "col": 3}},
			{"id": "shortcuts2", "type": "shortcut", "data": {"shortcut_name": "Spatial Entity Config", "col": 3}},
		]),
		"shortcuts": [
			{"type": "DocType", "link_to": "Spatial Feature", "label": "Spatial Feature", "doc_view": "List"},
			{"type": "DocType", "link_to": "Spatial Entity Config", "label": "Spatial Entity Config", "doc_view": "List"},
		],
		"links": [],
		"roles": [],
		"sequence_id": 1.0,
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	print("created Workspace:", doc.name)
