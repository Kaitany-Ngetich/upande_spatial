// Copyright (c) 2026, dev@upande.com and contributors
// For license information, please see license.txt

frappe.query_reports["EPANET Node Results"] = {
	filters: [
		{
			fieldname: "network",
			label: __("Network"),
			fieldtype: "Link",
			options: "EPANET Network",
			reqd: 1,
			on_change: () => {
				frappe.query_report.set_filter_value("run", "");
			},
		},
		{
			fieldname: "run",
			label: __("Run"),
			fieldtype: "Link",
			options: "EPANET Simulation Run",
			description: __("Blank = latest successful run"),
			get_query: () => ({
				filters: {
					network: frappe.query_report.get_filter_value("network"),
					status: "Success",
				},
			}),
		},
		{
			fieldname: "element_type",
			label: __("Element Type"),
			fieldtype: "Select",
			options: ["", "Junction", "Tank", "Reservoir"].join("\n"),
		},
		{
			fieldname: "at_hour",
			label: __("At Hour"),
			fieldtype: "Float",
			description: __("Hours after the start. Blank = last timestep"),
		},
		{
			fieldname: "min_pressure",
			label: __("Min Acceptable Pressure (m)"),
			fieldtype: "Float",
			default: 10,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "min_pressure" && data && data.below_min) {
			value = `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
		}
		return value;
	},
};
