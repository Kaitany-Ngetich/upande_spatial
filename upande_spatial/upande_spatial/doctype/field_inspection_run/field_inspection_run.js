// Copyright (c) 2026, dev@upande.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Field Inspection Run", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Visits"), () => frappe.set_route("List", "Field Inspection Visit", { run: frm.doc.name }));
		if (frm.doc.track) {
			frm.add_custom_button(__("Track"), () => frappe.set_route("Form", "Spatial Feature", frm.doc.track));
		}
	},
});
