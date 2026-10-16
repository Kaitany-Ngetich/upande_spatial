// Copyright (c) 2026, dev@upande.com and contributors
// For license information, please see license.txt

frappe.ui.form.on("Network Sensor Link", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Recent readings"), () => show_readings(frm));
		frm.add_custom_button(__("Refresh latest"), () =>
			frappe.call("upande_spatial.api.sensors.refresh", { link: frm.doc.name }).then(() => frm.reload_doc())
		);
	},
	quantity(frm) {
		const units = { Pressure: "m", "Flow rate": "L/s", Volume: "m3", Level: "m", Head: "m", Other: "" };
		frm.set_value("model_unit", units[frm.doc.quantity] || "");
	},
});

function show_readings(frm) {
	frappe.call("upande_spatial.api.sensors.get_readings", { link: frm.doc.name, hours: 24 }).then(({ message: r }) => {
		const rows = (r.readings || []).slice(-50).reverse();
		const body = rows.length
			? `<table class="table table-sm table-bordered" style="font-size:var(--text-sm)">
				<thead><tr><th>${__("Time")}</th><th style="text-align:right">${__("Value")} (${frappe.utils.escape_html(r.unit || "")})</th>
				<th style="text-align:right">${__("Raw")}</th><th>${__("Battery")}</th><th>${__("RSSI")}</th></tr></thead>
				<tbody>${rows.map((x) => `<tr><td>${frappe.datetime.str_to_user(x.timestamp)}</td>
					<td style="text-align:right">${x.value == null ? "–" : Number(x.value).toFixed(2)}</td>
					<td style="text-align:right">${x.raw ?? "–"} ${frappe.utils.escape_html(x.raw_unit || "")}</td>
					<td>${x.battery ?? "–"}</td><td>${x.rssi ?? "–"}</td></tr>`).join("")}</tbody></table>`
			: `<p class="text-muted">${frappe.utils.escape_html(r.message || __("No readings."))}</p>`;
		new frappe.ui.Dialog({ title: __("Last 24 h - {0}", [frm.doc.sensor_label]), size: "large",
			fields: [{ fieldtype: "HTML", options: body }] }).show();
	});
}
