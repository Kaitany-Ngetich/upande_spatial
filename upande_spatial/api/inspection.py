"""Field inspections: planned routes of waypoints that inspectors walk with
the mobile app (or Mergin Maps), checking in at each waypoint and filling
in a checklist.

	Field Inspection Template   checklist (questions + what answer fails)
	Field Inspection Plan       ordered waypoints, checklist per waypoint,
	                            check-in radius, one-off or repeating
	Field Inspection Run        one execution of a plan by one inspector
	Field Inspection Visit      one waypoint of one run: check-in position,
	                            answers, pass/fail
	GPS Track                   the line walked - a Spatial Feature (role
	                            "GPS Track") owned by the run

Waypoint positions are Spatial Features too (role "Inspection Waypoint",
owned by the plan), so plans show up in the Map Viewer and QGIS like any
other layer and can be drawn there.

App flow (all whitelisted below):
	my_runs -> download_run -> check_in -> submit_visit (per waypoint)
	-> upload_track (any time, in chunks) -> finish_run

Writes are checked here, not by DocType permissions: an inspector may only
touch runs assigned to them; Inspection Managers / System Managers any."""

import json
import math

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, flt, get_datetime, getdate, now_datetime, nowdate

MANAGER_ROLES = {"System Manager", "Inspection Manager"}
WAYPOINT_ROLE = "Inspection Waypoint"
TRACK_ROLE = "GPS Track"
OPEN_RUN_STATUSES = ("Planned", "Downloaded", "In Progress")
EARTH_RADIUS_M = 6371000.0


# ── helpers ──────────────────────────────────────────────────────
def distance_m(lat1, lon1, lat2, lon2):
	"""Great-circle distance in metres (haversine)."""
	p1, p2 = math.radians(flt(lat1)), math.radians(flt(lat2))
	dp, dl = p2 - p1, math.radians(flt(lon2) - flt(lon1))
	a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
	return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def is_manager(user=None):
	return bool(MANAGER_ROLES & set(frappe.get_roles(user or frappe.session.user)))


def _run_for_user(run):
	doc = frappe.get_doc("Field Inspection Run", run)
	if doc.inspector != frappe.session.user and not is_manager():
		frappe.throw(_("Run {0} is assigned to someone else.").format(run), frappe.PermissionError)
	return doc


def _visit_for_user(visit):
	doc = frappe.get_doc("Field Inspection Visit", visit)
	run = _run_for_user(doc.run)
	if run.status == "Cancelled":
		frappe.throw(_("Run {0} was cancelled.").format(run.name))
	return doc, run


def _feature_point(name):
	"""(lat, lon) of a Point Spatial Feature, or None."""
	if not name:
		return None
	raw = frappe.db.get_value("Spatial Feature", name, "geometry")
	try:
		fc = json.loads(raw or "{}")
		geom = (fc.get("features") or [{}])[0].get("geometry") or fc.get("geometry") or {}
	except ValueError:
		return None
	if geom.get("type") == "Point":
		lon, lat = geom["coordinates"][:2]
		return lat, lon
	return None


def _feature_summary(name):
	"""What the app shows about an asset: role, title, layer, properties,
	geometry (GeoJSON)."""
	if not name:
		return None
	row = frappe.db.get_value("Spatial Feature", name,
		["name", "title", "feature_role", "layer", "properties", "geometry", "length_m", "area_sq_m"], as_dict=True)
	if not row:
		return None
	try:
		fc = json.loads(row.geometry or "{}")
		geometry = (fc.get("features") or [{}])[0].get("geometry") or fc.get("geometry")
	except ValueError:
		geometry = None
	return {
		"name": row.name, "title": row.title, "role": row.feature_role, "layer": row.layer,
		"properties": json.loads(row.properties or "{}"), "geometry": geometry,
		"length_m": row.length_m, "area_sq_m": row.area_sq_m,
	}


# ── checklist rules ──────────────────────────────────────────────
def _norm(value):
	return str(value if value is not None else "").strip().lower()


def answer_fails(fail_when, answer):
	"""True when `answer` matches the question's fail rule:
	"= No", "!= OK", "> 60", ">= 60", "< 10", "<= 10", "in Broken,Leaking".
	Blank rule never fails; numeric rules on a non-number never fail."""
	rule = (fail_when or "").strip()
	if not rule or answer in (None, ""):
		return False
	low = rule.lower()
	if low.startswith("in "):
		return _norm(answer) in {_norm(x) for x in rule[3:].split(",")}
	for op in (">=", "<=", "!=", "=", ">", "<"):
		if rule.startswith(op):
			target = rule[len(op):].strip()
			if op in ("=", "!="):
				hit = _norm(answer) == _norm(target)
				return hit if op == "=" else not hit
			try:
				a, t = float(answer), float(target)
			except (TypeError, ValueError):
				return False
			return {">": a > t, ">=": a >= t, "<": a < t, "<=": a <= t}[op]
	return _norm(answer) == _norm(rule)  # bare value = "equals"


def validate_fail_rule(rule):
	rule = (rule or "").strip()
	if not rule or rule.lower().startswith("in "):
		return
	for op in (">=", "<=", ">", "<"):
		if rule.startswith(op):
			try:
				float(rule[len(op):].strip())
			except ValueError:
				frappe.throw(_("Fail rule {0}: '{1}' needs a number after it.").format(frappe.bold(rule), op))
			return


def template_for(waypoint_template=None, asset=None, plan_default=None):
	"""Checklist for a waypoint: its own, else the asset role's, else the plan's."""
	if waypoint_template:
		return waypoint_template
	if asset:
		role = frappe.db.get_value("Spatial Feature", asset, "feature_role")
		if role:
			t = frappe.db.get_value("Field Inspection Template", {"asset_role": role}, "name")
			if t:
				return t
	return plan_default


# ── runs ─────────────────────────────────────────────────────────
@frappe.whitelist()
def create_run(plan, inspector=None, scheduled_date=None):
	"""A new run of a plan with one Pending visit per waypoint, in route order."""
	if not is_manager():
		frappe.throw(_("Only Inspection Managers can create runs."), frappe.PermissionError)
	p = frappe.get_doc("Field Inspection Plan", plan)
	inspector = inspector or p.default_inspector
	if not inspector:
		frappe.throw(_("Plan {0} has no default inspector - choose one.").format(plan))
	run = frappe.get_doc({
		"doctype": "Field Inspection Run", "plan": p.name, "inspector": inspector,
		"scheduled_date": scheduled_date or nowdate(), "status": "Planned",
		"checkin_radius_m": p.checkin_radius_m or 15, "total_waypoints": len(p.waypoints),
	}).insert(ignore_permissions=True)
	for wp in sorted(p.waypoints, key=lambda w: (cint(w.sequence), w.idx)):
		frappe.get_doc({
			"doctype": "Field Inspection Visit", "run": run.name, "plan": p.name, "inspector": inspector,
			"sequence": wp.sequence, "waypoint_title": wp.waypoint_title or f"Waypoint {wp.sequence}",
			"asset": wp.asset, "point": wp.point,
			"template": template_for(wp.template, wp.asset, p.default_template),
			"target_lat": wp.latitude, "target_lon": wp.longitude, "status": "Pending",
		}).insert(ignore_permissions=True)
	return run.name


def refresh_run_summary(run_name):
	counts = dict(frappe.get_all("Field Inspection Visit", filters={"run": run_name}, fields=["status", {"COUNT": "*", "as": "n"}],
		group_by="status", as_list=True))
	total = sum(counts.values())
	frappe.db.set_value("Field Inspection Run", run_name, {
		"total_waypoints": total,
		"passed": counts.get("Passed", 0), "failed": counts.get("Failed", 0), "skipped": counts.get("Skipped", 0),
		"visited": counts.get("Passed", 0) + counts.get("Failed", 0),
	}, update_modified=False)


def create_scheduled_runs():
	"""Daily: a run for every enabled repeating plan that's due."""
	today = getdate(nowdate())
	step = {"Daily": lambda d: add_days(d, 1), "Weekly": lambda d: add_days(d, 7), "Monthly": lambda d: add_months(d, 1)}
	for p in frappe.get_all("Field Inspection Plan", filters={"enabled": 1, "frequency": ["in", list(step)]},
			fields=["name", "frequency", "next_run_date", "start_date", "default_inspector"]):
		due = getdate(p.next_run_date or p.start_date or today)
		if due > today or not p.default_inspector:
			continue
		try:
			frappe.set_user("Administrator")
			create_run(p.name, scheduled_date=due)
			nxt = due
			while getdate(nxt) <= today:
				nxt = step[p.frequency](nxt)
			frappe.db.set_value("Field Inspection Plan", p.name, "next_run_date", nxt)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Field inspection run for {p.name} failed")


# ── app API ──────────────────────────────────────────────────────
@frappe.whitelist()
def my_runs(date=None, include_finished=0):
	"""Runs assigned to the logged-in user (open ones, scheduled up to
	`date`, default today; finished ones too with include_finished=1)."""
	filters = {"inspector": frappe.session.user, "scheduled_date": ["<=", date or nowdate()]}
	if not cint(include_finished):
		filters["status"] = ["in", list(OPEN_RUN_STATUSES)]
	return frappe.get_all("Field Inspection Run", filters=filters, order_by="scheduled_date asc, creation asc",
		fields=["name", "plan", "farm", "scheduled_date", "status", "total_waypoints", "visited", "passed", "failed",
			"skipped", "checkin_radius_m", "distance_walked_m", "modified"])


@frappe.whitelist()
def download_run(run):
	"""Everything the app needs to work this run offline: the run, its
	visits in route order (target position, asset details), and every
	checklist used. Marks a Planned run Downloaded."""
	doc = _run_for_user(run)
	if doc.status == "Planned":
		doc.db_set("status", "Downloaded")
	plan = frappe.get_doc("Field Inspection Plan", doc.plan)
	visits = frappe.get_all("Field Inspection Visit", filters={"run": run}, order_by="sequence asc, creation asc",
		fields=["name", "sequence", "waypoint_title", "status", "asset", "point", "template", "target_lat", "target_lon",
			"arrived_at", "within_radius", "completed_at", "skip_reason", "client_id"])
	instructions = {w.point: w.instructions for w in plan.waypoints if w.point}
	templates = {}
	for v in visits:
		v["instructions"] = instructions.get(v.point)
		v["asset_details"] = _feature_summary(v.asset)
		if v.template and v.template not in templates:
			t = frappe.get_doc("Field Inspection Template", v.template)
			templates[t.name] = {
				"name": t.name, "asset_role": t.asset_role, "description": t.description,
				"questions": [{
					"question": q.question, "answer_type": q.answer_type, "required": cint(q.required),
					"options": [o.strip() for o in (q.options or "").splitlines() if o.strip()],
					"fail_when": q.fail_when, "help_text": q.help_text,
				} for q in t.questions],
			}
	route = frappe.db.get_value("Spatial Feature", {"reference_doctype": "Field Inspection Plan", "reference_name": plan.name,
		"feature_role": "Inspection Route"}, "name")
	return {
		"run": doc.as_dict(no_default_fields=False),
		"plan": {"name": plan.name, "description": plan.description, "farm": plan.farm, "checkin_radius_m": plan.checkin_radius_m},
		"route": _feature_summary(route),
		"visits": visits,
		"templates": templates,
		"server_time": str(now_datetime()),
	}


@frappe.whitelist()
def check_in(visit, lat, lon, accuracy=None, arrived_at=None):
	"""Record arrival at a waypoint. Returns whether the inspector is close
	enough to start the checklist (distance <= the run's check-in radius)."""
	v, run = _visit_for_user(visit)
	if v.status != "Pending":
		frappe.throw(_("{0} is already {1}.").format(v.waypoint_title, v.status))
	dist = distance_m(lat, lon, v.target_lat, v.target_lon) if v.target_lat or v.target_lon else None
	radius = flt(run.checkin_radius_m) or 15
	within = dist is not None and dist <= radius
	v.update({
		"arrived_at": get_datetime(arrived_at) if arrived_at else now_datetime(),
		"gps_lat": flt(lat), "gps_lon": flt(lon), "gps_accuracy_m": flt(accuracy) if accuracy not in (None, "") else None,
		"distance_from_target_m": round(dist, 1) if dist is not None else None, "within_radius": 1 if within else 0,
	})
	v.save(ignore_permissions=True)
	if run.status in ("Planned", "Downloaded"):
		run.db_set({"status": "In Progress", "started_at": run.started_at or v.arrived_at})
	return {"within_radius": within, "distance_m": v.distance_from_target_m, "radius_m": radius}


@frappe.whitelist()
def submit_visit(visit, answers=None, skip_reason=None, client_id=None):
	"""Save a waypoint's result. `answers` = [{"question", "answer",
	"photo"?}] (photo = a file_url from /api/method/upload_file attached to
	this visit). Pass `skip_reason` instead to skip the waypoint.
	Re-sending the same `client_id` (offline retry) returns the saved
	result instead of failing."""
	v, run = _visit_for_user(visit)
	if client_id and v.client_id == client_id and v.status != "Pending":
		return {"status": v.status, "visit": v.name, "duplicate": True}
	if v.status != "Pending":
		frappe.throw(_("{0} is already {1}.").format(v.waypoint_title, v.status))

	if skip_reason:
		v.update({"status": "Skipped", "skip_reason": skip_reason, "completed_at": now_datetime(), "client_id": client_id})
		v.save(ignore_permissions=True)
		refresh_run_summary(run.name)
		return {"status": v.status, "visit": v.name}

	if not v.within_radius and not is_manager():
		frappe.throw(_("Check in at {0} first - you need to be within {1} m of it.").format(
			v.waypoint_title, flt(run.checkin_radius_m) or 15))
	if isinstance(answers, str):
		answers = json.loads(answers)
	given = {_norm(a.get("question")): a for a in (answers or [])}
	questions = frappe.get_doc("Field Inspection Template", v.template).questions if v.template else []
	rows, failed, missing = [], False, []
	for q in questions:
		a = given.get(_norm(q.question)) or {}
		value, photo = a.get("answer"), a.get("photo")
		if q.answer_type == "Photo" and not value:
			value = photo
		if cint(q.required) and value in (None, "") and not photo:
			missing.append(q.question)
			continue
		bad = answer_fails(q.fail_when, value)
		failed = failed or bad
		rows.append({"question": q.question, "answer_type": q.answer_type, "answer": None if value is None else str(value),
			"photo": photo, "result": ("Fail" if bad else "Pass") if (q.fail_when or "").strip() else ""})
	if missing:
		frappe.throw(_("Answer required: {0}").format(", ".join(missing)))
	# answers to questions not on the checklist (e.g. a free note) are kept too
	known = {_norm(q.question) for q in questions}
	for key, a in given.items():
		if key not in known:
			rows.append({"question": a.get("question"), "answer_type": "Text", "answer": a.get("answer"), "photo": a.get("photo")})

	v.set("answers", rows)
	v.update({"status": "Failed" if failed else "Passed", "completed_at": now_datetime(), "client_id": client_id})
	v.save(ignore_permissions=True)
	refresh_run_summary(run.name)
	return {"status": v.status, "visit": v.name, "failed_questions": [r["question"] for r in rows if r.get("result") == "Fail"]}


@frappe.whitelist()
def upload_track(run, coordinates, start_time=None, end_time=None):
	"""Append GPS points ([[lon, lat], ...] in order) to the run's track.
	Call as often as convenient - each call extends the same line."""
	doc = _run_for_user(run)
	if isinstance(coordinates, str):
		coordinates = json.loads(coordinates)
	new = [[flt(c[0]), flt(c[1])] for c in coordinates or [] if len(c) >= 2]
	if not new:
		return {"track": doc.track, "points": 0, "distance_walked_m": doc.distance_walked_m}
	from upande_spatial.api.spatial import upsert_feature

	old, props = [], {}
	if doc.track and frappe.db.exists("Spatial Feature", doc.track):
		summary = _feature_summary(doc.track)
		geom = (summary or {}).get("geometry") or {}
		old = geom.get("coordinates") or [] if geom.get("type") == "LineString" else []
		props = (summary or {}).get("properties") or {}
	coords = old + new
	if len(coords) == 1:
		coords = coords * 2  # a LineString needs two positions; grows on the next upload
	props.update({
		"run": doc.name, "inspector": doc.inspector, "points": len(coords),
		"start_time": props.get("start_time") or start_time, "end_time": end_time or props.get("end_time"),
	})
	res = upsert_feature(
		geometry={"type": "LineString", "coordinates": coords},
		reference_doctype="Field Inspection Run", reference_name=doc.name, feature_role=TRACK_ROLE,
		farm=doc.farm, title=f"Track {doc.name}", properties=props, source_module="Field Inspection",
	)
	walked = sum(distance_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0]) for i in range(len(coords) - 1))
	doc.db_set({"track": res["name"], "distance_walked_m": round(walked, 1)})
	return {"track": res["name"], "points": len(coords), "distance_walked_m": round(walked, 1)}


@frappe.whitelist()
def finish_run(run, notes=None):
	"""Close a run: Completed when every waypoint is done, else Partially
	Completed (pending waypoints stay Pending)."""
	doc = _run_for_user(run)
	refresh_run_summary(doc.name)
	pending = frappe.db.count("Field Inspection Visit", {"run": doc.name, "status": "Pending"})
	doc.reload()
	doc.db_set({
		"status": "Partially Completed" if pending else "Completed", "finished_at": now_datetime(),
		"notes": notes or doc.notes,
	})
	return {"status": doc.status, "passed": doc.passed, "failed": doc.failed, "skipped": doc.skipped, "pending": pending}


# ── planning helpers ─────────────────────────────────────────────
@frappe.whitelist()
def add_waypoints_from_features(plan, features, template=None):
	"""Append a waypoint at each of these Spatial Features (points: the
	point itself; lines/polygons: their midpoint/centroid), e.g. every
	valve of a network. Returns the number added."""
	if not is_manager():
		frappe.throw(_("Only Inspection Managers can edit plans."), frappe.PermissionError)
	if isinstance(features, str):
		features = json.loads(features)
	from shapely.geometry import shape

	p = frappe.get_doc("Field Inspection Plan", plan)
	seq = max([cint(w.sequence) for w in p.waypoints] or [0])
	added = 0
	for name in features:
		f = _feature_summary(name)
		if not f or not f.get("geometry"):
			continue
		g = shape(f["geometry"])
		pt = g if g.geom_type == "Point" else (g.interpolate(0.5, normalized=True) if "Line" in g.geom_type else g.representative_point())
		seq += 1
		p.append("waypoints", {
			"sequence": seq, "waypoint_title": f.get("title") or f"{f.get('role') or 'Feature'} {name}",
			"asset": name, "template": template, "latitude": pt.y, "longitude": pt.x,
		})
		added += 1
	p.save()
	return added
