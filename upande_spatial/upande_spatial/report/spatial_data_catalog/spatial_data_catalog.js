// Copyright (c) 2026, dev@upande.com and contributors
// For license information, please see license.txt

frappe.query_reports["Spatial Data Catalog"] = {
	filters: [
		{
			fieldname: "farm",
			label: __("Farm"),
			fieldtype: "Link",
			options: "Farm",
			width: "120",
		},
	],
};
