// Copyright (c) 2026, dev@upande.com and contributors
// For license information, please see license.txt

// The run's Results (JSON) is the full simulation output (~200 KB+). The
// Overview turns it into what people actually look at: pressure range over
// the whole day, peak velocity, pressure bands, and the junctions/pipes to
// worry about - using the hourly time series, not just the final hour
// (which for an irrigation schedule is usually "everything switched off").

const PRESSURE_BANDS = [
	{ label: "< 10 m (low)", max: 10, color: "var(--red-500)" },
	{ label: "10–20 m", max: 20, color: "var(--orange-500)" },
	{ label: "20–40 m (good)", max: 40, color: "var(--green-500)" },
	{ label: "40–60 m (high)", max: 60, color: "var(--blue-500)" },
	{ label: "> 60 m (very high)", max: Infinity, color: "var(--purple-500)" },
];

frappe.ui.form.on("EPANET Simulation Run", {
	refresh(frm) {
		render_overview(frm);
		if (frm.is_new()) return;
		const filters = { network: frm.doc.network, run: frm.doc.name };
		frm.add_custom_button(__("Node Results"), () => frappe.set_route("query-report", "EPANET Node Results", filters), __("View"));
		frm.add_custom_button(__("Link Results"), () => frappe.set_route("query-report", "EPANET Link Results", filters), __("View"));
		frm.add_custom_button(__("Map Viewer"), () => window.open("/map-viewer", "_blank"), __("View"));
		if (frm.doc.status === "Success") {
			frm.add_custom_button(__("Plan inspection"), () => plan_inspection_dialog(frm)).addClass("btn-primary");
		}
	},
});

// ── Plan inspection from this run ──────────────────────────────────
const INSPECTION_THRESHOLDS = [
	["pipe_rating_m", __("Pipe pressure rating (m)")],
	["min_pressure_m", __("Minimum service pressure (m)")],
	["max_emitter_pressure_m", __("Max pressure at emitters (m), 0 = off")],
	["max_swing_m", __("Max daily pressure swing (m)")],
	["max_velocity", __("Max velocity (m/s)")],
	["min_velocity", __("Stagnant below (m/s)")],
	["max_headloss_m_km", __("Max headloss (m per km)")],
];
const SEVERITY_COLOR = { critical: "red", high: "orange", routine: "blue" };

function plan_inspection_dialog(frm) {
	frappe.call({
		method: "upande_spatial.api.epanet_inspection.preview",
		args: { run: frm.doc.name },
		freeze: true,
		freeze_message: __("Checking the results..."),
	}).then(({ message: first }) => {
		const ruleFields = Object.entries(first.rules).map(([key, r]) => ({
			fieldname: `rule_${key}`,
			fieldtype: "Check",
			label: `${r.label} <span class="indicator-pill ${SEVERITY_COLOR[r.severity]}" style="margin-left:4px">${r.severity}</span>`,
			default: first.enabled.includes(key) ? 1 : 0,
		}));
		const half = Math.ceil(ruleFields.length / 2);
		const d = new frappe.ui.Dialog({
			title: __("Plan inspection from {0}", [frm.doc.name]),
			size: "extra-large",
			fields: [
				{ fieldtype: "HTML", fieldname: "intro" },
				{ fieldtype: "Section Break", label: __("Rules") },
				...ruleFields.slice(0, half),
				{ fieldtype: "Column Break" },
				...ruleFields.slice(half),
				{ fieldtype: "Section Break", label: __("Thresholds (from the network - change for this plan only)"), collapsible: 1 },
				...INSPECTION_THRESHOLDS.slice(0, 4).map(([k, label]) => ({ fieldname: `t_${k}`, fieldtype: "Float", label, default: first.thresholds[k] })),
				{ fieldtype: "Column Break" },
				...INSPECTION_THRESHOLDS.slice(4).map(([k, label]) => ({ fieldname: `t_${k}`, fieldtype: "Float", label, default: first.thresholds[k] })),
				{ fieldtype: "Section Break", label: __("Flagged for inspection") },
				{ fieldtype: "HTML", fieldname: "preview_html" },
				{ fieldtype: "Section Break", label: __("Plan") },
				{ fieldname: "plan_name", fieldtype: "Data", label: __("Plan name"), default: `${frm.doc.network} - inspection from ${frm.doc.name}` },
				{ fieldname: "inspector", fieldtype: "Link", options: "User", label: __("Inspector"), default: frappe.session.user, reqd: 1 },
				{ fieldtype: "Column Break" },
				{ fieldname: "checkin_radius_m", fieldtype: "Float", label: __("Check-in radius (m)"), default: 15 },
				{ fieldname: "create_run", fieldtype: "Check", label: __("Also create a run (ready for the app / Mergin)"), default: 1 },
			],
			primary_action_label: __("Create plan"),
			primary_action(values) {
				frappe.call({
					method: "upande_spatial.api.epanet_inspection.create_plan",
					args: { run: frm.doc.name, ...args(values), plan_name: values.plan_name, inspector: values.inspector,
						checkin_radius_m: values.checkin_radius_m, create_run: values.create_run },
					freeze: true,
					freeze_message: __("Creating the inspection plan..."),
				}).then(({ message: r }) => {
					d.hide();
					frappe.show_alert({ message: __("Plan {0}: {1} waypoints", [r.plan, r.waypoints]), indicator: "green" });
					frappe.set_route("Form", r.run ? "Field Inspection Run" : "Field Inspection Plan", r.run || r.plan);
				});
			},
			secondary_action_label: __("Refresh preview"),
			secondary_action: () => refresh(),
		});
		d.fields_dict.intro.$wrapper.html(`<p class="text-muted">${__(
			"Junctions and pipes that break a rule become waypoints, ordered as a walk from the reservoir, each with the checklist for its most serious issue. The model is only as good as its inputs - these are things to verify in the field."
		)}</p>`);

		const args = (values) => ({
			rules: JSON.stringify(Object.keys(first.rules).filter((k) => values[`rule_${k}`])),
			thresholds: JSON.stringify(Object.fromEntries(INSPECTION_THRESHOLDS.map(([k]) => [k, values[`t_${k}`]]))),
		});
		const show = (p) => {
			const rows = p.elements;
			const counts = Object.entries(p.counts).map(([k, n]) => `${frappe.utils.escape_html(p.rules[k].label)}: <b>${n}</b>`).join(" · ");
			d.fields_dict.preview_html.$wrapper.html(
				rows.length
					? `<div style="margin-bottom:6px">${__("{0} waypoints", [`<b>${rows.length}</b>`])} - ${counts}</div>
					<div style="max-height:320px;overflow:auto"><table class="table table-sm table-bordered" style="font-size:var(--text-sm)">
						<thead><tr><th>${__("Severity")}</th><th>${__("Element")}</th><th>${__("Why")}</th><th>${__("Checklist")}</th></tr></thead>
						<tbody>${rows.map((e) => `<tr>
							<td><span class="indicator-pill ${SEVERITY_COLOR[e.severity]}">${e.severity}</span></td>
							<td>${frappe.utils.escape_html(e.title)} <span class="text-muted">· ${frappe.utils.escape_html(e.element)}</span></td>
							<td>${e.reasons.map((r) => frappe.utils.escape_html(r.detail + (r.hour ? ` at ${r.hour}` : ""))).join("<br>")}</td>
							<td>${frappe.utils.escape_html(e.template)}</td></tr>`).join("")}</tbody></table></div>`
					: `<div class="text-muted">${__("Nothing breaks the selected rules - no inspection needed.")}</div>`
			);
			d.get_primary_btn().prop("disabled", !rows.length);
		};
		const refresh = () =>
			frappe.call({ method: "upande_spatial.api.epanet_inspection.preview", args: { run: frm.doc.name, ...args(d.get_values(true)) } })
				.then(({ message }) => show(message));
		d.show();
		show(first);
		// Re-check whenever a rule is switched on/off.
		Object.keys(first.rules).forEach((k) => (d.fields_dict[`rule_${k}`].df.onchange = () => refresh()));
	});
}

function render_overview(frm) {
	const wrapper = frm.get_field("overview_html").$wrapper;
	let data;
	try {
		data = frm.doc.results ? JSON.parse(frm.doc.results) : null;
	} catch (e) {
		data = null;
	}
	if (!data) {
		wrapper.html(
			`<div class="text-muted" style="padding:8px 0">${
				frm.doc.status === "Failed" ? __("The simulation failed - see Messages below.") : __("No results stored for this run.")
			}</div>`
		);
		return;
	}

	const ts = data.timeseries || {};
	const types = data.element_types || {};
	const hours = (ts.times || []).map((t) => t / 3600);
	const hourLabel = (i) => (hours.length ? `${String(Math.floor(hours[i] ?? 0)).padStart(2, "0")}:00` : "");

	// Per-element extremes over all timesteps (fall back to final values).
	const span = (series, fallback, abs) => {
		const vals = (series || [fallback]).map((v) => (v == null ? null : abs ? Math.abs(v) : v));
		let lo = null, hi = null, iLo = 0, iHi = 0;
		vals.forEach((v, i) => {
			if (v == null) return;
			if (lo == null || v < lo) (lo = v), (iLo = i);
			if (hi == null || v > hi) (hi = v), (iHi = i);
		});
		return { lo, hi, iLo, iHi };
	};
	const pressureTS = (ts.node || {}).pressure || {};
	const junctions = Object.entries(data.node_results || {})
		.filter(([name]) => (types[name] || "Junction") === "Junction")
		.map(([name, r]) => ({ name, ...span(pressureTS[name], r.pressure) }));
	const velTS = (ts.link || {}).velocity || {};
	const flowTS = (ts.link || {}).flowrate || (ts.link || {}).flow || {};
	const links = Object.entries(data.link_results || {}).map(([name, r]) => ({
		name,
		type: types[name] || "Pipe",
		vel: span(velTS[name], r.velocity, true),
		flow: span(flowTS[name], r.flow, true),
	}));

	const s = data.summary || {};
	const st = data.settings || {};
	const pMin = s.min_junction_pressure_all_steps ?? Math.min(...junctions.map((j) => j.lo ?? Infinity));
	const pMax = s.max_junction_pressure_all_steps ?? Math.max(...junctions.map((j) => j.hi ?? -Infinity));
	const vMax = Math.max(0, ...links.map((l) => l.vel.hi ?? 0));
	const bands = PRESSURE_BANDS.map((b, i) => ({
		...b,
		count: junctions.filter((j) => j.hi != null && j.hi < b.max && j.hi >= (i ? PRESSURE_BANDS[i - 1].max : -Infinity)).length,
	}));
	const fmt = (v, d = 1) => (v == null || !isFinite(v) ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));

	const card = (title, value, sub) => `
		<div style="flex:1 1 170px;border:1px solid var(--border-color);border-radius:var(--border-radius-md);padding:12px 14px;background:var(--card-bg)">
			<div class="text-muted" style="font-size:var(--text-xs);text-transform:uppercase;letter-spacing:.04em">${title}</div>
			<div style="font-size:var(--text-2xl);font-weight:600;margin-top:2px">${value}</div>
			<div class="text-muted" style="font-size:var(--text-sm)">${sub}</div>
		</div>`;

	const link = (name) => `<a href="/app/spatial-feature/${encodeURIComponent(name)}" data-feature="${frappe.utils.escape_html(name)}">${frappe.utils.escape_html(name)}</a>`;
	const table = (title, rows, cols) => `
		<div style="flex:1 1 300px;min-width:0">
			<div style="font-weight:600;margin:4px 0 6px">${title}</div>
			<table class="table table-sm table-bordered" style="font-size:var(--text-sm);margin:0">
				<thead><tr>${cols.map((c) => `<th style="${c.right ? "text-align:right" : ""}">${c.label}</th>`).join("")}</tr></thead>
				<tbody>${
					rows.length
						? rows.map((r) => `<tr>${cols.map((c) => `<td style="${c.right ? "text-align:right" : ""}">${c.value(r)}</td>`).join("")}</tr>`).join("")
						: `<tr><td colspan="${cols.length}" class="text-muted">${__("None")}</td></tr>`
				}</tbody>
			</table>
		</div>`;

	const lowest = [...junctions].filter((j) => j.lo != null).sort((a, b) => a.lo - b.lo).slice(0, 10);
	const highest = [...junctions].filter((j) => j.hi != null).sort((a, b) => b.hi - a.hi).slice(0, 10);
	const fastest = [...links].filter((l) => l.vel.hi != null).sort((a, b) => b.vel.hi - a.vel.hi).slice(0, 10);
	const totalJ = junctions.length || 1;

	wrapper.html(`
		<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px">
			${card(__("Junction pressure (whole run)"), `${fmt(pMin)} – ${fmt(pMax)} m`, __("lowest – highest at any hour"))}
			${card(__("Peak velocity"), `${fmt(vMax, 2)} m/s`, vMax > 2 ? __("above 2 m/s - check for surge / wear") : __("within the usual 0.5–2 m/s"))}
			${card(__("Network"), `${data.node_count ?? junctions.length} / ${data.link_count ?? links.length}`, __("nodes / links simulated"))}
			${card(__("Simulated"), `${fmt(st.duration_hours, 0)} h`, `${hours.length || 1} ${__("timesteps")} · ${st.headloss || "?"} · ${st.demand_model || "?"} · ${st.flow_units || ""}`)}
		</div>
		<div style="margin-bottom:16px">
			<div style="font-weight:600;margin-bottom:6px">${__("Junctions by peak pressure")}</div>
			<div style="display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--gray-200)">
				${bands.map((b) => (b.count ? `<div title="${frappe.utils.escape_html(b.label)}: ${b.count}" style="flex:${b.count / totalJ};background:${b.color}"></div>` : "")).join("")}
			</div>
			<div style="display:flex;flex-wrap:wrap;gap:14px;margin-top:6px;font-size:var(--text-sm)">
				${bands.map((b) => `<span><span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:${b.color};margin-right:5px"></span>${b.label}: <b>${b.count}</b></span>`).join("")}
			</div>
		</div>
		<div style="display:flex;flex-wrap:wrap;gap:16px">
			${table(__("Lowest pressure"), lowest, [
				{ label: __("Junction"), value: (r) => link(r.name) },
				{ label: __("Min (m)"), value: (r) => fmt(r.lo), right: true },
				{ label: __("At"), value: (r) => hourLabel(r.iLo) },
			])}
			${table(__("Highest pressure"), highest, [
				{ label: __("Junction"), value: (r) => link(r.name) },
				{ label: __("Max (m)"), value: (r) => fmt(r.hi), right: true },
				{ label: __("At"), value: (r) => hourLabel(r.iHi) },
			])}
			${table(__("Fastest pipes"), fastest, [
				{ label: __("Link"), value: (r) => link(r.name) },
				{ label: __("Velocity (m/s)"), value: (r) => fmt(r.vel.hi, 2), right: true },
				{ label: __("Flow (L/s)"), value: (r) => fmt(r.flow.hi * 1000, 2), right: true },
			])}
		</div>
		${
			(data.warnings || []).length
				? `<div class="alert alert-warning" style="margin-top:14px">${data.warnings.map((w) => frappe.utils.escape_html(w)).join("<br>")}</div>`
				: ""
		}
		<div class="text-muted" style="font-size:var(--text-xs);margin-top:10px">${__(
			"Pressures and velocities are the extremes over every hour of the run. Hour-by-hour values per element: View → Node Results / Link Results."
		)}</div>
	`);

	// Readable names (e.g. "J-014") next to the feature IDs in the tables.
	const names = [...new Set([...lowest, ...highest, ...fastest].map((r) => r.name))];
	if (names.length) {
		frappe.db.get_list("Spatial Feature", { filters: { name: ["in", names] }, fields: ["name", "title"], limit: names.length }).then((rows) => {
			rows.forEach((r) => {
				if (!r.title || r.title === r.name) return;
				wrapper.find(`a[data-feature="${r.name}"]`).each(function () {
					$(this).html(`${frappe.utils.escape_html(r.title)} <span class="text-muted">· ${frappe.utils.escape_html(r.name)}</span>`);
				});
			});
		});
	}
}
