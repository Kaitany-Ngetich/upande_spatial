app_name = "upande_spatial"
app_title = "Upande Spatial"
app_publisher = "dev@upande.com"
app_description = "Dedicated application for spatial data"
app_email = "dev@upande.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "upande_spatial",
# 		"logo": "/assets/upande_spatial/logo.png",
# 		"title": "Upande Spatial",
# 		"route": "/upande_spatial",
# 		"has_permission": "upande_spatial.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/upande_spatial/css/upande_spatial.css"
# app_include_js = "/assets/upande_spatial/js/upande_spatial.js"

# include js, css files in header of web template
# web_include_css = "/assets/upande_spatial/css/upande_spatial.css"
# web_include_js = "/assets/upande_spatial/js/upande_spatial.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "upande_spatial/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "upande_spatial/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "upande_spatial.utils.jinja_methods",
# 	"filters": "upande_spatial.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "upande_spatial.install.before_install"
# after_install = "upande_spatial.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "upande_spatial.uninstall.before_uninstall"
# after_uninstall = "upande_spatial.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "upande_spatial.utils.before_app_install"
# after_app_install = "upande_spatial.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "upande_spatial.utils.before_app_uninstall"
# after_app_uninstall = "upande_spatial.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "upande_spatial.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "upande_spatial.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"upande_spatial.tasks.all"
# 	],
# 	"daily": [
# 		"upande_spatial.tasks.daily"
# 	],
# 	"hourly": [
# 		"upande_spatial.tasks.hourly"
# 	],
# 	"weekly": [
# 		"upande_spatial.tasks.weekly"
# 	],
# 	"monthly": [
# 		"upande_spatial.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "upande_spatial.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "upande_spatial.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "upande_spatial.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "upande_spatial.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["upande_spatial.utils.before_request"]
# after_request = ["upande_spatial.utils.after_request"]

# Job Events
# ----------
# before_job = ["upande_spatial.utils.before_job"]
# after_job = ["upande_spatial.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"upande_spatial.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Fixtures
# --------
# Data-level records this app depends on that must survive a fresh deploy -
# same lesson learned the hard way on upande_security: a Workspace (or any
# data record) that only exists in this bench's database, with nothing
# exported/fixture-tracked, silently vanishes on a fresh site install.
fixtures = [
	{
		# The workspace shell (left-sidebar entry in Desk). Also exported as
		# its own module JSON (upande_spatial/workspace/upande_spatial/) since
		# developer_mode picked it up automatically - this fixture is
		# deliberate belt-and-suspenders, not the only thing keeping it alive.
		"dt": "Workspace",
		"filters": [
			["name", "=", "Upande Spatial"],
		],
	},
	{
		# Which (geometry type, feature role) combinations are valid per
		# doctype - Farm/Warehouse/Guard Device Token configs set up so far.
		# New doctypes get added here as more modules adopt Spatial Feature;
		# a doctype with no config row here is simply unrestricted, so this
		# list only ever needs entries for doctypes that ARE configured.
		"dt": "Spatial Entity Config",
	},
]

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

