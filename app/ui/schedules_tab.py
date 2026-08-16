"""
schedules_tab.py

The "Schedules" correction screen. Real workflow this exists for: the
planner finalizes a day's plan the day before; a supervisor executes it
and makes real-world adjustments (a driver worked longer than assigned, a
driver/vehicle/supplier got swapped, a trip that wasn't planned happened
anyway, or a planned trip never happened); the planner needs to come back
afterward and correct the saved record to match what actually happened, in
the same visual format they already work in (Plan a Day), editing only
what changed.

Reads/writes app.db's finalized_jobs table directly -- the same table
"Finalize Day" (plan_day_tab.py) writes once; this tab is the only place
that table is ever corrected afterward. See AI/06_NEXT_SESSION.md Section
7.4 and AI/07_CHANGELOG_AI.md (Phase 31) for the full design writeup.

Every edit to an already-saved row asks for a specific confirmation
("Change Driver for SR 42 ... from 'X' to 'Y'?") before writing -- direct
database edits are easy to get wrong by accident, and this table feeds
real monthly overtime/hours numbers.
"""
from datetime import datetime, date, time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QComboBox, QMenu, QMessageBox, QCompleter, QWidgetAction
)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt, QPoint

from app import db

# Column order deliberately follows the real Excel file's left-to-right
# header order (app/excel_import.py's _HEADER_MAP: SR# -> Order -> Start
# Date -> Time -> Pick Up Location -> Contact Person -> Order Location ->
# Event -> Vehicle Type -> Additional Info -> Vehicle -> Driver -> Same
# Driver -> Charge Code) so the layout matches what the planner's eyes are
# already used to from the source file/Plan a Day, rather than an
# arbitrary order -- Cancelled/Date/Actual Start/Actual End/Hours are the
# exceptions, Schedules-tab-specific correction/tracking fields with no
# single Excel-column equivalent, kept at the front near SR.
(
    COL_CANCELLED, COL_SR, COL_ORDER_NO, COL_DATE, COL_START, COL_END, COL_HOURS,
    COL_PICKUP, COL_CONTACT, COL_ORDER_LOCATION, COL_EVENT, COL_VEHICLE_TYPE,
    COL_ADDITIONAL_INFO, COL_DRIVER, COL_VEHICLE, COL_SAME_DRIVER, COL_CHARGE_CODE, COL_ACTIONS,
) = range(18)
_COLUMN_HEADERS = [
    "Cancelled", "SR", "Order#", "Date", "Actual Start", "Actual End", "Hours",
    "Pick Up", "Contact Person", "Order Location", "Event", "Vehicle Type Required",
    "Additional Info", "Driver / Supplier", "Vehicle / Unit", "Same Driver", "Charge Code", "",
]
# Same 6 columns Plan a Day also hides by default (Order#, Contact Person,
# Order Location, Additional Info, Same Driver, Charge Code) -- toggled via
# the "Columns" button, matching that tab's exact precedent.
_DEFAULT_HIDDEN_COLUMNS = {
    COL_ORDER_NO, COL_CONTACT, COL_ORDER_LOCATION, COL_ADDITIONAL_INFO,
    COL_SAME_DRIVER, COL_CHARGE_CODE,
}
# Plain-text columns that go through the Continuous-Forms itemChanged path
# (as opposed to COL_CANCELLED's checkbox or COL_DRIVER/COL_VEHICLE's combo
# boxes). Maps column -> the finalized_jobs field name it corresponds to,
# and the human label used in confirm-dialog wording.
_TEXT_FIELD_COLUMNS = {
    # COL_DATE/COL_START/COL_END are intentionally NOT here -- they go
    # through _save_datetime_field() instead (see _on_item_changed), since
    # all three affect the same stored start_dt/end_dt pair and Hours gets
    # recomputed alongside them.
    COL_SR: ("sr", "SR"),
    COL_ORDER_NO: ("order_no", "Order#"),
    COL_HOURS: ("hours", "Hours"),
    COL_PICKUP: ("pickup_location", "Pick Up"),
    COL_CONTACT: ("contact_person", "Contact Person"),
    COL_ORDER_LOCATION: ("order_location", "Order Location"),
    COL_EVENT: ("event_text", "Event"),
    COL_VEHICLE_TYPE: ("vehicle_type_required", "Vehicle Type Required"),
    COL_ADDITIONAL_INFO: ("additional_info", "Additional Info"),
    COL_SAME_DRIVER: ("same_driver_key", "Same Driver"),
    COL_CHARGE_CODE: ("charge_code", "Charge Code"),
}
_FILTERABLE_COLUMNS = [
    COL_SR, COL_ORDER_NO, COL_DATE, COL_PICKUP, COL_CONTACT, COL_ORDER_LOCATION,
    COL_EVENT, COL_VEHICLE_TYPE, COL_DRIVER, COL_VEHICLE, COL_SAME_DRIVER, COL_CHARGE_CODE,
]
CANCELLED_TINT = QColor("#5a2020")
UNASSIGNED_LABEL = "-- Unassigned --"
_ROW_ID_ROLE = Qt.UserRole
_ROW_STATE_ROLE = Qt.UserRole + 1


class _NoScrollComboBox(QComboBox):
    """Ignores wheel events -- same fix as plan_day_tab.py's/
    vehicle_maintenance_dialog.py's class of the same name (duplicated
    locally again rather than shared, matching this project's established
    preference for small local duplication over new cross-file coupling)."""

    def wheelEvent(self, event):
        event.ignore()


class _NumericTableWidgetItem(QTableWidgetItem):
    """SR sorts numerically, not lexicographically -- same fix/duplication
    as plan_day_tab.py's class of the same name."""

    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except (ValueError, TypeError):
            return super().__lt__(other)


def _parse_iso_date(text):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_display_date(text):
    """Parses the DD-MM-YYYY format the planner actually types/sees in
    this tab (From/Till and the Date column) -- storage itself stays ISO
    (YYYY-MM-DD) in the database, matching every other date column in this
    project; only the display/input format differs. Same DD-MM-YYYY
    convention already used by vehicle_maintenance_dialog.py's service
    history grid."""
    try:
        return datetime.strptime((text or "").strip(), "%d-%m-%Y").date()
    except ValueError:
        return None


def _iso_to_display(iso_text):
    d = _parse_iso_date(iso_text)
    return d.strftime("%d-%m-%Y") if d else ""


def _display_to_iso(text):
    d = _parse_display_date(text)
    return d.isoformat() if d else None


def _parse_hhmm(text):
    text = (text or "").strip()
    try:
        h, m = text.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def _combine_date_time(display_date_text, time_text):
    d = _parse_display_date(display_date_text)
    t = _parse_hhmm(time_text)
    if d is None or t is None:
        return None
    return datetime.combine(d, t).isoformat()


def _split_datetime(iso_text):
    """(date_str, hhmm_str) from a stored ISO datetime, or ("", "") if
    unparseable/blank."""
    if not iso_text:
        return "", ""
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        return "", ""
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


class SchedulesTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._suppress_save = False
        self._column_filters = {}  # col -> set of allowed display strings, or absent = no filter
        self._build_ui()

    # ------------------------------------------------------------- build

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "Correct already-finalized days here to match what actually happened -- "
            "reassign drivers/vehicles/suppliers, fix actual hours worked, mark a planned "
            "trip Cancelled if it never happened, or add a trip that wasn't planned but did."
        ))

        today_display = date.today().strftime("%d-%m-%Y")
        load_row = QHBoxLayout()
        load_row.addWidget(QLabel("From:"))
        self.from_input = QLineEdit()
        self.from_input.setPlaceholderText("DD-MM-YYYY")
        self.from_input.setText(today_display)
        self.from_input.setAlignment(Qt.AlignCenter)
        self.from_input.setMaximumWidth(110)
        load_row.addWidget(self.from_input)
        load_row.addWidget(QLabel("Till:"))
        self.till_input = QLineEdit()
        self.till_input.setPlaceholderText("DD-MM-YYYY")
        self.till_input.setText(today_display)
        self.till_input.setAlignment(Qt.AlignCenter)
        self.till_input.setMaximumWidth(110)
        load_row.addWidget(self.till_input)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._on_load)
        load_row.addWidget(load_btn)
        load_row.addStretch(1)
        add_row_btn = QPushButton("+ Add Row")
        add_row_btn.clicked.connect(self._on_add_row)
        load_row.addWidget(add_row_btn)
        self.columns_btn = QPushButton("Columns ▾")
        self.columns_btn.clicked.connect(self._show_columns_menu)
        load_row.addWidget(self.columns_btn)
        layout.addLayout(load_row)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888888;")
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, len(_COLUMN_HEADERS))
        self.table.setHorizontalHeaderLabels(_COLUMN_HEADERS)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        self.table.setColumnWidth(COL_CANCELLED, 70)
        self.table.setColumnWidth(COL_SR, 55)
        self.table.setColumnWidth(COL_ORDER_NO, 90)
        self.table.setColumnWidth(COL_DATE, 95)
        self.table.setColumnWidth(COL_START, 90)
        self.table.setColumnWidth(COL_END, 90)
        self.table.setColumnWidth(COL_HOURS, 65)
        self.table.setColumnWidth(COL_PICKUP, 160)
        self.table.setColumnWidth(COL_CONTACT, 140)
        self.table.setColumnWidth(COL_ORDER_LOCATION, 160)
        self.table.setColumnWidth(COL_EVENT, 220)
        self.table.setColumnWidth(COL_VEHICLE_TYPE, 150)
        self.table.setColumnWidth(COL_ADDITIONAL_INFO, 200)
        self.table.setColumnWidth(COL_DRIVER, 190)
        self.table.setColumnWidth(COL_VEHICLE, 130)
        self.table.setColumnWidth(COL_SAME_DRIVER, 200)
        self.table.setColumnWidth(COL_CHARGE_CODE, 110)
        self.table.setColumnWidth(COL_ACTIONS, 110)
        for col in _DEFAULT_HIDDEN_COLUMNS:
            self.table.setColumnHidden(col, True)
        header.sectionClicked.connect(self._on_header_clicked)
        self.table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)

    def _show_columns_menu(self):
        menu = QMenu(self)
        for col, label in enumerate(_COLUMN_HEADERS):
            if not label:
                continue  # the trailing Actions column has no header text
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(col))
            action.toggled.connect(lambda checked, c=col: self.table.setColumnHidden(c, not checked))
        menu.exec(self.columns_btn.mapToGlobal(self.columns_btn.rect().bottomLeft()))

    # ------------------------------------------------------------- load

    def _on_load(self):
        date_from_display = self.from_input.text().strip()
        date_to_display = self.till_input.text().strip()
        date_from = _display_to_iso(date_from_display)
        date_to = _display_to_iso(date_to_display)
        if date_from is None or date_to is None:
            QMessageBox.information(self, "Invalid dates", "Enter both From and Till as DD-MM-YYYY.")
            return
        self._load_reassignment_options()
        rows = db.list_finalized_jobs(self.conn, date_from, date_to)
        self._column_filters = {}
        self._render_rows(rows)
        self.status_label.setText(f"{len(rows)} record(s) from {date_from_display} to {date_to_display}.")

    def _load_reassignment_options(self):
        self._driver_rows = list(db.list_drivers(self.conn))
        self._supplier_rows = list(db.list_suppliers(self.conn))
        self._vehicle_rows = list(db.list_vehicles(self.conn))
        self._driver_name_by_id = {r["id"]: r["name"] for r in self._driver_rows}
        self._driver_id_by_name = {r["name"]: r["id"] for r in self._driver_rows}
        self._supplier_id_by_name = {r["name"]: r["id"] for r in self._supplier_rows}
        self._vehicle_plate_by_id = {r["id"]: r["plate"] for r in self._vehicle_rows}
        self._vehicle_id_by_plate = {r["plate"]: r["id"] for r in self._vehicle_rows}

    def _driver_combo_items(self):
        return (
            [UNASSIGNED_LABEL]
            + sorted(self._driver_id_by_name, key=str.upper)
            + sorted(self._supplier_id_by_name, key=str.upper)
        )

    def _vehicle_combo_items(self):
        return [UNASSIGNED_LABEL] + sorted(self._vehicle_id_by_plate, key=str.upper)

    def _render_rows(self, rows):
        self._suppress_save = True
        self.table.setRowCount(0)
        driver_items = self._driver_combo_items()
        vehicle_items = self._vehicle_combo_items()
        for r in rows:
            self._insert_row(dict(r), driver_items, vehicle_items)
        self._suppress_save = False
        self._apply_column_filters()

    # ------------------------------------------------------------ combos

    def _make_combo(self, items, current_text):
        combo = _NoScrollComboBox()
        combo.addItems(items)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        completer = combo.completer()
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setFilterMode(Qt.MatchStartsWith)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        idx = combo.findText(current_text)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(current_text)
        combo.lineEdit().setCursorPosition(0)
        return combo

    def _driver_display(self, state):
        if state.get("driver_id") is not None:
            return state.get("driver_name") or self._driver_name_by_id.get(state["driver_id"], UNASSIGNED_LABEL)
        if state.get("supplier_label"):
            return state["supplier_label"]
        return UNASSIGNED_LABEL

    def _vehicle_display(self, state):
        if state.get("vehicle_id") is not None:
            return state.get("vehicle_plate") or self._vehicle_plate_by_id.get(state["vehicle_id"], UNASSIGNED_LABEL)
        if state.get("supplier_label"):
            return state["supplier_label"]
        return UNASSIGNED_LABEL

    # -------------------------------------------------------- row build

    def _insert_row(self, state, driver_items, vehicle_items, is_draft=False):
        row = self.table.rowCount()
        self.table.insertRow(row)

        cancelled_item = QTableWidgetItem()
        cancelled_item.setFlags(cancelled_item.flags() | Qt.ItemIsUserCheckable)
        cancelled_item.setFlags(cancelled_item.flags() & ~Qt.ItemIsEditable)
        cancelled_item.setCheckState(Qt.Checked if state.get("cancelled") else Qt.Unchecked)
        cancelled_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, COL_CANCELLED, cancelled_item)

        date_iso, start_str = _split_datetime(state.get("start_dt"))
        _, end_str = _split_datetime(state.get("end_dt"))
        date_iso = state.get("plan_date") or date_iso
        date_str = _iso_to_display(date_iso)

        sr_item = _NumericTableWidgetItem(str(state.get("sr") or ""))
        sr_item.setTextAlignment(Qt.AlignCenter)
        sr_item.setData(_ROW_ID_ROLE, state.get("id"))
        sr_item.setData(_ROW_STATE_ROLE, state)
        self.table.setItem(row, COL_SR, sr_item)

        text_values = {
            COL_DATE: date_str, COL_START: start_str, COL_END: end_str,
            COL_HOURS: "" if state.get("hours") is None else f"{state['hours']:.2f}".rstrip("0").rstrip("."),
            COL_EVENT: state.get("event_text") or "",
            COL_VEHICLE_TYPE: state.get("vehicle_type_required") or "",
            COL_PICKUP: state.get("pickup_location") or "",
            COL_ORDER_NO: state.get("order_no") or "",
            COL_CONTACT: state.get("contact_person") or "",
            COL_ORDER_LOCATION: state.get("order_location") or "",
            COL_ADDITIONAL_INFO: state.get("additional_info") or "",
            COL_SAME_DRIVER: state.get("same_driver_key") or "",
            COL_CHARGE_CODE: state.get("charge_code") or "",
        }
        for col, text in text_values.items():
            item = QTableWidgetItem(text)
            if col in (COL_DATE, COL_START, COL_END, COL_HOURS):
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)

        driver_combo = self._make_combo(driver_items, self._driver_display(state))
        driver_combo.currentTextChanged.connect(lambda text, r=row: self._on_driver_combo_changed(r, text))
        self.table.setCellWidget(row, COL_DRIVER, driver_combo)

        vehicle_combo = self._make_combo(vehicle_items, self._vehicle_display(state))
        vehicle_combo.currentTextChanged.connect(lambda text, r=row: self._on_vehicle_combo_changed(r, text))
        self.table.setCellWidget(row, COL_VEHICLE, vehicle_combo)

        if is_draft:
            self._set_draft_actions(row)
        self._apply_row_tint(row, bool(state.get("cancelled")))
        return row

    def _set_draft_actions(self, row):
        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(2, 0, 2, 0)
        actions_layout.setSpacing(4)
        save_btn = QPushButton("Save")
        save_btn.setToolTip("Save this new row")
        save_btn.clicked.connect(lambda: self._on_save_draft_row(row))
        discard_btn = QPushButton("✕")
        discard_btn.setToolTip("Discard -- nothing has been saved for this row yet")
        discard_btn.setFixedWidth(28)
        discard_btn.clicked.connect(lambda: self._on_discard_draft_row(row))
        actions_layout.addWidget(save_btn)
        actions_layout.addWidget(discard_btn)
        self.table.setCellWidget(row, COL_ACTIONS, actions)

    def _apply_row_tint(self, row, cancelled):
        # setData() on ANY role -- background included, not just text --
        # re-emits itemChanged in Qt (same gotcha documented in
        # vehicle_maintenance_dialog.py's _save_service_row). Without
        # suppressing here, tinting a row after a save would immediately
        # re-enter _on_item_changed for every text column in the row,
        # misread as a real (if no-op) edit. Saves/restores the PREVIOUS
        # suppress state (not an unconditional True->False) since this is
        # also called from inside _insert_row's own already-suppressed
        # population loop -- blindly resetting to False here would turn
        # suppression off mid-loop for the rows still to come.
        previous = self._suppress_save
        self._suppress_save = True
        try:
            tint = CANCELLED_TINT if cancelled else None
            for col in range(self.table.columnCount()):
                if col in (COL_DRIVER, COL_VEHICLE):
                    combo = self.table.cellWidget(row, col)
                    if combo is not None:
                        combo.setStyleSheet(f"QComboBox {{ background-color: {tint.name()}; }}" if tint else "")
                    continue
                item = self.table.item(row, col)
                if item is None:
                    continue
                if tint is not None:
                    item.setBackground(tint)
                else:
                    item.setData(Qt.BackgroundRole, None)
        finally:
            self._suppress_save = previous

    # --------------------------------------------------------- add row

    def _on_add_row(self):
        driver_items = self._driver_combo_items() if hasattr(self, "_driver_id_by_name") else [UNASSIGNED_LABEL]
        vehicle_items = self._vehicle_combo_items() if hasattr(self, "_vehicle_id_by_plate") else [UNASSIGNED_LABEL]
        today = date.today().isoformat()
        state = {"id": None, "plan_date": today, "cancelled": 0}
        self._suppress_save = True
        row = self._insert_row(state, driver_items, vehicle_items, is_draft=True)
        self._suppress_save = False
        self.table.scrollToBottom()
        self.table.setCurrentCell(row, COL_SR)

    def _on_discard_draft_row(self, row):
        self.table.removeRow(row)

    def _on_save_draft_row(self, row):
        sr_item = self.table.item(row, COL_SR)
        if sr_item is None or sr_item.data(_ROW_ID_ROLE) is not None:
            return  # already saved somehow -- nothing to do
        fields = self._collect_row_fields(row)
        plan_date_iso = fields.pop("plan_date")
        new_id = db.insert_finalized_job(self.conn, plan_date_iso, **fields)
        state = dict(fields, id=new_id, plan_date=plan_date_iso)
        sr_item.setData(_ROW_ID_ROLE, new_id)
        sr_item.setData(_ROW_STATE_ROLE, state)
        self.table.removeCellWidget(row, COL_ACTIONS)
        self.status_label.setText(f"New row saved (SR {state.get('sr') or '--'}).")

    def _collect_row_fields(self, row):
        driver_combo = self.table.cellWidget(row, COL_DRIVER)
        vehicle_combo = self.table.cellWidget(row, COL_VEHICLE)
        driver_id, driver_name, supplier_id, supplier_label = self._resolve_driver_field(
            driver_combo.currentText() if driver_combo else UNASSIGNED_LABEL
        )
        vehicle_id, vehicle_plate = self._resolve_vehicle_field(
            vehicle_combo.currentText() if vehicle_combo else UNASSIGNED_LABEL
        )
        date_text = self._cell_text(row, COL_DATE)
        start_dt = _combine_date_time(date_text, self._cell_text(row, COL_START))
        end_dt = _combine_date_time(date_text, self._cell_text(row, COL_END))
        hours_text = self._cell_text(row, COL_HOURS)
        try:
            hours = float(hours_text) if hours_text else None
        except ValueError:
            hours = None
        cancelled_item = self.table.item(row, COL_CANCELLED)
        return {
            "plan_date": _display_to_iso(date_text) or date.today().isoformat(),
            "sr": self._cell_text(row, COL_SR),
            "driver_id": driver_id, "vehicle_id": vehicle_id,
            "supplier_id": supplier_id, "supplier_label": supplier_label,
            "start_dt": start_dt, "end_dt": end_dt, "hours": hours,
            "event_text": self._cell_text(row, COL_EVENT),
            "pickup_location": self._cell_text(row, COL_PICKUP),
            "vehicle_type_required": self._cell_text(row, COL_VEHICLE_TYPE),
            "driver_name": driver_name, "vehicle_plate": vehicle_plate,
            "order_no": self._cell_text(row, COL_ORDER_NO),
            "contact_person": self._cell_text(row, COL_CONTACT),
            "order_location": self._cell_text(row, COL_ORDER_LOCATION),
            "additional_info": self._cell_text(row, COL_ADDITIONAL_INFO),
            "charge_code": self._cell_text(row, COL_CHARGE_CODE),
            "same_driver_key": self._cell_text(row, COL_SAME_DRIVER),
            "cancelled": 1 if cancelled_item and cancelled_item.checkState() == Qt.Checked else 0,
        }

    def _cell_text(self, row, col):
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _resolve_driver_field(self, text):
        text = text.strip()
        if not text or text == UNASSIGNED_LABEL:
            return None, "", None, None
        if text in self._driver_id_by_name:
            return self._driver_id_by_name[text], text, None, None
        base_name = text.rstrip("0123456789").strip()
        supplier_id = self._supplier_id_by_name.get(base_name, self._supplier_id_by_name.get(text))
        return None, "", supplier_id, text

    def _resolve_vehicle_field(self, text):
        text = text.strip()
        if not text or text == UNASSIGNED_LABEL:
            return None, ""
        if text in self._vehicle_id_by_plate:
            return self._vehicle_id_by_plate[text], text
        return None, ""

    # ------------------------------------------------------- edit + confirm

    def _confirm_change(self, row, label, old_display, new_display):
        if old_display == new_display:
            return True
        sr = self._cell_text(row, COL_SR) or "?"
        event_text = self._cell_text(row, COL_EVENT)
        context = f"SR {sr}" + (f" ({event_text})" if event_text else "")
        answer = QMessageBox.question(
            self, "Confirm Change",
            f"Change {label} for {context} from '{old_display}' to '{new_display}'?",
        )
        return answer == QMessageBox.Yes

    def _on_driver_combo_changed(self, row, new_text):
        if self._suppress_save:
            return
        sr_item = self.table.item(row, COL_SR)
        if sr_item is None:
            return
        row_id = sr_item.data(_ROW_ID_ROLE)
        state = sr_item.data(_ROW_STATE_ROLE) or {}
        old_display = self._driver_display(state)
        combo = self.table.cellWidget(row, COL_DRIVER)
        if row_id is None:
            return  # still a draft row -- no confirmation/DB call until Save
        if not self._confirm_change(row, "Driver/Supplier", old_display, new_text):
            self._revert_combo(combo, old_display)
            return
        driver_id, driver_name, supplier_id, supplier_label = self._resolve_driver_field(new_text)
        db.update_finalized_job(
            self.conn, row_id, driver_id=driver_id, driver_name=driver_name or None,
            supplier_id=supplier_id, supplier_label=supplier_label,
        )
        state.update(driver_id=driver_id, driver_name=driver_name, supplier_id=supplier_id,
                      supplier_label=supplier_label)
        sr_item.setData(_ROW_STATE_ROLE, state)
        if supplier_label:
            self._sync_vehicle_cell(row, supplier_label)
        self.status_label.setText(f"Saved: Driver/Supplier for SR {self._cell_text(row, COL_SR)}.")

    def _on_vehicle_combo_changed(self, row, new_text):
        if self._suppress_save:
            return
        sr_item = self.table.item(row, COL_SR)
        if sr_item is None:
            return
        row_id = sr_item.data(_ROW_ID_ROLE)
        state = sr_item.data(_ROW_STATE_ROLE) or {}
        old_display = self._vehicle_display(state)
        combo = self.table.cellWidget(row, COL_VEHICLE)
        if row_id is None:
            return
        if not self._confirm_change(row, "Vehicle/Unit", old_display, new_text):
            self._revert_combo(combo, old_display)
            return
        vehicle_id, vehicle_plate = self._resolve_vehicle_field(new_text)
        db.update_finalized_job(self.conn, row_id, vehicle_id=vehicle_id, vehicle_plate=vehicle_plate or None)
        state.update(vehicle_id=vehicle_id, vehicle_plate=vehicle_plate)
        sr_item.setData(_ROW_STATE_ROLE, state)
        self.status_label.setText(f"Saved: Vehicle/Unit for SR {self._cell_text(row, COL_SR)}.")

    def _revert_combo(self, combo, old_display):
        if combo is None:
            return
        combo.blockSignals(True)
        idx = combo.findText(old_display)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(old_display)
        combo.blockSignals(False)

    def _sync_vehicle_cell(self, row, text):
        combo = self.table.cellWidget(row, COL_VEHICLE)
        if combo is None:
            return
        combo.blockSignals(True)
        idx = combo.findText(text)
        combo.setCurrentIndex(idx) if idx >= 0 else combo.setEditText(text)
        combo.lineEdit().setCursorPosition(0)
        combo.blockSignals(False)

    def _save_datetime_field(self, row, col, sr_item, state, row_id, label, old_display):
        new_display = self._cell_text(row, col)
        if not self._confirm_change(row, label, old_display, new_display):
            self._suppress_save = True
            self.table.item(row, col).setText(old_display)
            self._suppress_save = False
            return
        date_text = self._cell_text(row, COL_DATE)
        start_dt = _combine_date_time(date_text, self._cell_text(row, COL_START))
        end_dt = _combine_date_time(date_text, self._cell_text(row, COL_END))
        updates = {"start_dt": start_dt, "end_dt": end_dt}
        if col == COL_DATE:
            updates["plan_date"] = _display_to_iso(date_text)
        # Hours is auto-recomputed as a convenience whenever Date/Start/End
        # changes -- included in the SAME save (no separate confirmation),
        # but stays directly hand-editable afterward (e.g. an unpaid break)
        # via the normal Hours column edit path below.
        hours = None
        if start_dt and end_dt:
            try:
                delta = datetime.fromisoformat(end_dt) - datetime.fromisoformat(start_dt)
                hours = round(delta.total_seconds() / 3600.0, 2)
            except ValueError:
                hours = None
        if hours is not None:
            updates["hours"] = hours
            self._suppress_save = True
            hours_item = self.table.item(row, COL_HOURS)
            if hours_item:
                hours_item.setText(f"{hours:.2f}".rstrip("0").rstrip("."))
            self._suppress_save = False
        db.update_finalized_job(self.conn, row_id, **updates)
        state.update(updates)
        sr_item.setData(_ROW_STATE_ROLE, state)
        self.status_label.setText(f"Saved: {label} for SR {self._cell_text(row, COL_SR)}.")

    def _on_item_changed(self, item):
        if self._suppress_save:
            return
        row, col = item.row(), item.column()
        sr_item = self.table.item(row, COL_SR)
        if sr_item is None:
            return
        row_id = sr_item.data(_ROW_ID_ROLE)
        state = sr_item.data(_ROW_STATE_ROLE) or {}

        if col == COL_CANCELLED:
            new_cancelled = 1 if item.checkState() == Qt.Checked else 0
            old_cancelled = 1 if state.get("cancelled") else 0
            if row_id is None:
                return
            if new_cancelled == old_cancelled:
                return
            if not self._confirm_change(row, "Cancelled", bool(old_cancelled), bool(new_cancelled)):
                self._suppress_save = True
                item.setCheckState(Qt.Checked if old_cancelled else Qt.Unchecked)
                self._suppress_save = False
                return
            db.update_finalized_job(self.conn, row_id, cancelled=new_cancelled)
            state["cancelled"] = new_cancelled
            sr_item.setData(_ROW_STATE_ROLE, state)
            self._apply_row_tint(row, bool(new_cancelled))
            self.status_label.setText(f"Saved: Cancelled for SR {self._cell_text(row, COL_SR)}.")
            return

        if row_id is None:
            return  # draft row -- no confirmation/DB call until Save

        if col == COL_DATE:
            self._save_datetime_field(row, col, sr_item, state, row_id, "Date", _iso_to_display(state.get("plan_date")))
            return
        if col == COL_START:
            self._save_datetime_field(row, col, sr_item, state, row_id, "Actual Start",
                                       _split_datetime(state.get("start_dt"))[1])
            return
        if col == COL_END:
            self._save_datetime_field(row, col, sr_item, state, row_id, "Actual End",
                                       _split_datetime(state.get("end_dt"))[1])
            return

        if col not in _TEXT_FIELD_COLUMNS:
            return
        field_name, label = _TEXT_FIELD_COLUMNS[col]
        new_text = item.text().strip()

        old_display = "" if state.get(field_name) is None else str(state.get(field_name))
        if field_name == "hours" and state.get("hours") is not None:
            old_display = f"{state['hours']:.2f}".rstrip("0").rstrip(".")
        if not self._confirm_change(row, label, old_display, new_text):
            self._suppress_save = True
            item.setText(old_display)
            self._suppress_save = False
            return
        value = new_text or None
        if field_name == "hours":
            try:
                value = float(new_text) if new_text else None
            except ValueError:
                QMessageBox.warning(self, "Invalid hours", "Hours must be a number.")
                self._suppress_save = True
                item.setText(old_display)
                self._suppress_save = False
                return
        db.update_finalized_job(self.conn, row_id, **{field_name: value})
        state[field_name] = value
        sr_item.setData(_ROW_STATE_ROLE, state)
        self.status_label.setText(f"Saved: {label} for SR {self._cell_text(row, COL_SR)}.")

    # --------------------------------------------------------- filter/sort

    def _build_filter_menu(self, col):
        """Builds the Sort/Search/Select-All/checklist menu for one column
        header, WITHOUT showing it (no blocking .exec() call here) -- kept
        separate from _on_header_clicked() specifically so this part is
        directly unit-testable (QMenu.exec() is a real modal call with no
        clean way to drive it from a script). Returns (menu, sort_asc,
        sort_desc, select_all, clear_all, values, value_actions)."""
        menu = QMenu(self)
        sort_asc = menu.addAction("Sort Ascending")
        sort_desc = menu.addAction("Sort Descending")
        menu.addSeparator()

        values = set()
        for row in range(self.table.rowCount()):
            values.add(self._display_value_for_filter(row, col))
        allowed = self._column_filters.get(col)
        select_all = menu.addAction("Select All")
        clear_all = menu.addAction("Clear All")
        menu.addSeparator()

        # Search box, same idea as Excel/Sheets/Tableau/Airtable's own
        # column-filter dropdowns: without it, a high-cardinality column
        # (Date over a year of history, Event, Driver/Supplier, ...) turns
        # into a flat checklist hundreds of entries long -- typing a few
        # characters narrows it instead of scrolling. QWidgetAction is the
        # standard Qt way to embed a live-typing widget in a QMenu without
        # the menu auto-closing on keystrokes (a plain QAction would).
        search_action = QWidgetAction(menu)
        search_box = QLineEdit()
        search_box.setPlaceholderText("Search...")
        search_action.setDefaultWidget(search_box)
        menu.insertAction(select_all, search_action)
        menu.insertSeparator(select_all)

        value_actions = {}
        for v in sorted(values, key=str.upper):
            action = menu.addAction(v if v else "(blank)")
            action.setCheckable(True)
            action.setChecked(allowed is None or v in allowed)
            value_actions[action] = v

        def _narrow(text):
            text = text.strip().lower()
            for action, v in value_actions.items():
                action.setVisible(text in (v or "").lower())
        search_box.textChanged.connect(_narrow)
        search_box.setFocus()

        return menu, sort_asc, sort_desc, select_all, clear_all, values, value_actions

    def _on_header_clicked(self, col):
        if col not in _FILTERABLE_COLUMNS:
            return
        menu, sort_asc, sort_desc, select_all, clear_all, values, value_actions = self._build_filter_menu(col)

        header = self.table.horizontalHeader()
        anchor = QPoint(header.sectionViewportPosition(col), header.height())
        chosen = menu.exec(header.mapToGlobal(anchor))
        if chosen is None:
            return
        if chosen is sort_asc:
            self.table.sortItems(col, Qt.AscendingOrder)
            self._rebind_row_widgets()
        elif chosen is sort_desc:
            self.table.sortItems(col, Qt.DescendingOrder)
            self._rebind_row_widgets()
        elif chosen is select_all:
            self._column_filters.pop(col, None)
            self._apply_column_filters()
        elif chosen is clear_all:
            self._column_filters[col] = set()
            self._apply_column_filters()
        elif chosen in value_actions:
            current = self._column_filters.get(col)
            current = set(values) if current is None else set(current)
            v = value_actions[chosen]
            if chosen.isChecked():
                current.add(v)
            else:
                current.discard(v)
            self._column_filters[col] = current
            self._apply_column_filters()

    def _display_value_for_filter(self, row, col):
        if col in (COL_DRIVER, COL_VEHICLE):
            combo = self.table.cellWidget(row, col)
            return combo.currentText() if combo else ""
        item = self.table.item(row, col)
        return item.text() if item else ""

    def _apply_column_filters(self):
        for row in range(self.table.rowCount()):
            visible = True
            for col, allowed in self._column_filters.items():
                if allowed is None:
                    continue
                if self._display_value_for_filter(row, col) not in allowed:
                    visible = False
                    break
            self.table.setRowHidden(row, not visible)

    def _rebind_row_widgets(self):
        driver_items = self._driver_combo_items()
        vehicle_items = self._vehicle_combo_items()
        for row in range(self.table.rowCount()):
            sr_item = self.table.item(row, COL_SR)
            if sr_item is None:
                continue
            state = sr_item.data(_ROW_STATE_ROLE) or {}
            driver_combo = self._make_combo(driver_items, self._driver_display(state))
            driver_combo.currentTextChanged.connect(lambda text, r=row: self._on_driver_combo_changed(r, text))
            self.table.setCellWidget(row, COL_DRIVER, driver_combo)
            vehicle_combo = self._make_combo(vehicle_items, self._vehicle_display(state))
            vehicle_combo.currentTextChanged.connect(lambda text, r=row: self._on_vehicle_combo_changed(r, text))
            self.table.setCellWidget(row, COL_VEHICLE, vehicle_combo)
            self._apply_row_tint(row, bool(state.get("cancelled")))
