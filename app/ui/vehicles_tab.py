"""
vehicles_tab.py

In-house vehicle roster: plate, type, capacity notes, and (as of
2026-08-14) a single Active/Deactive checkbox controlling planning
eligibility -- replacing the old separate "In Workshop" and "Don't Use
Tomorrow" toggles, which did overlapping jobs.

EditVehicleDialog is the ONLY place any vehicle field is edited (Phase
28b, 2026-08-14) -- it covers every column on the vehicles table,
including the detail fields (model/year/chassis/engine/registration/
certificates/tyre/battery/picture) that used to be editable from the
Vehicle Maintenance Log window. The Maintenance Log (opened per-row via
the wrench button, see vehicle_maintenance_dialog.py) now shows those
same fields read-only, as a report, plus the service history -- "if we
want to change anything we go in vehicle tab and click edit selected,"
per the project owner's own framing.
"""
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QLineEdit, QFormLayout, QGridLayout, QMessageBox,
    QHeaderView, QDialog, QDialogButtonBox, QFileDialog
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap

from app import db
from app.ui.vehicle_maintenance_dialog import (
    VehicleMaintenanceDialog, _display_date, _is_expired, _EXPIRED_COLOR,
)

EXCLUDED_COLOR = QColor("#c08838")

COL_ACTIVE = 0
COL_MAINTENANCE = 1
COL_PLATE = 2
COL_TYPE = 3
COL_REG_EXPIRY = 4
COL_RTA_EXPIRY = 5
COL_AD_EXPIRY = 6
COL_NOTES = 7


def _parse_iso_date_or_none(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return "INVALID"


class EditVehicleDialog(QDialog):
    """Covers every vehicles-table column -- the only place any vehicle
    field is edited (Phase 28b). vehicle_row: a sqlite3.Row from
    db.get_vehicle(), or None when adding a brand-new vehicle (Plate/Type
    still required up front via VehiclesTab's own quick-add row in that
    case, so vehicle_row is never actually None in practice today, but
    the dialog itself doesn't assume that)."""

    def __init__(self, vehicle_row, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Vehicle")
        self._pending_picture_bytes = None
        self.resize(640, 560)

        root = QVBoxLayout(self)
        grid = QGridLayout()
        left = QFormLayout()
        right = QFormLayout()

        self.plate_input = QLineEdit(vehicle_row["plate"])
        self.type_input = QLineEdit(vehicle_row["vehicle_type"])
        self.notes_input = QLineEdit(vehicle_row["capacity_notes"] or "")
        self.model_input = QLineEdit(vehicle_row["vehicle_model"] or "")
        self.year_input = QLineEdit(str(vehicle_row["vehicle_year"]) if vehicle_row["vehicle_year"] else "")
        self.chassis_input = QLineEdit(vehicle_row["vehicle_chassis"] or "")
        self.engine_input = QLineEdit(vehicle_row["vehicle_engine"] or "")
        self.registration_input = QLineEdit(vehicle_row["vehicle_registration"] or "")
        self.reg_expiry_input = QLineEdit(vehicle_row["vehicle_reg_expiry"] or "")
        self.reg_expiry_input.setPlaceholderText("YYYY-MM-DD")

        left.addRow("Plate:", self.plate_input)
        left.addRow("Type:", self.type_input)
        left.addRow("Capacity / Notes:", self.notes_input)
        left.addRow("Model:", self.model_input)
        left.addRow("Year:", self.year_input)
        left.addRow("Chassis / VIN #:", self.chassis_input)
        left.addRow("Engine #:", self.engine_input)
        left.addRow("Registration #:", self.registration_input)
        left.addRow("Reg. Expiry:", self.reg_expiry_input)

        self.tyre_size_input = QLineEdit(vehicle_row["tyre_size"] or "")
        self.battery_type_input = QLineEdit(vehicle_row["battery_type"] or "")
        self.rta_cert_input = QLineEdit(vehicle_row["rta_certificate"] or "")
        self.rta_cert_expiry_input = QLineEdit(vehicle_row["rta_certificate_expiry"] or "")
        self.rta_cert_expiry_input.setPlaceholderText("YYYY-MM-DD")
        self.ad_cert_input = QLineEdit(vehicle_row["ad_certificate"] or "")
        self.ad_cert_expiry_input = QLineEdit(vehicle_row["ad_certificate_expiry"] or "")
        self.ad_cert_expiry_input.setPlaceholderText("YYYY-MM-DD")

        right.addRow("Tyre Size:", self.tyre_size_input)
        right.addRow("Battery Type:", self.battery_type_input)
        right.addRow("RTA Certificate #:", self.rta_cert_input)
        right.addRow("RTA Certificate Expiry:", self.rta_cert_expiry_input)
        right.addRow("Ad. Certificate #:", self.ad_cert_input)
        right.addRow("Ad. Certificate Expiry:", self.ad_cert_expiry_input)

        picture_col = QVBoxLayout()
        self.picture_label = QLabel()
        self.picture_label.setFixedSize(140, 100)
        self.picture_label.setAlignment(Qt.AlignCenter)
        self.picture_label.setStyleSheet("border: 1px solid #999999; border-radius: 6px;")
        if vehicle_row["vehicle_picture"]:
            pixmap = QPixmap()
            pixmap.loadFromData(vehicle_row["vehicle_picture"])
            if not pixmap.isNull():
                self.picture_label.setPixmap(pixmap.scaled(140, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.picture_label.setText("No picture")
        picture_col.addWidget(self.picture_label)
        change_pic_btn = QPushButton("Change Picture...")
        change_pic_btn.clicked.connect(self._on_change_picture)
        picture_col.addWidget(change_pic_btn)
        picture_col.addStretch(1)
        right.addRow("Picture:", picture_col)

        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        root.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_change_picture(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Vehicle Picture", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        with open(path, "rb") as f:
            self._pending_picture_bytes = f.read()
        pixmap = QPixmap(path)
        self.picture_label.setPixmap(pixmap.scaled(140, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _on_accept(self):
        if not self.plate_input.text().strip() or not self.type_input.text().strip():
            QMessageBox.information(self, "Missing info", "Plate and Type are required.")
            return
        for label, field in [
            ("Reg. Expiry", self.reg_expiry_input),
            ("RTA Certificate Expiry", self.rta_cert_expiry_input),
            ("Ad. Certificate Expiry", self.ad_cert_expiry_input),
        ]:
            if _parse_iso_date_or_none(field.text()) == "INVALID":
                QMessageBox.warning(self, "Invalid date", f"{label} must be in YYYY-MM-DD format (or left blank).")
                return
        self.accept()

    def basic_values(self):
        """(plate, vehicle_type, capacity_notes) -- for db.update_vehicle()."""
        return self.plate_input.text().strip(), self.type_input.text().strip(), self.notes_input.text().strip()

    def maintenance_field_values(self):
        """kwargs for db.update_vehicle_maintenance_fields()."""
        year_text = self.year_input.text().strip()
        return dict(
            vehicle_picture=self._pending_picture_bytes,
            vehicle_model=self.model_input.text(),
            vehicle_year=int(year_text) if year_text.isdigit() else None,
            vehicle_chassis=self.chassis_input.text(),
            vehicle_engine=self.engine_input.text(),
            vehicle_registration=self.registration_input.text(),
            vehicle_reg_expiry=self.reg_expiry_input.text().strip() or None,
            tyre_size=self.tyre_size_input.text(),
            battery_type=self.battery_type_input.text(),
            capacity_notes=self.notes_input.text(),
            rta_certificate=self.rta_cert_input.text(),
            rta_certificate_expiry=self.rta_cert_expiry_input.text().strip() or None,
            ad_certificate=self.ad_cert_input.text(),
            ad_certificate_expiry=self.ad_cert_expiry_input.text().strip() or None,
        )


class VehiclesTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("In-house Vehicles"))

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Active", "", "Plate", "Type", "Reg. Expiry",
            "RTA Cert. Expiry", "Ad. Cert. Expiry", "Capacity / Notes",
        ])
        self.table.horizontalHeader().setSectionResizeMode(COL_ACTIVE, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(COL_MAINTENANCE, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(COL_TYPE, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_NOTES, QHeaderView.Stretch)
        self.table.setColumnWidth(COL_ACTIVE, 50)
        self.table.setColumnWidth(COL_MAINTENANCE, 36)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)

        exclude_hint = QLabel(
            "Untick \"Active\" to exclude a vehicle from planning -- workshop, parked at an event "
            "site, or any other reason it's unavailable tomorrow. Excluded vehicles turn orange and "
            "drop to the bottom as a reminder. Click the wrench icon to open a vehicle's Maintenance "
            "Log (model/year/certificates/service history)."
        )
        exclude_hint.setWordWrap(True)
        exclude_hint.setStyleSheet("color: #666666; font-size: 11px;")
        layout.addWidget(exclude_hint)

        form_row = QHBoxLayout()
        self.plate_input = QLineEdit()
        self.plate_input.setPlaceholderText("Plate, e.g. I 72610")
        self.type_input = QLineEdit()
        self.type_input.setPlaceholderText("Type, e.g. 5 Ton Chiller Truck (with lift)")
        self.notes_input = QLineEdit()
        self.notes_input.setPlaceholderText("Capacity / notes (optional)")
        form_row.addWidget(self.plate_input)
        form_row.addWidget(self.type_input)
        form_row.addWidget(self.notes_input)
        layout.addLayout(form_row)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Vehicle")
        add_btn.clicked.connect(self._on_add)
        edit_btn = QPushButton("Edit Selected")
        edit_btn.clicked.connect(self._on_edit)
        del_btn = QPushButton("Delete Selected")
        del_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)

    def refresh(self):
        vehicles = db.list_vehicles(self.conn)  # already sorted: excluded last
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        icon_path = Path(__file__).with_name("maintenance_log_button.png")
        wrench_icon = QIcon(str(icon_path)) if icon_path.exists() else QIcon()
        for v in vehicles:
            row = self.table.rowCount()
            self.table.insertRow(row)
            excluded = bool(v["excluded_from_planning"])

            active_item = QTableWidgetItem()
            active_item.setFlags(active_item.flags() | Qt.ItemIsUserCheckable)
            active_item.setFlags(active_item.flags() & ~Qt.ItemIsEditable)
            active_item.setCheckState(Qt.Unchecked if excluded else Qt.Checked)
            active_item.setData(Qt.UserRole, v["id"])
            active_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, COL_ACTIVE, active_item)

            maint_btn = QPushButton()
            maint_btn.setIcon(wrench_icon)
            maint_btn.setFixedSize(28, 24)
            maint_btn.setToolTip("Vehicle Maintenance Log")
            maint_btn.clicked.connect(lambda checked=False, vid=v["id"]: self._open_maintenance_log(vid))
            self.table.setCellWidget(row, COL_MAINTENANCE, maint_btn)

            self.table.setItem(row, COL_PLATE, QTableWidgetItem(v["plate"]))
            self.table.setItem(row, COL_TYPE, QTableWidgetItem(v["vehicle_type"]))
            self.table.setItem(row, COL_REG_EXPIRY, QTableWidgetItem(_display_date(v["vehicle_reg_expiry"])))
            self.table.setItem(row, COL_RTA_EXPIRY, QTableWidgetItem(_display_date(v["rta_certificate_expiry"])))
            self.table.setItem(row, COL_AD_EXPIRY, QTableWidgetItem(_display_date(v["ad_certificate_expiry"])))
            self.table.setItem(row, COL_NOTES, QTableWidgetItem(v["capacity_notes"] or ""))
            if excluded:
                for col in (COL_ACTIVE, COL_PLATE, COL_TYPE, COL_REG_EXPIRY,
                            COL_RTA_EXPIRY, COL_AD_EXPIRY, COL_NOTES):
                    self.table.item(row, col).setForeground(EXCLUDED_COLOR)
            else:
                # Excluded rows are already fully orange (the existing,
                # higher-priority "don't plan this" signal) -- expired-date
                # red only applies to active rows, where it's actually
                # relevant to what's being scheduled tomorrow.
                for col, iso in (
                    (COL_REG_EXPIRY, v["vehicle_reg_expiry"]),
                    (COL_RTA_EXPIRY, v["rta_certificate_expiry"]),
                    (COL_AD_EXPIRY, v["ad_certificate_expiry"]),
                ):
                    if _is_expired(iso):
                        self.table.item(row, col).setForeground(QColor(_EXPIRED_COLOR))
        self.table.blockSignals(False)

    def _selected_vehicle_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, COL_ACTIVE)
        return item.data(Qt.UserRole) if item else None

    def _on_item_changed(self, item):
        if item.column() != COL_ACTIVE:
            return
        vehicle_id = item.data(Qt.UserRole)
        if vehicle_id is None:
            return
        active = item.checkState() == Qt.Checked
        db.set_vehicle_excluded(self.conn, vehicle_id, excluded=not active)
        self.refresh()

    def _open_maintenance_log(self, vehicle_id):
        dialog = VehicleMaintenanceDialog(self.conn, vehicle_id, self)
        dialog.exec()
        self.refresh()

    def _on_add(self):
        plate = self.plate_input.text().strip()
        vtype = self.type_input.text().strip()
        notes = self.notes_input.text().strip()
        if not plate or not vtype:
            QMessageBox.information(self, "Missing info", "Plate and Type are required.")
            return
        try:
            db.add_vehicle(self.conn, plate, vtype, notes)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not add vehicle: {e}")
            return
        self.plate_input.clear()
        self.type_input.clear()
        self.notes_input.clear()
        self.refresh()

    def _on_delete(self):
        vid = self._selected_vehicle_id()
        if vid is None:
            return
        confirm = QMessageBox.question(self, "Delete Vehicle", "Delete this vehicle?")
        if confirm != QMessageBox.Yes:
            return
        db.delete_vehicle(self.conn, vid)
        self.refresh()

    def _on_edit(self):
        vid = self._selected_vehicle_id()
        if vid is None:
            QMessageBox.information(self, "No selection", "Select a vehicle first.")
            return
        vehicle_row = db.get_vehicle(self.conn, vid)
        if vehicle_row is None:
            QMessageBox.warning(self, "Not found", "This vehicle no longer exists.")
            self.refresh()
            return

        dialog = EditVehicleDialog(vehicle_row, self)
        if dialog.exec() != QDialog.Accepted:
            return
        plate, vtype, notes = dialog.basic_values()
        try:
            db.update_vehicle(self.conn, vid, plate, vtype, notes)
            db.update_vehicle_maintenance_fields(self.conn, vid, **dialog.maintenance_field_values())
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not update vehicle: {e}")
            return
        self.refresh()
