"""
locations_tab.py

Predefined location lookup: maps a short code exactly as it appears in
the daily Excel file (e.g. "CPK", "BQT STORE", "DICC") to a real, precise
address Google Maps can resolve exactly.

Anything in the daily file that ISN'T in this list (a bare area name, or
a one-off customer address) still gets used as-is for the Maps lookup --
it just gets flagged internally as an approximate/area-level estimate
rather than an exact one, which the AI Review layer takes into account.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QLineEdit, QMessageBox, QHeaderView
)
from PySide6.QtCore import Qt

from app import db


class LocationsTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Predefined Locations"))

        note = QLabel(
            "Map short codes from your Excel file (e.g. \"CPK\", \"BQT STORE\") to their real, "
            "precise address. Anything in the daily file NOT listed here still gets looked up "
            "as-is -- it's just treated as a rougher, area-level travel-time estimate rather "
            "than an exact one."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(note)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Short Code (as in Excel)", "Real Address"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        form_row = QHBoxLayout()
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Short code, e.g. CPK")
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("Real address, e.g. Central Production Kitchen, Al Quoz, Dubai")
        form_row.addWidget(self.code_input)
        form_row.addWidget(self.address_input, stretch=1)
        layout.addLayout(form_row)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add / Update")
        add_btn.clicked.connect(self._on_add)
        del_btn = QPushButton("Delete Selected")
        del_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)

    def refresh(self):
        self.table.setRowCount(0)
        for loc in db.list_locations(self.conn):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(loc["short_code"]))
            self.table.setItem(row, 1, QTableWidgetItem(loc["full_address"]))

    def _on_add(self):
        code = self.code_input.text().strip()
        address = self.address_input.text().strip()
        if not code or not address:
            QMessageBox.information(self, "Missing info", "Both short code and address are required.")
            return
        db.add_location(self.conn, code, address)
        self.code_input.clear()
        self.address_input.clear()
        self.refresh()

    def _on_delete(self):
        row = self.table.currentRow()
        if row < 0:
            return
        code = self.table.item(row, 0).text()
        confirm = QMessageBox.question(self, "Delete Location", f"Delete the mapping for '{code}'?")
        if confirm != QMessageBox.Yes:
            return
        db.delete_location(self.conn, code)
        self.refresh()
