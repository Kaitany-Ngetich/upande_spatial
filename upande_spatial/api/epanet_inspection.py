"""Plan field inspections from an EPANET simulation run.

A run's hourly results are checked against the network's Inspection
Thresholds (EPANET Network, overridable per call). Every junction or pipe
that breaks a rule becomes a waypoint - with the matching checklist and the
reason in its instructions - in a new Field Inspection Plan, ordered as a
nearest-next walk from the reservoir.

Rules (extremes over every reported hour, not just the final step):

	junctions   negative_pressure   min < 0 m                     critical
	            over_rating         max > pipe rating             critical
	            low_pressure        min < minimum service         high
	            pressure_swing      max - min > max swing         high
	            emitter_pressure    max > emitter limit (if set)  high
	pipes       high_velocity       peak |v| > max velocity       high
	            stagnant            peak |v| < stagnant limit     routine
	            steep_headloss      |head drop| / length > limit  routine
	            flow_reversal       flow runs both ways           routine

Model results are only as good as the inputs (DEM elevations, assumed
reservoir level, demands), so these are "verify in the field" flags; the
inspection answers tell whether the model or the network needs fixing."""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from upande_spatial.api.inspection import create_run as _create_run
from upande_spatial.api.inspection import distance_m, is_manager

SEVERITY_ORDER = {"critical": 0, "high": 1, "routine": 2}
FLOW_EPS = 1e-5  # m3/s (0.01 L/s) - smaller flows count as "no flow"

THRESHOLDS = {
	# key: (EPANET Network field, default)
	"pipe_rating_m": ("insp_pipe_rating_m", 60.0),
	"min_pressure_m": ("insp_min_pressure_m", 10.0),
	"max_emitter_pressure_m": ("insp_max_emitter_pressure_m", 0.0),
	"max_swing_m": ("insp_max_swing_m", 20.0),
	"max_velocity": ("insp_max_velocity", 2.0),
	"min_velocity": ("insp_min_velocity", 0.3),
	"max_headloss_m_km": ("insp_max_headloss_m_km", 10.0),
}

RULES = {
	"negative_pressure": {"label": "Negative pressure", "kind": "node", "severity": "critical", "template": "Low pressure check"},
	"over_rating": {"label": "Above pipe rating", "kind": "node", "severity": "critical", "template": "Burst risk check"},
	"low_pressure": {"label": "Too low to irrigate", "kind": "node", "severity": "high", "template": "Low pressure check"},
	"pressure_swing": {"label": "Large daily pressure swing", "kind": "node", "severity": "high", "template": "Burst risk check"},
	"emitter_pressure": {"label": "Too high for emitters", "kind": "node", "severity": "high", "template": "Burst risk check"},
	"high_velocity": {"label": "High velocity", "kind": "link", "severity": "high", "template": "High velocity pipe check"},
	"stagnant": {"label": "Stagnant pipe", "kind": "link", "severity": "routine", "template": "Flushing check"},
	"steep_headloss": {"label": "Steep headloss", "kind": "link", "severity": "routine", "template": "Valve & blockage check"},
	"flow_reversal": {"label": "Flow reverses", "kind": "link", "severity": "routine", "template": "Valve & blockage check"},
}

TEMPLATES = {
	"Low pressure check": {
		"asset_role": None,
		"description": "Junction where the model shows pressure too low to irrigate (or negative).",
		"questions": [
			("Measured pressure (m)", "Number", 1, "< 10", ""),
			("Water reaching the emitters / outlets?", "Yes/No", 1, "= No", ""),
			("Upstream valves fully open?", "Yes/No", 1, "= No", ""),
			("Filter or strainer blocked?", "Yes/No", 1, "= Yes", ""),
			("Visible leak nearby?", "Yes/No", 1, "= Yes", ""),
			("Photo", "Photo", 0, "", ""),
			("Notes", "Text", 0, "", ""),
		],
	},
	"Burst risk check": {
		"asset_role": None,
		"description": "Junction where the model shows pressure above the pipe rating, too high for emitters, or swinging strongly during the day.",
		"questions": [
			("Measured pressure (m)", "Number", 1, "", "Write the gauge reading; compare with the pipe rating."),
			("Signs of leaks or bursts (wet ground, spray)?", "Yes/No", 1, "= Yes", ""),
			("Joints and fittings condition", "Select", 1, "in Leaking,Damaged", "Good\nWeeping\nLeaking\nDamaged"),
			("Pressure-reducing valve present?", "Yes/No", 0, "", ""),
			("Photo", "Photo", 0, "", ""),
			("Notes", "Text", 0, "", ""),
		],
	},
	"High velocity pipe check": {
		"asset_role": None,
		"description": "Pipe where the model shows velocity above the limit - erosion, noise, water hammer.",
		"questions": [
			("Noise or vibration in the pipe?", "Yes/No", 1, "= Yes", ""),
			("Thrust blocks / anchors intact?", "Yes/No", 1, "= No", ""),
			("Joints or bends leaking?", "Yes/No", 1, "= Yes", ""),
			("Photo", "Photo", 0, "", ""),
			("Notes", "Text", 0, "", ""),
		],
	},
	"Flushing check": {
		"asset_role": None,
		"description": "Pipe where water barely moves - sediment builds up; flush through the nearest drain/washout.",
		"questions": [
			("Flushed through drain / washout?", "Yes/No", 1, "= No", ""),
			("Water clarity when flushing", "Select", 1, "in Dirty,Very dirty", "Clear\nSlightly cloudy\nDirty\nVery dirty"),
			("Photo", "Photo", 0, "", ""),
			("Notes", "Text", 0, "", ""),
		],
	},
	"Valve & blockage check": {
		"asset_role": None,
		"description": "Pipe with steep headloss or flow that reverses during the day - partly closed valve, blockage, or a failed check valve.",
		"questions": [
			("Valves along this pipe fully open?", "Yes/No", 1, "= No", ""),
			("Check valve working (if fitted)?", "Select", 1, "= Not working", "Working\nNot working\nNone fitted"),
			("Signs of blockage or damage?", "Yes/No", 1, "= Yes", ""),
			("Photo", "Photo", 0, "", ""),
			("Notes", "Text", 0, "", ""),
		],
	},
}


def _ensure_templates(names):
	for name in names:
		if frappe.db.exists("Field Inspection Template", name):
			continue
		t = TEMPLATES[name]
		frappe.get_doc({
			"doctype": "Field Inspection Template", "template_name": name, "asset_role": t["asset_role"],
			"description": t["description"],
			"questions": [{"question": q, "answer_type": at, "required": req, "fail_when": fw or None,
				"options": opts if at == "Select" else None, "help_text": opts if at != "Select" and opts else None}
				for q, at, req, fw, opts in t["questions"]],
		}).insert(ignore_permissions=True)


def network_thresholds(network):
	"""The network's Inspection Thresholds (defaults where unset)."""
	doc = frappe.get_doc("EPANET Network", network)
	out = {}
	for key, (field, default) in THRESHOLDS.items():
		value = doc.get(field) if doc.meta.has_field(field) else None
		out[key] = flt(value) if value not in (None, "") else default
	return out


def _point_of(feature):
	"""(lat, lon) to send an inspector to: the point itself, or a line's middle."""
	g = feature.get("_geom") or {}
	if g.get("type") == "Point":
		return g["coordinates"][1], g["coordinates"][0]
	from shapely.geometry import shape

	s = shape(g)
	p = s.interpolate(0.5, normalized=True) if "Line" in s.geom_type else s.representative_point()
	return p.y, p.x


def _hour(times, i):
	if not times or i is None or i >= len(times):
		return None
	h = times[i] / 3600.0
	return f"{int(h):02d}:{int(round((h % 1) * 60)):02d}"


@frappe.whitelist()
def preview(run, thresholds=None, rules=None):
	"""What a plan from this run would contain: per flagged element its
	reasons (rule, value, hour), severity, checklist and position, plus
	counts per rule. Nothing is created."""
	from upande_spatial.api.epanet import _build_model, network_elements

	doc = frappe.get_doc("EPANET Simulation Run", run)
	frappe.has_permission("EPANET Network", "read", doc.network, throw=True)
	if doc.status != "Success":
		frappe.throw(_("{0} did not succeed - nothing to inspect.").format(run))
	data = json.loads(doc.results or "{}")
	limits = network_thresholds(doc.network)
	if isinstance(thresholds, str):
		thresholds = json.loads(thresholds or "{}")
	for k, v in (thresholds or {}).items():
		if k in limits and v not in (None, ""):
			limits[k] = flt(v)
	if isinstance(rules, str):
		rules = json.loads(rules)
	# Default: critical + high rules. Routine ones (stagnant, headloss, reversal)
	# tend to flag many laterals at once, so they're opt-in.
	enabled = set(rules) if rules else {k for k, r in RULES.items() if r["severity"] != "routine"}

	ts = data.get("timeseries") or {}
	times = ts.get("times") or []
	types = data.get("element_types") or {}
	node_ts, link_ts = ts.get("node") or {}, ts.get("link") or {}
	elements = network_elements(doc.network)
	flags = {}

	def flag(name, rule, value, unit, hour, detail):
		f = flags.setdefault(name, {"element": name, "reasons": []})
		f["reasons"].append({"rule": rule, "label": RULES[rule]["label"], "severity": RULES[rule]["severity"],
			"value": round(value, 2), "unit": unit, "hour": hour, "detail": detail})

	# junctions
	pressure = node_ts.get("pressure") or {}
	for name, r in (data.get("node_results") or {}).items():
		if types.get(name, "Junction") != "Junction":
			continue
		series = [v for v in (pressure.get(name) or [r.get("pressure")]) if v is not None]
		if not series:
			continue
		lo, hi = min(series), max(series)
		i_lo, i_hi = series.index(lo), series.index(hi)
		if "negative_pressure" in enabled and lo < 0:
			flag(name, "negative_pressure", lo, "m", _hour(times, i_lo), f"pressure falls to {lo:.1f} m")
		elif "low_pressure" in enabled and limits["min_pressure_m"] and lo < limits["min_pressure_m"]:
			flag(name, "low_pressure", lo, "m", _hour(times, i_lo), f"min {lo:.1f} m < {limits['min_pressure_m']:g} m service pressure")
		if "over_rating" in enabled and limits["pipe_rating_m"] and hi > limits["pipe_rating_m"]:
			flag(name, "over_rating", hi, "m", _hour(times, i_hi), f"max {hi:.1f} m > {limits['pipe_rating_m']:g} m pipe rating")
		elif "emitter_pressure" in enabled and limits["max_emitter_pressure_m"] and hi > limits["max_emitter_pressure_m"]:
			flag(name, "emitter_pressure", hi, "m", _hour(times, i_hi), f"max {hi:.1f} m > {limits['max_emitter_pressure_m']:g} m emitter limit")
		if "pressure_swing" in enabled and limits["max_swing_m"] and hi - lo > limits["max_swing_m"]:
			flag(name, "pressure_swing", hi - lo, "m", None, f"swings {hi - lo:.1f} m ({lo:.1f}–{hi:.1f} m) during the run")

	# pipes
	velocity, flow, head = link_ts.get("velocity") or {}, link_ts.get("flow") or {}, node_ts.get("head") or {}
	wn = None
	if "steep_headloss" in enabled and limits["max_headloss_m_km"]:
		try:
			wn = _build_model(frappe.get_doc("EPANET Network", doc.network))["wn"]
		except Exception:
			wn = None  # network changed since the run - skip the headloss rule
	for name, r in (data.get("link_results") or {}).items():
		if types.get(name, "Pipe") != "Pipe":
			continue
		v = [abs(x) for x in (velocity.get(name) or [r.get("velocity")]) if x is not None]
		if v:
			vmax = max(v)
			if "high_velocity" in enabled and limits["max_velocity"] and vmax > limits["max_velocity"]:
				flag(name, "high_velocity", vmax, "m/s", _hour(times, v.index(vmax)), f"peak {vmax:.2f} m/s > {limits['max_velocity']:g} m/s")
			elif "stagnant" in enabled and limits["min_velocity"] and vmax < limits["min_velocity"]:
				flag(name, "stagnant", vmax, "m/s", None, f"peak only {vmax:.2f} m/s (< {limits['min_velocity']:g} m/s)")
		q = [x for x in (flow.get(name) or []) if x is not None]
		if "flow_reversal" in enabled and q and max(q) > FLOW_EPS and min(q) < -FLOW_EPS:
			flag(name, "flow_reversal", max(abs(x) for x in q) * 1000, "L/s", None, "flow runs in both directions during the run")
		if wn is not None and name in wn.link_name_list:
			link = wn.get_link(name)
			ha, hb = head.get(link.start_node_name) or [], head.get(link.end_node_name) or []
			if ha and hb and link.length:
				grads = [abs(a - b) / link.length * 1000 for a, b in zip(ha, hb) if a is not None and b is not None]
				if grads and max(grads) > limits["max_headloss_m_km"]:
					g = max(grads)
					flag(name, "steep_headloss", g, "m/km", _hour(times, grads.index(g)), f"{g:.1f} m per km > {limits['max_headloss_m_km']:g}")

	out = []
	for name, f in flags.items():
		el = elements.get(name)
		if not el or not el.get("_geom"):
			continue
		f["reasons"].sort(key=lambda x: SEVERITY_ORDER[x["severity"]])
		lat, lon = _point_of(el)
		top = f["reasons"][0]
		out.append({**f, "title": el.get("title") or name, "role": el.get("feature_role"), "severity": top["severity"],
			"template": RULES[top["rule"]]["template"], "lat": lat, "lon": lon})
	out.sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], x["element"]))
	counts = {}
	for f in out:
		for r in f["reasons"]:
			counts[r["rule"]] = counts.get(r["rule"], 0) + 1
	return {"run": run, "network": doc.network, "thresholds": limits, "rules": {k: RULES[k] for k in RULES},
		"enabled": sorted(enabled), "counts": counts, "elements": out}


@frappe.whitelist()
def create_plan(run, thresholds=None, rules=None, plan_name=None, inspector=None, checkin_radius_m=15, create_run=1):
	"""Create a Field Inspection Plan (and a run of it) for every element
	preview() flags. Waypoints are ordered as a nearest-next walk from the
	reservoir; each gets the checklist for its most severe issue and all
	its reasons in the instructions."""
	if not is_manager():
		frappe.throw(_("Only Inspection Managers can plan inspections."), frappe.PermissionError)
	p = preview(run, thresholds, rules)
	if not p["elements"]:
		frappe.throw(_("Nothing in {0} breaks the selected rules - no inspection needed.").format(run))
	from upande_spatial.api.epanet import network_elements

	net = frappe.get_doc("EPANET Network", p["network"])
	elements = network_elements(p["network"])
	start = next((_point_of(e) for e in elements.values() if e.get("feature_role") == "Reservoir" and e.get("_geom")), None)
	todo = list(p["elements"])
	cur = start or (todo[0]["lat"], todo[0]["lon"])
	route = []
	while todo:
		nxt = min(todo, key=lambda e: distance_m(cur[0], cur[1], e["lat"], e["lon"]))
		route.append(nxt)
		todo.remove(nxt)
		cur = (nxt["lat"], nxt["lon"])

	_ensure_templates({e["template"] for e in route})
	name = (plan_name or f"{p['network']} - inspection from {run}").strip()
	base, n = name, 2
	while frappe.db.exists("Field Inspection Plan", name):
		name, n = f"{base} ({n})", n + 1
	farm = net.reference_name if net.reference_doctype == "Farm" else None
	severities = {}
	for e in route:
		severities[e["severity"]] = severities.get(e["severity"], 0) + 1
	plan = frappe.get_doc({
		"doctype": "Field Inspection Plan", "plan_name": name, "farm": farm, "company": net.get("company"),
		"default_inspector": inspector or frappe.session.user, "checkin_radius_m": flt(checkin_radius_m) or 15,
		"frequency": "One-off",
		"description": (f"From EPANET run {run} of {p['network']}: " + ", ".join(f"{v} {k}" for k, v in severities.items())
			+ ". Model-based flags - verify in the field. Thresholds: "
			+ ", ".join(f"{k}={v:g}" for k, v in p["thresholds"].items()) + "."),
		"waypoints": [{
			"waypoint_title": f"{e['title']} - {e['reasons'][0]['label']}",
			"asset": e["element"], "template": e["template"], "latitude": e["lat"], "longitude": e["lon"],
			"instructions": f"[{e['severity'].upper()}] " + "; ".join(
				r["detail"] + (f" at {r['hour']}" if r.get("hour") else "") for r in e["reasons"]),
		} for e in route],
	}).insert()
	run_name = _create_run(plan.name, inspector=inspector or frappe.session.user) if cint(create_run) else None
	return {"plan": plan.name, "run": run_name, "waypoints": len(route), "by_severity": severities}
