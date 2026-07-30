"""
entity_rules_widget.py

Reusable two-pane widget: a list of entities (drivers or suppliers) on the
left with Add/Delete buttons, and on the right, that entity's rule lines --
plain lines of text, one rule per line, with Add line / Delete line / Save.

Used for both the Drivers tab and the Suppliers tab so the two stay
visually and behaviorally consistent.
"""
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QInputDialog, QMessageBox, QSplitter,
    QAbstractItemView
)
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from app.rules_parser import describe_rule_type

EXCLUDED_COLOR = QColor("#c08838")


class EntityRulesWidget(QWidget):
    """
    entity_label: e.g. "Driver" / "Supplier" -- used in dialog/button text
    list_fn(conn) -> rows with .id, .name, .excluded_from_planning
    add_fn(conn, name) -> new id
    delete_fn(conn, entity_id)
    exclude_fn(conn, entity_id, excluded) -> toggles the "don't use tomorrow" flag
    get_rules_fn(conn, entity_id) -> rows with .id, .line_text, .rule_type
    add_rule_fn(conn, entity_id, text) -> (rule_id, rule_type, parsed_value)
    update_rule_fn(conn, rule_id, text) -> (rule_type, parsed_value)
    delete_rule_fn(conn, rule_id)
    """

    def __init__(self, conn, entity_label, list_fn, add_fn, delete_fn,
                 get_rules_fn, add_rule_fn, update_rule_fn, delete_rule_fn,
                 exclude_fn=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.entity_label = entity_label
        self.list_fn = list_fn
        self.add_fn = add_fn
        self.delete_fn = delete_fn
        self.get_rules_fn = get_rules_fn
        self.add_rule_fn = add_rule_fn
        self.update_rule_fn = update_rule_fn
        self.delete_rule_fn = delete_rule_fn
        self.exclude_fn = exclude_fn

        self.current_entity_id = None
        self._build_ui()
        self.refresh_entities()

    # ------------------------------------------------------------ UI setup

    def _build_ui(self):
        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # ---- Left: entity list ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel(f"{self.entity_label}s"))

        self.entity_list = QListWidget()
        self.entity_list.currentItemChanged.connect(self._on_entity_selected)
        self.entity_list.itemChanged.connect(self._on_entity_checkbox_toggled)
        left_layout.addWidget(self.entity_list)

        exclude_hint = QLabel(f"Untick a {self.entity_label.lower()} to exclude them from tomorrow's planning (sick day, vacation, expired contract, etc.) -- they'll turn orange and drop to the bottom as a reminder.")
        exclude_hint.setWordWrap(True)
        exclude_hint.setStyleSheet("color: #666666; font-size: 11px;")
        left_layout.addWidget(exclude_hint)

        btn_row = QHBoxLayout()
        add_btn = QPushButton(f"Add {self.entity_label}")
        add_btn.clicked.connect(self._on_add_entity)
        del_btn = QPushButton(f"Delete {self.entity_label}")
        del_btn.clicked.connect(self._on_delete_entity)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        left_layout.addLayout(btn_row)

        splitter.addWidget(left)

        # ---- Right: rule lines for selected entity ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.rules_header = QLabel("Select a " + self.entity_label.lower() + " to view its rules")
        right_layout.addWidget(self.rules_header)

        self.rules_list = QListWidget()
        self.rules_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.rules_list.itemChanged.connect(self._on_rule_line_edited)
        self.rules_list.itemSelectionChanged.connect(self._on_rule_selected)
        right_layout.addWidget(self.rules_list)

        self.recognition_label = QLabel("")
        self.recognition_label.setWordWrap(True)
        self.recognition_label.setStyleSheet("color: #2a7a2a; font-style: italic;")
        right_layout.addWidget(self.recognition_label)

        rule_btn_row = QHBoxLayout()
        add_line_btn = QPushButton("Add Line")
        add_line_btn.clicked.connect(self._on_add_rule_line)
        del_line_btn = QPushButton("Delete Line")
        del_line_btn.clicked.connect(self._on_delete_rule_line)
        rule_btn_row.addWidget(add_line_btn)
        rule_btn_row.addWidget(del_line_btn)
        right_layout.addLayout(rule_btn_row)

        hint = QLabel(
            "Tip: each line is one rule (e.g. \"Shift start: 07:00 AM\", \"Max duty hours: 8\"). "
            "Lines the app recognizes are enforced automatically; anything else is still saved "
            "and used by the AI as context."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666666; font-size: 11px;")
        right_layout.addWidget(hint)

        splitter.addWidget(right)
        splitter.setSizes([220, 480])

        self.rules_list.setEnabled(False)
        add_line_btn.setEnabled(True)  # will be gated in handlers instead

    # ------------------------------------------------------------ entities

    def refresh_entities(self, select_id=None):
        self.entity_list.blockSignals(True)
        self.entity_list.clear()
        rows = self.list_fn(self.conn)  # already sorted: included first, excluded last
        selected_item = None
        for position, row in enumerate(rows, start=1):
            excluded = bool(row["excluded_from_planning"]) if "excluded_from_planning" in row.keys() else False
            label = f"{position}. {row['name']}"
            if excluded:
                label += "  (not used tomorrow)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, row["id"])
            if self.exclude_fn is not None:
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
            self.current_entity_id = None
            self._render_rules([])

    def _on_entity_checkbox_toggled(self, item):
        if self.exclude_fn is None:
            return
        entity_id = item.data(Qt.UserRole)
        excluded = item.checkState() == Qt.Unchecked
        self.exclude_fn(self.conn, entity_id, excluded)
        self.refresh_entities(select_id=entity_id)

    def _on_add_entity(self):
        name, ok = QInputDialog.getText(self, f"Add {self.entity_label}", f"{self.entity_label} name:")
        if not ok or not name.strip():
            return
        try:
            new_id = self.add_fn(self.conn, name.strip())
        except sqlite3.IntegrityError:
            QMessageBox.information(
                self, "Already exists",
                f"A {self.entity_label.lower()} named '{name.strip()}' already exists. "
                f"Select it in the list on the left to edit its rules instead of adding it again."
            )
            self.refresh_entities()
            return
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not add {self.entity_label.lower()}: {e}")
            return
        self.refresh_entities(select_id=new_id)

    def _on_delete_entity(self):
        item = self.entity_list.currentItem()
        if not item:
            return
        entity_id = item.data(Qt.UserRole)
        name = item.text()
        confirm = QMessageBox.question(
            self, f"Delete {self.entity_label}",
            f"Delete '{name}' and all of its rule lines? This cannot be undone."
        )
        if confirm != QMessageBox.Yes:
            return
        self.delete_fn(self.conn, entity_id)
        self.refresh_entities()

    def _on_entity_selected(self, current, previous):
        if current is None:
            self.current_entity_id = None
            self._render_rules([])
            return
        self.current_entity_id = current.data(Qt.UserRole)
        self.rules_header.setText(f"Rules for: {current.text()}")
        self.rules_list.setEnabled(True)
        rules = self.get_rules_fn(self.conn, self.current_entity_id)
        self._render_rules(rules)

    # ------------------------------------------------------------ rules

    def _render_rules(self, rules):
        self.rules_list.blockSignals(True)
        self.rules_list.clear()
        for r in rules:
            item = QListWidgetItem(r["line_text"])
            item.setData(Qt.UserRole, r["id"])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self.rules_list.addItem(item)
        self.rules_list.blockSignals(False)
        self.recognition_label.setText("")

    def _on_add_rule_line(self):
        if self.current_entity_id is None:
            QMessageBox.information(self, "No selection", f"Select a {self.entity_label.lower()} first.")
            return
        text, ok = QInputDialog.getText(self, "Add Rule Line", "Rule line:")
        if not ok or not text.strip():
            return
        rule_id, rule_type, parsed_value = self.add_rule_fn(self.conn, self.current_entity_id, text.strip())
        rules = self.get_rules_fn(self.conn, self.current_entity_id)
        self._render_rules(rules)
        self.recognition_label.setText(describe_rule_type(rule_type, parsed_value))

    def _on_delete_rule_line(self):
        item = self.rules_list.currentItem()
        if not item:
            return
        rule_id = item.data(Qt.UserRole)
        self.delete_rule_fn(self.conn, rule_id)
        rules = self.get_rules_fn(self.conn, self.current_entity_id)
        self._render_rules(rules)

    def _on_rule_line_edited(self, item):
        rule_id = item.data(Qt.UserRole)
        new_text = item.text().strip()
        if not new_text:
            return
        rule_type, parsed_value = self.update_rule_fn(self.conn, rule_id, new_text)
        self.recognition_label.setText(describe_rule_type(rule_type, parsed_value))

    def _on_rule_selected(self):
        item = self.rules_list.currentItem()
        if not item:
            self.recognition_label.setText("")
            return
        rule_id = item.data(Qt.UserRole)
        rules = self.get_rules_fn(self.conn, self.current_entity_id)
        match = next((r for r in rules if r["id"] == rule_id), None)
        if match:
            parsed_value = __import__("json").loads(match["parsed_json"])
            self.recognition_label.setText(describe_rule_type(match["rule_type"], parsed_value))
