"""Thin client for the Upande Spatial ERP API.

Plain Python (urllib, no QGIS imports) so it can be tested outside QGIS.

Users sign in with their own ERP email and password, exactly like the ERP
web login (including two-factor codes when the site has 2FA on). Only the
resulting session cookie is kept, in memory - the password is never stored
anywhere, and logging out (or closing QGIS) ends the ERP session. So the
plugin only ever sees and changes what that ERP user may, for as long as
their session lasts.

An API key/secret is also accepted, for scripts and automated tests."""

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

QGIS_API = "upande_spatial.api.qgis."
SPATIAL_API = "upande_spatial.api.spatial."
EPANET_API = "upande_spatial.api.epanet."


class ErpError(Exception):
	pass


class SessionExpired(ErpError):
	"""Not (or no longer) logged in - the UI should ask to log in again."""


class TwoFactorRequired(ErpError):
	"""Password accepted; ERP now wants the one-time code (see login_otp)."""

	def __init__(self, message, tmp_id, method):
		super().__init__(message)
		self.tmp_id = tmp_id
		self.method = method  # "OTP App", "SMS" or "Email"


def _server_message(body):
	"""Frappe's readable error text, out of its JSON error envelope."""
	try:
		data = json.loads(body)
	except Exception:
		return body[:300]
	for key in ("_server_messages",):
		if data.get(key):
			try:
				msgs = [json.loads(m).get("message", m) for m in json.loads(data[key])]
				return "; ".join(str(m) for m in msgs)
			except Exception:
				pass
	if data.get("exception"):
		return str(data["exception"]).split(":", 1)[-1].strip()
	return data.get("message") or body[:300]


class ErpClient:
	def __init__(self, url, api_key=None, api_secret=None, timeout=60, verify_ssl=True):
		self.url = (url or "").rstrip("/")
		self.auth = f"token {api_key}:{api_secret}" if api_key and api_secret else None
		self.timeout = timeout
		self.user = None
		self.cookies = http.cookiejar.CookieJar()  # in memory only, never written to disk
		handlers = [urllib.request.HTTPCookieProcessor(self.cookies)]
		if not verify_ssl:
			handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
		self._opener = urllib.request.build_opener(*handlers)

	# ── session ────────────────────────────────────────────────
	@property
	def logged_in(self):
		return bool(self.auth) or any(c.name == "sid" and c.value not in ("", "Guest") for c in self.cookies)

	def login(self, email, password):
		"""ERP's own login. Raises TwoFactorRequired when the site asks for a
		one-time code - finish with login_otp(). The password is only sent,
		never kept."""
		try:
			res = self._request("login", {"usr": email, "pwd": password})
		except SessionExpired as e:  # a refused login is a login error, not an expiry
			raise ErpError(str(e) or "Invalid login credentials") from None
		return self._after_login(res, email)

	def login_otp(self, tmp_id, otp, email=None):
		try:
			res = self._request("login", {"tmp_id": tmp_id, "otp": otp, "cmd": "login"})
		except SessionExpired as e:
			raise ErpError(str(e) or "Incorrect verification code") from None
		return self._after_login(res, email)

	def _after_login(self, res, email):
		if res.get("verification") and res.get("tmp_id"):
			v = res["verification"]
			method = v.get("method") or "OTP App"
			prompt = v.get("prompt") or f"Enter the verification code from your {method}."
			raise TwoFactorRequired(prompt, res["tmp_id"], method)
		self.user = email
		return res

	def logout(self):
		if not self.auth and self.logged_in:
			try:
				self._request("logout", {})
			except ErpError:
				pass
		self.cookies.clear()
		self.user = None

	# ── transport ──────────────────────────────────────────────
	def call(self, method, **params):
		"""POST a whitelisted method. Dict/list params are JSON-encoded, the
		way Frappe expects form args; None params are left out."""
		data = {}
		for k, v in params.items():
			if v is None:
				continue
			data[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
		return self._request(method, data).get("message")

	def _headers(self):
		h = {"Accept": "application/json"}
		if self.auth:
			h["Authorization"] = self.auth
		return h

	def _request(self, method, data):
		req = urllib.request.Request(
			f"{self.url}/api/method/{method}",
			data=urllib.parse.urlencode(data).encode(),
			headers=self._headers(),
			method="POST",
		)
		body = self._open(req).decode("utf-8", "replace")
		try:
			return json.loads(body)
		except ValueError:
			raise ErpError(f"Unexpected response from {self.url}: {body[:200]}") from None

	def _open(self, req):
		try:
			with self._opener.open(req, timeout=self.timeout) as resp:
				return resp.read()
		except urllib.error.HTTPError as e:
			body = e.read().decode("utf-8", "replace")
			message = _server_message(body)
			if e.code in (401, 403) and (
				not self.logged_in or "session" in body.lower() or "login to access" in body.lower()
				or "AuthenticationError" in body or "SessionExpired" in body
			):
				if "login to access" in body.lower() or "not whitelisted" in body.lower():
					message = "Not logged in to ERP (or your session has expired) - please log in."
				raise SessionExpired(message or "Please log in to ERP.") from None
			raise ErpError(f"{e.code}: {message}") from None
		except urllib.error.URLError as e:
			raise ErpError(f"Cannot reach {self.url}: {e.reason}") from None

	def download(self, method, **params):
		"""GET a method that answers with a file (e.g. export_inp) -> bytes."""
		query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
		req = urllib.request.Request(f"{self.url}/api/method/{method}?{query}", headers=self._headers())
		return self._open(req)

	# Convenience wrappers - one per server endpoint the plugin uses.
	def ping(self):
		return self.call(QGIS_API + "ping")

	def catalog(self, farm=None):
		return self.call(QGIS_API + "catalog", farm=farm)

	def get_layer(self, layer, geometry_type=None, farm=None):
		return self.call(QGIS_API + "get_layer", layer=layer, geometry_type=geometry_type, farm=farm)

	def apply_changes(self, changes, force=False):
		return self.call(QGIS_API + "apply_changes", changes=changes, force=1 if force else 0)

	def epanet_results(self, network):
		return self.call(QGIS_API + "epanet_results", network=network)

	def export_inp(self, network):
		return self.download(EPANET_API + "export_inp", network=network)

	def checkout(self, name):
		return self.call(SPATIAL_API + "checkout_feature", name=name)

	def release(self, name):
		return self.call(SPATIAL_API + "release_feature", name=name)

	def owner_candidates(self, doctype):
		return self.call(SPATIAL_API + "list_owner_candidates", doctype=doctype)
