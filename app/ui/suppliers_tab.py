"""
suppliers_tab.py

Structured rate/availability offerings per supplier -- exact format,
reliably parsed. A supplier can offer several vehicle types, each with
its own rate and daily availability count. The app generates the actual
unit numbering/naming ("SUPPLIER", "SUPPLIER 1", "SAME SUPPLIER", ...)
dynamically at planning time -- the planner never pre-names units.
"""
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QInputDialog, QMessageBox, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from app import db

EXCLUDED_COLOR = QColor("#c08838")


class SuppliersTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.current_supplier_id = None
        self._build_ui()
        self.refresh_entities()

    def _build_ui(self):
        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Suppliers"))

        self.entity_list = QListWidget()
        self.entity_list.currentItemChanged.connect(self._on_entity_selected)
        self.entity_list.itemChanged.connect(self._on_checkbox_toggled)
        left_layout.addWidget(self.entity_list)

        exclude_hint = QLabel("Untick to exclude a supplier from tomorrow's planning (contract expired, unavailable, etc.).")
        exclude_hint.setWordWrap(True)
        exclude_hint.setStyleSheet("color: #666666; font-size: 11px;")
        left_layout.addWidget(exclude_hint)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Supplier")
        add_btn.clicked.connect(self._on_add)
        del_btn = QPushButton("Delete Supplier")
        del_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        left_layout.addLayout(btn_row)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.header_label = QLabel("Select a supplier")
        right_layout.addWidget(self.header_label)

        right_layout.addWidget(QLabel("Vehicle types offered (hard rules -- exact format, enforced automatically):"))
        self.offerings_table = QTableWidget(0, 3)
        self.offerings_table.setHorizontalHeaderLabels(["Vehicle Type", "Rate / Hour", "Available / Day"])
        self.offerings_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.offerings_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.offerings_table.setSelectionBehavior(QTableWidget.SelectRows)
        right_layout.addWidget(self.offerings_table)

        offering_form = QHBoxLayout()
        self.type_input = QLineEdit()
        self.type_input.setPlaceholderText("e.g. 12 Seated Bus")
        self.rate_input = QLineEdit()
        self.rate_input.setPlaceholderText("e.g. 100")
        self.availability_input = QLineEdit()
        self.availability_input.setPlaceholderText("e.g. 2")
        offering_form.addWidget(self.type_input, stretch=2)
        offering_form.addWidget(self.rate_input, stretch=1)
        offering_form.addWidget(self.availability_input, stretch=1)
        right_layout.addLayout(offering_form)

        offering_btn_row = QHBoxLayout()
        add_offering_btn = QPushButton("Add Offering")
        add_offering_btn.clicked.connect(self._on_add_offering)
        del_offering_btn = QPushButton("Delete Selected Offering")
        del_offering_btn.clicked.connect(self._on_delete_offering)
        offering_btn_row.addWidget(add_offering_btn)
        offering_btn_row.addWidget(del_offering_btn)
        right_layout.addLayout(offering_btn_row)

        self.history_label = QLabel("")
        self.history_label.setStyleSheet("color: #888888; font-size: 11px;")
        right_layout.addWidget(self.history_label)

        right_layout.addWidget(QLabel(" "))
        right_layout.addWidget(QLabel("Additional notes for AI (free text -- context only, not enforced automatically):"))
        self.notes_list = QListWidget()
        self.notes_list.itemChanged.connect(self._on_note_edited)
        right_layout.addWidget(self.notes_list)

        note_btn_row = QHBoxLayout()
        add_note_btn = QPushButton("Add Line")
        add_note_btn.clicked.connect(self._on_add_note)
        del_note_btn = QPushButton("Delete Line")
        del_note_btn.clicked.connect(self._on_delete_note)
        note_btn_row.addWidget(add_note_btn)
        note_btn_row.addWidget(del_note_btn)
        right_layout.addLayout(note_btn_row)

        splitter.addWidget(right)
        splitter.setSizes([220, 560])

    def refresh_entities(self, select_id=None):
        self.entity_list.blockSignals(True)
        self.entity_list.clear()
        rows = db.list_suppliers(self.conn)
        selected_item = None
        for position, row in enumerate(rows, start=1):
            excluded = bool(row["excluded_from_planning"])
            label = f"{position}. {row['name']}"
            if excluded:
                label += "  (not used tomorrow)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, row["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if excluded else Qt.Checked)
            if excluded:
                item.setForeground(EXCLUDED_COLOR)
            self.entity_list.addItem(item)
            if select_id is not None and row["id"] == select_id:
                selected_item = item
        self.entity_list.blockSignals(False)
        if selected_item:
            self.entity_list.setCurrentItem(selected_item)
        elif self.entity_list.count() > 0:
            self.entity_list.setCurrentRow(0)
        else:
            self.current_supplier_id = None
            self._clear_form()

    def _on_checkbox_toggled(self, item):
        supplier_id = item.data(Qt.UserRole)
        excluded = item.checkState() == Qt.Unchecked
        db.set_supplier_excluded(self.conn, supplier_id, excluded)
        self.refresh_entities(select_id=supplier_id)

    def _on_add(self):
        name, ok = QInputDialog.getText(self, "Add Supplier", "Supplier name:")
        if not ok or not name.strip():
            return
        try:
            new_id = db.add_supplier(self.conn, name.strip())
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not add supplier: {e}")
            return
        self.refresh_entities(select_id=new_id)

    def _on_delete(self):
        item = self.entity_list.currentItem()
        if not item:
            return
        supplier_id = item.data(Qt.UserRole)
        name = item.text()
        confirm = QMessageBox.question(self, "Delete Supplier", f"Delete '{name}' and all their offerings? This cannot be undone.")
        if confirm != QMessageBox.Yes:
            return
        db.delete_supplier(self.conn, supplier_id)
        self.refresh_entities()

    def _on_entity_selected(self, current, previous):
        if current is None:
            self.current_supplier_id = None
            self._clear_form()
            return
        self.current_supplier_id = current.data(Qt.UserRole)
        self.header_label.setText(f"Editing: {current.text()}")
        self._load_form(self.current_supplier_id)

    def _clear_form(self):
        self.offerings_table.setRowCount(0)
        self.notes_list.clear()
        self.history_label.setText("")

    def _load_form(self, supplier_id):
        offerings = db.get_supplier_offerings(self.conn, supplier_id)
        self.offerings_table.setRowCount(0)
        for o in offerings:
            row = self.offerings_table.rowCount()
            self.offerings_table.insertRow(row)
            self.offerings_table.setItem(row, 0, QTableWidgetItem(o["vehicle_type"]))
            self.offerings_table.setItem(row, 1, QTableWidgetItem(str(o["rate_per_hour"]) if o["rate_per_hour"] is not None else ""))
            self.offerings_table.setItem(row, 2, QTableWidgetItem(str(o["max_available_per_day"]) if o["max_available_per_day"] is not None else ""))
            self.offerings_table.item(row, 0).setData(Qt.UserRole, o["id"])

        cumulative = db.get_supplier_cumulative_hours(self.conn, supplier_id)
        self.history_label.setText(f"Cumulative hours given to this supplier historically: {cumulative:.1f}")

        rules = db.get_supplier_rules(self.conn, supplier_id)
        self.notes_list.blockSignals(True)
        self.notes_list.clear()
        for r in rules:
            item = QListWidgetItem(r["line_text"])
            item.setData(Qt.UserRole, r["id"])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self.notes_list.addItem(item)
        self.notes_list.blockSignals(False)

    def _on_add_offering(self):
        if self.current_supplier_id is None:
            QMessageBox.information(self, "No selection", "Select a supplier first.")
            return
        vtype = self.type_input.text().strip()
        try:
            rate = float(self.rate_input.text().strip())
            availability = int(self.availability_input.text().strip())
        except ValueError:
            QMessageBox.information(self, "Invalid input", "Rate and availability must be numbers.")
            return
        if not vtype:
            QMessageBox.information(self, "Missing info", "Vehicle type is required.")
            return
        db.add_supplier_offering(self.conn, self.current_supplier_id, vtype, rate, availability)
        self.type_input.clear()
        self.rate_input.clear()
        self.availability_input.clear()
        self._load_form(self.current_supplier_id)

    def _on_delete_offering(self):
        row = self.offerings_table.currentRow()
        if row < 0:
            return
        offering_id = self.offerings_table.item(row, 0).data(Qt.UserRole)
        db.delete_supplier_offering(self.conn, offering_id)
        self._load_form(self.current_supplier_id)

    def _on_add_note(self):
        if self.current_supplier_id is None:
            QMessageBox.information(self, "No selection", "Select a supplier first.")
            return
        text, ok = QInputDialog.getText(self, "Add Note", "Note for AI:")
        if not ok or not text.strip():
            return
        db.add_supplier_rule(self.conn, self.current_supplier_id, text.strip())
        self._load_form(self.current_supplier_id)

    def _on_delete_note(self):
        item = self.notes_list.currentItem()
        if not item:
            return
        db.delete_supplier_rule(self.conn, item.data(Qt.UserRole))
        self._load_form(self.current_supplier_id)

    def _on_note_edited(self, item):
        rule_id = item.data(Qt.UserRole)
        new_text = item.text().strip()
        if new_text:
            db.update_supplier_rule(self.conn, rule_id, new_text)
