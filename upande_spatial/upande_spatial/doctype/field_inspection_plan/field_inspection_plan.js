// Copyright (c) 2026, dev@upande.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Field Inspection Plan", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Create Run"), () => {
			frappe.prompt(
				[
					{ fieldname: "inspector", fieldtype: "Link", options: "User", label: __("Inspector"), reqd: 1, default: frm.doc.default_inspector },
					{ fieldname: "scheduled_date", fieldtype: "Date", label: __("Date"), reqd: 1, default: frappe.datetime.get_today() },
				],
				(v) => frappe.call({
					method: "upande_spatial.api.inspection.create_run",
					args: { plan: frm.doc.name, inspector: v.inspector, scheduled_date: v.scheduled_date },
					callback: (r) => r.message && frappe.set_route("Form", "Field Inspection Run", r.message),
				}),
				__("New inspection run"),
				__("Create"),
			);
		});
		frm.add_custom_button(__("Runs"), () => frappe.set_route("List", "Field Inspection Run", { plan: frm.doc.name }));
	},
});
