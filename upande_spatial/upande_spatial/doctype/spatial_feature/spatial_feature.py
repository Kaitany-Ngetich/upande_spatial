# Copyright (c) 2026, dev@upande.com and contributors
# For license information, please see license.txt

import json
import math

import frappe
from frappe import _
from frappe.model.document import Document

EARTH_RADIUS_M = 6371000.0


def _project_equirect(lat, lng, lat0_rad):
	"""Equirectangular projection centered on lat0 — good enough for a
	farm-sized polygon (a few km across), not meant for anything
	continental. Returns (x, y) in meters."""
	x = EARTH_RADIUS_M * math.radians(lng) * math.cos(lat0_rad)
	y = EARTH_RADIUS_M * math.radians(lat)
	return x, y


def _haversine_m(lat1, lng1, lat2, lng2):
	r1, r2 = math.radians(lat1), math.radians(lat2)
	dlat = math.radians(lat2 - lat1)
	dlng = math.radians(lng2 - lng1)
	a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlng / 2) ** 2
	return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _ring_area_sq_m(ring):
	"""Shoelace formula on a GeoJSON ring ([lng, lat] pairs), projected to
	local meters first. `ring` already includes the closing point or not —
	either works, the shoelace sum is the same."""
	if len(ring) < 3:
		return 0.0
	lat0_rad = math.radians(sum(pt[1] for pt in ring) / len(ring))
	projected = [_project_equirect(pt[1], pt[0], lat0_rad) for pt in ring]
	area = 0.0
	n = len(projected)
	i = 0
	while i < n:
		x1, y1 = projected[i]
		x2, y2 = projected[(i + 1) % n]
		area += x1 * y2 - x2 * y1
		i += 1
	return abs(area) / 2.0


def _line_length_m(points):
	total = 0.0
	i = 0
	while i < len(points) - 1:
		lng1, lat1 = points[i][0], points[i][1]
		lng2, lat2 = points[i + 1][0], points[i + 1][1]
		total += _haversine_m(lat1, lng1, lat2, lng2)
		i += 1
	return total


class SpatialFeature(Document):
	def validate(self):
		self.derive_geometry_type_and_measurements()
		self.validate_properties_json()

	def derive_geometry_type_and_measurements(self):
		geom = self._parsed_geometry()
		if not geom:
			return

		gtype = geom.get("type")
		coords = geom.get("coordinates")
		self.geometry_type = gtype
		self.area_sq_m = 0
		self.length_m = 0

		try:
			if gtype == "Polygon" and coords:
				# First ring is the outer boundary; holes (if any) aren't
				# subtracted here — good enough for Phase 1's farm-boundary
				# use case, where holes essentially never occur.
				self.area_sq_m = _ring_area_sq_m(coords[0])
			elif gtype == "MultiPolygon" and coords:
				self.area_sq_m = sum(_ring_area_sq_m(poly[0]) for poly in coords)
			elif gtype == "LineString" and coords:
				self.length_m = _line_length_m(coords)
			elif gtype == "MultiLineString" and coords:
				self.length_m = sum(_line_length_m(line) for line in coords)
			# Point/MultiPoint: no area or length, both stay 0.
		except Exception as e:
			# Malformed geometry shouldn't block saving the record — the
			# geometry_type is still useful even if measurement fails.
			frappe.log_error("SpatialFeature measurement", str(e))

	def validate_properties_json(self):
		if not self.properties:
			return
		try:
			json.loads(self.properties)
		except Exception:
			frappe.throw(_("Properties must be valid JSON."))

	def _parsed_geometry(self):
		"""Frappe's Geolocation field stores a FeatureCollection. This
		doctype only ever holds ONE feature per record, so pull out its
		single geometry object."""
		if not self.geometry:
			return None
		try:
			parsed = json.loads(self.geometry)
		except Exception:
			return None
		features = parsed.get("features") or []
		if not features:
			return None
		return features[0].get("geometry")
