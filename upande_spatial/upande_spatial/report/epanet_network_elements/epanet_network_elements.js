// Copyright (c) 2026, dev@upande.com and contributors
// For license information, please see license.txt

frappe.query_reports["EPANET Network Elements"] = {
	filters: [
		{
			fieldname: "network",
			label: __("Network"),
			fieldtype: "Link",
			options: "EPANET Network",
			reqd: 1,
		},
		{
			fieldname: "element_type",
			label: __("Element Type"),
			fieldtype: "Select",
			options: ["", "Junction", "Reservoir", "Tank", "Pipe", "Pump", "Valve"].join("\n"),
		},
	],
};
