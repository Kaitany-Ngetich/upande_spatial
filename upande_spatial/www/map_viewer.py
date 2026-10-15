"""Context for the Map Viewer page (www/map-viewer.html - Frappe finds this
module by the page name with hyphens as underscores)."""

no_cache = 1


def get_context(context):
	context.title = "Map Viewer"
	context.full_width = 1
	context.no_header = 1
	context.metatags = {
		"title": "Map Viewer",
		"description": "Full-bleed web-GIS dashboard for Upande Spatial - layers, query, draw/edit, import/export, spatial analysis and sharing.",
	}
	return context
