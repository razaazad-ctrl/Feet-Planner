"""
drivers_tab.py

Structured hard-rule fields for drivers -- exact format, always parsed
the same way, always enforced. This replaces trying to detect rules from
free-typed lines (which silently failed when the planner's phrasing
didn't match an exact regex -- the root cause of drivers exceeding their
hour cap without the engine catching it).

Free-text lines are still available below, for anything that's genuinely
just context for the AI's judgment (not a hard constraint).
"""
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QInputDialog, QMessageBox, QSplitter,
    QFormLayout, QComboBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from app import db

EXCLUDED_COLOR = QColor("#c08838")


class DriversTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.current_driver_id = None
        self._build_ui()
        self.refresh_entities()

    def _build_ui(self):
        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Drivers"))

        self.entity_list = QListWidget()
        self.entity_list.currentItemChanged.connect(self._on_entity_selected)
        self.entity_list.itemChanged.connect(self._on_checkbox_toggled)
        left_layout.addWidget(self.entity_list)

        exclude_hint = QLabel("Untick to exclude a driver from tomorrow's planning (sick day, vacation, etc.) -- turns orange and drops to the bottom as a reminder.")
        exclude_hint.setWordWrap(True)
        exclude_hint.setStyleSheet("color: #666666; font-size: 11px;")
        left_layout.addWidget(exclude_hint)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Driver")
        add_btn.clicked.connect(self._on_add)
        del_btn = QPushButton("Delete Driver")
        del_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        left_layout.addLayout(btn_row)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.header_label = QLabel("Select a driver")
        right_layout.addWidget(self.header_label)

        right_layout.addWidget(QLabel("Hard rules (exact format, enforced automatically):"))
        form = QFormLayout()

        self.working_hours_input = QLineEdit()
        self.working_hours_input.setPlaceholderText("e.g. 9  (normal full day)")
        form.addRow("Working hours per day:", self.working_hours_input)

        self.max_working_hours_input = QLineEdit()
        self.max_working_hours_input.setPlaceholderText("e.g. 12  (hard daily ceiling incl. overtime; blank = same as working hours, i.e. no overtime)")
        form.addRow("Max working hours per day:", self.max_working_hours_input)

        # HR-002 rework: the planner just marks the driver morning or
        # evening -- not an exact clock time. The actual first-job time
        # for a given day comes out of the plan itself and is reported to
        # the driver afterward, never fixed in advance.
        self.shift_period_input = QComboBox()
        self.shift_period_input.addItem("(No restriction)", None)
        self.shift_period_input.addItem("Morning (before 12:00)", "morning")
        self.shift_period_input.addItem("Evening (12:00 onward)", "evening")
        form.addRow("Shift:", self.shift_period_input)

        self.off_days_input = QLineEdit()
        self.off_days_input.setPlaceholderText("e.g. friday  (or friday,saturday)")
        form.addRow("Off day(s):", self.off_days_input)

        self.max_overtime_input = QLineEdit()
        self.max_overtime_input.setPlaceholderText("e.g. 20  (leave blank = unlimited overtime)")
        form.addRow("Max overtime hours / month:", self.max_overtime_input)

        self.monthly_target_input = QLineEdit()
        self.monthly_target_input.setPlaceholderText("e.g. 208  (mainly for temp drivers)")
        form.addRow("Total hours / month target:", self.monthly_target_input)

        self.license_types_input = QLineEdit()
        self.license_types_input.setPlaceholderText("e.g. 5 Ton Chiller Truck (with lift), Driver Only")
        form.addRow("License types (vehicle types qualified for):", self.license_types_input)

        right_layout.addLayout(form)

        save_btn = QPushButton("Save Hard Rules")
        save_btn.clicked.connect(self._on_save_hard_rules)
        right_layout.addWidget(save_btn)

        self.month_hours_label = QLabel("")
        self.month_hours_label.setStyleSheet("color: #888888; font-size: 11px;")
        right_layout.addWidget(self.month_hours_label)

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
        rows = db.list_drivers(self.conn)
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
            self.current_driver_id = None
            self._clear_form()

    def _on_checkbox_toggled(self, item):
        driver_id = item.data(Qt.UserRole)
        excluded = item.checkState() == Qt.Unchecked
        db.set_driver_excluded(self.conn, driver_id, excluded)
        self.refresh_entities(select_id=driver_id)

    def _on_add(self):
        name, ok = QInputDialog.getText(self, "Add Driver", "Driver name:")
        if not ok or not name.strip():
            return
        try:
            new_id = db.add_driver(self.conn, name.strip())
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not add driver: {e}")
            return
        self.refresh_entities(select_id=new_id)

    def _on_delete(self):
        item = self.entity_list.currentItem()
        if not item:
            return
        driver_id = item.data(Qt.UserRole)
        name = item.text()
        confirm = QMessageBox.question(self, "Delete Driver", f"Delete '{name}' and all their rules? This cannot be undone.")
        if confirm != QMessageBox.Yes:
            return
        db.delete_driver(self.conn, driver_id)
        self.refresh_entities()

    def _on_entity_selected(self, current, previous):
        if current is None:
            self.current_driver_id = None
            self._clear_form()
            return
        self.current_driver_id = current.data(Qt.UserRole)
        self.header_label.setText(f"Editing: {current.text()}")
        self._load_form(self.current_driver_id)

    def _clear_form(self):
        for field in [self.working_hours_input, self.max_working_hours_input, self.off_days_input,
                      self.max_overtime_input, self.monthly_target_input, self.license_types_input]:
            field.clear()
        self.shift_period_input.setCurrentIndex(0)
        self.notes_list.clear()
        self.month_hours_label.setText("")

    def _load_form(self, driver_id):
        row = self.conn.execute("SELECT * FROM drivers WHERE id = ?", (driver_id,)).fetchone()
        self.working_hours_input.setText(str(row["working_hours_per_day"]) if row["working_hours_per_day"] is not None else "")
        self.max_working_hours_input.setText(str(row["max_working_hours_per_day"]) if row["max_working_hours_per_day"] is not None else "")
        shift_period_index = self.shift_period_input.findData(row["shift_period"])
        self.shift_period_input.setCurrentIndex(shift_period_index if shift_period_index >= 0 else 0)
        self.off_days_input.setText(row["off_days"] or "")
        self.max_overtime_input.setText(str(row["max_overtime_hours_per_month"]) if row["max_overtime_hours_per_month"] is not None else "")
        self.monthly_target_input.setText(str(row["total_hours_per_month_target"]) if row["total_hours_per_month_target"] is not None else "")
        self.license_types_input.setText(row["license_types"] or "")

        from datetime import date
        today = date.today()
        month_hours = db.get_driver_month_to_date_hours(self.conn, driver_id, today.year, today.month)
        self.month_hours_label.setText(f"Hours logged this month so far (from finalized days): {month_hours:.1f}")

        rules = db.get_driver_rules(self.conn, driver_id)
        self.notes_list.blockSignals(True)
        self.notes_list.clear()
        for r in rules:
            item = QListWidgetItem(r["line_text"])
            item.setData(Qt.UserRole, r["id"])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self.notes_list.addItem(item)
        self.notes_list.blockSignals(False)

    def _parse_float_or_none(self, text):
        text = text.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _on_save_hard_rules(self):
        if self.current_driver_id is None:
            return
        working_hours = self._parse_float_or_none(self.working_hours_input.text())
        max_working_hours = self._parse_float_or_none(self.max_working_hours_input.text())
        max_overtime = self._parse_float_or_none(self.max_overtime_input.text())
        monthly_target = self._parse_float_or_none(self.monthly_target_input.text())
        off_days = [d.strip().lower() for d in self.off_days_input.text().split(",") if d.strip()]
        license_types = [t.strip() for t in self.license_types_input.text().split(",") if t.strip()]

        db.set_driver_hard_rules(
            self.conn, self.current_driver_id,
            working_hours_per_day=working_hours,
            shift_period=self.shift_period_input.currentData(),
            off_days=off_days,
            max_overtime_hours_per_month=max_overtime,
            total_hours_per_month_target=monthly_target,
            license_types=license_types,
            max_working_hours_per_day=max_working_hours,
        )
        QMessageBox.information(self, "Saved", "Hard rules saved.")

    def _on_add_note(self):
        if self.current_driver_id is None:
            QMessageBox.information(self, "No selection", "Select a driver first.")
            return
        text, ok = QInputDialog.getText(self, "Add Note", "Note for AI:")
        if not ok or not text.strip():
            return
        db.add_driver_rule(self.conn, self.current_driver_id, text.strip())
        self._load_form(self.current_driver_id)

    def _on_delete_note(self):
        item = self.notes_list.currentItem()
        if not item:
            return
        db.delete_driver_rule(self.conn, item.data(Qt.UserRole))
        self._load_form(self.current_driver_id)

    def _on_note_edited(self, item):
        rule_id = item.data(Qt.UserRole)
        new_text = item.text().strip()
        if new_text:
            db.update_driver_rule(self.conn, rule_id, new_text)
