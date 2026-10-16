"""Read sensor data that other modules collect, for the water network.

Devices, LoRaWAN ingestion and the readings themselves belong to the
sensor modules (their Sensor Readings doctype: sensor_name, deveui,
sensor_type, value, units, timestamp, battery, rssi, snr). upande_spatial
only reads them: a Network Sensor Link says which device / sensor type
measures which network element and how to convert its values into model
units (m, L/s, m3). Nothing is copied.

Everything that needs readings goes through readings() / latest() here, so
if Sensor Readings ever lives on another site, only this module changes."""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime

MODEL_UNITS = {"Pressure": "m", "Flow rate": "L/s", "Volume": "m3", "Level": "m", "Head": "m", "Other": ""}
# Which network elements a quantity is normally measured on (a hint, not enforced).
QUANTITY_ROLES = {
	"Pressure": ("Junction", "Tank", "Reservoir"),
	"Head": ("Junction", "Tank", "Reservoir"),
	"Level": ("Tank", "Reservoir"),
	"Flow rate": ("Pipe", "Pump", "Valve"),
	"Volume": ("Pipe", "Pump", "Valve"),
}


def source_available(source_doctype="Sensor Readings"):
	return bool(frappe.db.exists("DocType", source_doctype))


def convert(link, raw):
	"""Source value -> model units: value * factor + offset (flows also * direction)."""
	if raw is None:
		return None
	value = flt(raw) * (flt(link.factor) or 1.0) + flt(link.offset)
	if link.quantity == "Flow rate" and str(link.flow_direction or "+1").strip() == "-1":
		value = -value
	return value


def _rows(link, start=None, end=None, limit=2000, latest_only=False):
	filters = {"deveui": link.deveui}
	if link.sensor_type:
		filters["sensor_type"] = link.sensor_type
	if start or end:
		filters["timestamp"] = ["between", [start or "1900-01-01", end or now_datetime()]]
	return frappe.get_all(
		link.source_doctype or "Sensor Readings",
		filters=filters,
		fields=["name", "timestamp", "value", "units", "sensor_type", "battery", "rssi", "snr"],
		order_by="timestamp desc" if latest_only else "timestamp asc",
		limit_page_length=1 if latest_only else cint(limit) or 2000,
		ignore_permissions=True,  # the caller already checked access to the link
	)


def readings(link, start=None, end=None, limit=2000):
	"""[{timestamp, value (model units), raw, raw_unit, battery, rssi, snr}] oldest first."""
	if isinstance(link, str):
		link = frappe.get_doc("Network Sensor Link", link)
	if not source_available(link.source_doctype or "Sensor Readings"):
		return []
	return [
		{"timestamp": r.timestamp, "value": convert(link, r.value), "raw": r.value, "raw_unit": r.units,
		 "battery": r.battery, "rssi": r.rssi, "snr": r.snr}
		for r in _rows(link, start, end, limit)
	]


def latest(link):
	"""The newest reading in model units, or None."""
	if isinstance(link, str):
		link = frappe.get_doc("Network Sensor Link", link)
	if not source_available(link.source_doctype or "Sensor Readings"):
		return None
	rows = _rows(link, latest_only=True)
	if not rows:
		return None
	r = rows[0]
	return {"timestamp": r.timestamp, "value": convert(link, r.value), "raw": r.value, "raw_unit": r.units}


@frappe.whitelist()
def get_readings(link, hours=24, limit=500):
	"""Readings of one linked sensor for the last `hours`, in model units."""
	doc = frappe.get_doc("Network Sensor Link", link)
	doc.check_permission("read")
	src = doc.source_doctype or "Sensor Readings"
	if not source_available(src):
		return {"available": False, "message": _("{0} isn't installed on this site - the sensor module that collects the readings lives elsewhere.").format(src), "readings": []}
	start = add_to_date(now_datetime(), hours=-flt(hours or 24))
	rows = readings(doc, start=start, limit=limit)
	return {"available": True, "unit": doc.model_unit, "readings": rows,
		"message": None if rows else _("No readings for {0} in the last {1} h.").format(doc.deveui, hours)}


def refresh_latest():
	"""Scheduler (every 15 min): cache each enabled link's newest reading."""
	for name in frappe.get_all("Network Sensor Link", filters={"enabled": 1}, pluck="name"):
		try:
			_refresh_one(name)
		except Exception as e:  # one bad link must not stop the others
			frappe.db.set_value("Network Sensor Link", name, {"last_checked_at": now_datetime(),
				"source_status": f"Error: {str(e)[:120]}"}, update_modified=False)
	frappe.db.commit()


@frappe.whitelist()
def refresh(link=None):
	"""Refresh now - one link, or all of them."""
	if link:
		frappe.get_doc("Network Sensor Link", link).check_permission("write")
	if link:
		_refresh_one(link)
	else:
		refresh_latest()
	return True


def _refresh_one(link):
	doc = frappe.get_doc("Network Sensor Link", link)
	src = doc.source_doctype or "Sensor Readings"
	values = {"last_checked_at": now_datetime()}
	if not source_available(src):
		values["source_status"] = f"{src} not on this site"
	else:
		r = latest(doc)
		values.update({"last_value": r["value"], "last_raw_value": flt(r["raw"]), "last_reading_at": get_datetime(r["timestamp"]),
			"source_status": "OK"} if r else {"source_status": "No readings yet"})
	frappe.db.set_value("Network Sensor Link", link, values, update_modified=False)
