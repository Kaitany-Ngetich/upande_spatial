"""Login and Publish dialogs."""

from qgis.core import QgsMapLayerProxyModel, QgsSettings
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
	QCheckBox,
	QComboBox,
	QDialog,
	QDialogButtonBox,
	QFormLayout,
	QInputDialog,
	QLabel,
	QLineEdit,
	QMessageBox,
	QVBoxLayout,
)

from .client import ErpClient, ErpError, TwoFactorRequired

SETTINGS = "upande_spatial/"


def saved_login():
	"""(url, email, verify_ssl). Only the site and - if the user ticked
	"Remember my email" - the email are kept. Never the password."""
	s = QgsSettings()
	return (
		s.value(SETTINGS + "url", "", type=str),
		s.value(SETTINGS + "email", "", type=str),
		s.value(SETTINGS + "verify_ssl", True, type=bool),
	)


class LoginDialog(QDialog):
	"""Sign in with ERP email + password (and a 2FA code if the site asks).
	On success `self.client` is a logged-in ErpClient whose session lives
	in memory only."""

	def __init__(self, parent=None, message=None):
		super().__init__(parent)
		self.client = None
		self.setWindowTitle("Log in to ERP - Upande Spatial")
		url, email, verify = saved_login()
		form = QFormLayout()
		if message:
			note = QLabel(message)
			note.setWordWrap(True)
			form.addRow(note)
		self.url = QLineEdit(url)
		self.url.setPlaceholderText("https://yoursite.frappe.cloud")
		form.addRow("ERP site", self.url)
		self.email = QLineEdit(email)
		self.email.setPlaceholderText("you@company.com")
		form.addRow("Email", self.email)
		self.password = QLineEdit()
		self.password.setEchoMode(QLineEdit.EchoMode.Password)
		form.addRow("Password", self.password)
		self.remember = QCheckBox("Remember my email on this computer")
		self.remember.setChecked(bool(email))
		form.addRow(self.remember)
		self.verify = QCheckBox("Verify SSL certificate")
		self.verify.setChecked(verify)
		form.addRow(self.verify)
		hint = QLabel("Your password is only sent to ERP to sign in - it is never saved. You stay signed in until you log out or close QGIS.")
		hint.setWordWrap(True)
		hint.setStyleSheet("color: gray")
		form.addRow(hint)
		self.status = QLabel("")
		self.status.setWordWrap(True)
		form.addRow(self.status)

		buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
		buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Log in")
		buttons.accepted.connect(self.login)
		buttons.rejected.connect(self.reject)
		layout = QVBoxLayout(self)
		layout.addLayout(form)
		layout.addWidget(buttons)
		(self.password if email else self.url if not url else self.email).setFocus()
		self.resize(460, 300)

	def login(self):
		url = self.url.text().strip().rstrip("/")
		email = self.email.text().strip()
		password = self.password.text()
		if not (url and email and password):
			self.status.setText("Enter the site, your email and your password.")
			return
		if not url.startswith(("http://", "https://")):
			url = "https://" + url
		client = ErpClient(url, verify_ssl=self.verify.isChecked())
		try:
			try:
				client.login(email, password)
			except TwoFactorRequired as tf:
				otp, ok = QInputDialog.getText(self, "Verification code", str(tf))
				if not ok or not otp.strip():
					self.status.setText("Login cancelled - a verification code is required for this account.")
					return
				client.login_otp(tf.tmp_id, otp.strip(), email)
			info = client.ping()
		except ErpError as e:
			self.status.setText(f"<span style='color:#b3261e'>{e}</span>")
			return
		finally:
			self.password.clear()

		s = QgsSettings()
		s.setValue(SETTINGS + "url", url)
		s.setValue(SETTINGS + "verify_ssl", self.verify.isChecked())
		s.setValue(SETTINGS + "email", email if self.remember.isChecked() else "")
		s.remove(SETTINGS + "authcfg")  # left over from the API-key version
		client.user = info.get("user") or email
		self.client = client
		self.accept()


class PublishDialog(QDialog):
	"""Send any vector layer's features to ERP as new Spatial Features."""

	def __init__(self, client, catalog, iface, parent=None):
		super().__init__(parent)
		self.client = client
		self.setWindowTitle("Publish layer to ERP")
		form = QFormLayout()

		self.layer = QgsMapLayerComboBox(self)
		self.layer.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
		if iface and iface.activeLayer():
			self.layer.setLayer(iface.activeLayer())
		form.addRow("Layer", self.layer)
		self.selected_only = QCheckBox("Selected features only")
		form.addRow(self.selected_only)

		owners = catalog.get("reference_doctypes") or {}
		self.owner_type = QComboBox()
		self.owner_type.setEditable(True)
		self.owner_type.addItems([""] + list(owners))
		form.addRow("Owner type", self.owner_type)
		self.owner_name = QComboBox()
		self.owner_name.setEditable(True)
		form.addRow("Owner", self.owner_name)
		self.role = QComboBox()
		self.role.setEditable(True)
		form.addRow("Feature role", self.role)
		self.farm = QComboBox()
		self.farm.setEditable(True)
		self.farm.addItems([""] + (catalog.get("farms") or []))
		form.addRow("Farm", self.farm)
		self.title_field = QgsFieldComboBox(self)
		self.title_field.setAllowEmptyFieldName(True)
		form.addRow("Title from field", self.title_field)
		note = QLabel(
			"Every other attribute is saved as a feature property. Geometry is converted to WGS84. "
			"Leave the owner blank to publish standalone features (e.g. an analysis result)."
		)
		note.setWordWrap(True)
		form.addRow(note)

		self._owners = owners
		self.owner_type.currentTextChanged.connect(self._owner_type_changed)
		self.layer.layerChanged.connect(self.title_field.setLayer)
		self.title_field.setLayer(self.layer.currentLayer())

		buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
		buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Publish")
		buttons.accepted.connect(self.accept)
		buttons.rejected.connect(self.reject)
		layout = QVBoxLayout(self)
		layout.addLayout(form)
		layout.addWidget(buttons)
		self.resize(460, 380)

	def _owner_type_changed(self, doctype):
		self.role.clear()
		self.role.addItems([""] + (self._owners.get(doctype) or []))
		self.owner_name.clear()
		if not doctype:
			return
		try:
			rows = self.client.owner_candidates(doctype) or []
			self.owner_name.addItems([r.get("name") for r in rows])
		except Exception as e:
			QMessageBox.warning(self, "Owners", f"Could not list {doctype} records: {e}")

	def target(self):
		return {
			"reference_doctype": self.owner_type.currentText().strip() or None,
			"reference_name": self.owner_name.currentText().strip() or None,
			"feature_role": self.role.currentText().strip() or None,
			"farm": self.farm.currentText().strip() or None,
			"title_field": self.title_field.currentField() or None,
		}
