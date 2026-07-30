"""
vehicles_tab.py

In-house vehicle roster: plate, type, capacity notes, and a workshop
in/out toggle so the planner can mark a vehicle unavailable on days it's
being serviced.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QLineEdit, QFormLayout, QMessageBox, QHeaderView,
    QCheckBox, QDialog, QDialogButtonBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from app import db

EXCLUDED_COLOR = QColor("#c08838")


class EditVehicleDialog(QDialog):
    def __init__(self, plate, vehicle_type, notes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Vehicle")
        layout = QFormLayout(self)

        self.plate_input = QLineEdit(plate)
        self.type_input = QLineEdit(vehicle_type)
        self.notes_input = QLineEdit(notes)

        layout.addRow("Plate:", self.plate_input)
        layout.addRow("Type:", self.type_input)
        layout.addRow("Capacity / Notes:", self.notes_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self):
        return self.plate_input.text().strip(), self.type_input.text().strip(), self.notes_input.text().strip()


class VehiclesTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("In-house Vehicles"))

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Plate", "Type", "Capacity / Notes", "In Workshop", "Don't Use Tomorrow"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        exclude_hint = QLabel(
            "Untick \"Don't Use Tomorrow\" is off by default; check it to exclude a vehicle from "
            "planning for reasons other than the workshop -- e.g. parked at an event site serving "
            "as temporary storage. Excluded vehicles turn orange and drop to the bottom as a reminder."
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
        toggle_btn = QPushButton("Toggle Workshop Status")
        toggle_btn.clicked.connect(self._on_toggle_workshop)
        toggle_exclude_btn = QPushButton("Toggle Don't Use Tomorrow")
        toggle_exclude_btn.clicked.connect(self._on_toggle_excluded)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(del_btn)
        btn_row.addWidget(toggle_btn)
        btn_row.addWidget(toggle_exclude_btn)
        layout.addLayout(btn_row)

    def refresh(self):
        vehicles = db.list_vehicles(self.conn)  # already sorted: excluded/workshop last
        self.table.setRowCount(0)
        for v in vehicles:
            row = self.table.rowCount()
            self.table.insertRow(row)
            excluded = bool(v["excluded_from_planning"])
            self.table.setItem(row, 0, QTableWidgetItem(v["plate"]))
            self.table.setItem(row, 1, QTableWidgetItem(v["vehicle_type"]))
            self.table.setItem(row, 2, QTableWidgetItem(v["capacity_notes"] or ""))
            self.table.setItem(row, 3, QTableWidgetItem("Yes" if v["in_workshop"] else "No"))
            self.table.setItem(row, 4, QTableWidgetItem("Yes" if excluded else "No"))
            if excluded:
                for col in range(5):
                    self.table.item(row, col).setForeground(EXCLUDED_COLOR)
            self.table.item(row, 0).setData(Qt.UserRole, v["id"])

    def _selected_vehicle_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(Qt.UserRole)

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
        row = self.table.currentRow()
        current_plate = self.table.item(row, 0).text()
        current_type = self.table.item(row, 1).text()
        current_notes = self.table.item(row, 2).text()

        dialog = EditVehicleDialog(current_plate, current_type, current_notes, self)
        if dialog.exec() != QDialog.Accepted:
            return
        plate, vtype, notes = dialog.values()
        if not plate or not vtype:
            QMessageBox.information(self, "Missing info", "Plate and Type are required.")
            return
        try:
            db.update_vehicle(self.conn, vid, plate, vtype, notes)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not update vehicle: {e}")
            return
        self.refresh()

    def _on_toggle_workshop(self):
        vid = self._selected_vehicle_id()
        if vid is None:
            return
        row = self.table.currentRow()
        currently_in = self.table.item(row, 3).text() == "Yes"
        db.set_vehicle_workshop_status(self.conn, vid, not currently_in)
        self.refresh()

    def _on_toggle_excluded(self):
        vid = self._selected_vehicle_id()
        if vid is None:
            return
        row = self.table.currentRow()
        currently_excluded = self.table.item(row, 4).text() == "Yes"
        db.set_vehicle_excluded(self.conn, vid, not currently_excluded)
        self.refresh()
