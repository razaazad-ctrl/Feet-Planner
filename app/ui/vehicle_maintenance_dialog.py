"""
vehicle_maintenance_dialog.py

The Vehicle Maintenance Log window, opened from the Vehicles tab's wrench
button for one specific vehicle.

Phase 28b (2026-08-14): this window is READ-ONLY for every vehicle detail
field (model, year, chassis, engine, registration, certificates, tyre,
battery, picture) -- it's a report, not a form. Editing any of those
fields happens exclusively through the Vehicles tab's "Edit Selected"
(EditVehicleDialog, vehicles_tab.py). The service history below IS still
editable here, as an Access-style "Continuous Forms" grid: every row is
live-editable in place (including an inline Service Type combo box per
row), an "Add a Record" button appends a blank row at the bottom, and
each row auto-saves as soon as it's edited -- no separate add/edit form,
no explicit per-row Save button, matching real MS Access form behavior.
"""
from datetime import date, datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QCheckBox, QComboBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QFrame, QAbstractItemView,
)
from PySide6.QtGui import QPixmap, QGuiApplication
from PySide6.QtCore import Qt, QTimer, QFile
from PySide6.QtUiTools import QUiLoader

from app import db

SERVICE_TYPES = [
    "Quotation", "Oil/Filter Change", "Chiller Unit Service", "Accident",
    "Battery Change", "Repair", "Mechanical Work", "Body Work", "Tyre Change",
]

_EXPIRED_COLOR = "#c0392b"
_ASSETS = Path(__file__).parent

# Service history table column indices.
SC_START = 0
SC_END = 1
SC_TYPE = 2
SC_DETAILS = 3
SC_CURRENT = 4
SC_NEXT = 5
SC_QTY = 6
SC_PERSON = 7
SC_WORKSHOP = 8


def _parse_iso_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _display_date(iso_text):
    d = _parse_iso_date(iso_text)
    return d.strftime("%d-%b-%Y") if d else (iso_text or "--")


def _is_expired(iso_text):
    d = _parse_iso_date(iso_text)
    return d is not None and d < date.today()


def _parse_display_date(text):
    """Parses the DD-MM-YYYY format typed directly into the editable
    service-history Start Date / End Date cells."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%d-%m-%Y").date()
    except ValueError:
        return None


def _iso_to_display(iso_text):
    """DB storage stays ISO (YYYY-MM-DD) -- every other date field in this
    dialog (expiry checks, summary cards) already parses that format via
    _parse_iso_date/_display_date. This only converts to DD-MM-YYYY for
    showing in the editable service-history grid cells."""
    d = _parse_iso_date(iso_text)
    return d.strftime("%d-%m-%Y") if d else ""


def _display_to_iso(text):
    """The inverse of _iso_to_display -- converts what the planner typed
    (DD-MM-YYYY) back to ISO before it's written to service_records."""
    d = _parse_display_date(text)
    return d.isoformat() if d else None


class _NoScrollComboBox(QComboBox):
    """A QComboBox that ignores mouse-wheel events, used for the Service
    Type column in the service-history grid. A plain QComboBox changes
    its selection on any wheel scroll while the cursor happens to be
    over it -- including just scrolling the table past that row -- which
    could silently turn a saved record into the wrong service type.
    Ignoring the event here (rather than accepting it) lets Qt propagate
    it up to the table's viewport, so scrolling the table still works
    normally; only the combo box's own value stops changing by accident."""

    def wheelEvent(self, event):
        event.ignore()


class VehicleMaintenanceDialog(QDialog):
    def __init__(self, conn, vehicle_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.vehicle_id = vehicle_id

        self.setWindowTitle("Vehicle Maintenance Log")
        # A flat resize(1050, 950) ignored the taskbar and could open with
        # part of the window (footer/Close button) hidden below it on
        # smaller or taskbar-heavy screens. availableGeometry() already
        # excludes the taskbar, so clamping to it (minus a small margin)
        # keeps the whole dialog on-screen regardless of monitor size.
        # The minimum size is clamped by the same cap -- clamping only the
        # target size and leaving setMinimumSize(950, 700) as a hard floor
        # would let a later setMinimumSize call force the width/height
        # straight back past the clamp on a small/constrained screen.
        target_w, target_h = 1050, 950
        min_w, min_h = 950, 700
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            cap_w = max(avail.width() - 40, 1)
            cap_h = max(avail.height() - 40, 1)
            target_w = min(target_w, cap_w)
            target_h = min(target_h, cap_h)
            min_w = min(min_w, cap_w)
            min_h = min(min_h, cap_h)
        self.setMinimumSize(min_w, min_h)
        self.resize(target_w, target_h)
        # Every widget class used anywhere in this dialog is styled
        # explicitly -- must not rely on inheriting anything from the
        # app's default (OS dark-mode-following; no app-level stylesheet
        # exists anywhere else in this codebase) look, the same way
        # DriverSupplierSummaryDialog (Summary popup) fully self-styles.
        self.setStyleSheet("""
            QDialog { background: #ffffff; color: #161616; }
            QLabel { color: #161616; background: transparent; }
            QComboBox {
                background: #ffffff; color: #161616;
                border: 1px solid #cfd6e0; border-radius: 4px; padding: 2px 4px;
            }
            QComboBox QAbstractItemView {
                background: #ffffff; color: #161616; selection-background-color: #eef4ff;
            }
            QCheckBox { color: #161616; spacing: 6px; }
            QPushButton {
                background: #f3f5f8; color: #161616; border: 1px solid #d6dce4;
                border-radius: 6px; padding: 6px 14px;
            }
            QPushButton:hover { background: #e7ebf1; }
            QFrame#card { border: 1px solid #e2e8f1; border-radius: 12px; }
            QLabel#cardTitle { font-size: 13px; color: #46505f; font-weight: 650; }
            QLabel#cardDate { font-size: 15px; color: #111827; font-weight: 650; }
            QLabel#cardExtra { font-size: 13px; color: #555555; }
            QPushButton#closeButton {
                background: #3f7ee8; color: white; border: none; border-radius: 10px;
                padding: 8px 22px; font-size: 13px;
            }
            QPushButton#closeButton:hover { background: #336dcc; }
            QTableWidget {
                background: #ffffff; alternate-background-color: #fbfcfe;
                border: 1px solid #e1e6ef; border-radius: 8px;
                gridline-color: #e4e9f0; color: #161616; font-size: 12px;
                selection-background-color: #cfe3fb; selection-color: #161616;
            }
            QTableWidget::item:selected {
                background: #cfe3fb; color: #161616;
            }
            QHeaderView::section {
                background: #f5f7fa; color: #222222; border: none;
                border-bottom: 1px solid #dfe4ec; border-right: 1px solid #f5f7fa;
                padding: 6px; font-size: 11px; font-weight: 650;
            }
            QTableCornerButton::section {
                background: #f5f7fa; border: none;
            }
            QScrollBar:vertical {
                background: #f5f7fa; width: 13px; margin: 0; border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #c7cfdb; border-radius: 6px; min-height: 24px;
            }
            QScrollBar::handle:vertical:hover { background: #aab4c4; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 16)
        root.setSpacing(14)

        root.addLayout(self._build_header())
        root.addWidget(self._build_vehicle_info())

        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(10)
        root.addLayout(self.cards_row)

        root.addWidget(self._divider())

        history_header = QHBoxLayout()
        history_header.addWidget(QLabel("Service history (double-click a cell to edit -- saves automatically):"))
        history_header.addStretch(1)
        add_record_btn = QPushButton("+ Add a Record")
        add_record_btn.clicked.connect(self._on_add_record_row)
        history_header.addWidget(add_record_btn)
        delete_record_btn = QPushButton("Delete Selected Row")
        delete_record_btn.clicked.connect(self._on_delete_record_row)
        history_header.addWidget(delete_record_btn)
        root.addLayout(history_header)

        self.service_table = QTableWidget()
        self.service_table.setColumnCount(9)
        self.service_table.setHorizontalHeaderLabels([
            "Start Date", "End Date", "Service Type", "Details", "Current Reading",
            "Next Reading", "Qty", "Person", "Workshop",
        ])
        self.service_table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed | QTableWidget.AnyKeyPressed
        )
        self.service_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.service_table.setAlternatingRowColors(True)
        # Always-visible (not auto-hide) vertical scrollbar -- a clear,
        # themed, easily-grabbed manual scroll control now that the
        # Service Type combo box (Phase 28g) no longer responds to wheel
        # scroll, so the table's own scrollbar is the more discoverable
        # way to move through many months of service history.
        self.service_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.service_table.horizontalHeader().setSectionResizeMode(SC_DETAILS, QHeaderView.Stretch)
        self.service_table.verticalHeader().setDefaultSectionSize(26)
        self.service_table.itemChanged.connect(self._on_service_item_changed)
        root.addWidget(self.service_table, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("closeButton")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

        self._suppress_save = False  # guards against saving rows still under construction
        self._load_vehicle()
        self._reload_service_records()

    # ---------------------------------------------------------- header/info

    def _build_header(self):
        row = QHBoxLayout()
        icon_label = QLabel()
        icon_path = _ASSETS / "vehicle_maintenance_icon.png"
        if icon_path.exists():
            icon_label.setPixmap(QPixmap(str(icon_path)).scaledToHeight(48, Qt.SmoothTransformation))
        row.addWidget(icon_label)
        title = QLabel("VEHICLE MAINTENANCE LOG")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        row.addWidget(title)
        row.addStretch(1)
        self.active_checkbox = QCheckBox("Active")
        self.active_checkbox.stateChanged.connect(self._on_active_toggled)
        row.addWidget(self.active_checkbox)
        return row

    def _build_vehicle_info(self):
        """Loads the vehicle-info section (Model/Type/Chassis/Engine/RTA/
        Ad Certificate stack, plate, picture) from vehicle_info_section.ui
        at runtime via QUiLoader (Phase 28p) -- at the project owner's
        request, this section is now editable visually in Qt Designer
        (already bundled with the project's PySide6 install) instead of
        by hand-editing code. QUiLoader parses the raw .ui XML directly
        every time the app starts; there is no separate compile/generate
        step -- editing and saving the .ui file in Designer is picked up
        immediately on the next run.

        The .ui file itself was generated once (not hand-typed) from this
        method's previous code, via QFormBuilder, seeded with the sizes
        measured from the project owner's own before/after reference
        images (Phase 28o) -- that's the starting point for them to now
        adjust by mouse."""
        loader = QUiLoader()
        ui_path = _ASSETS / "vehicle_info_section.ui"
        ui_file = QFile(str(ui_path))
        ui_file.open(QFile.ReadOnly)
        section = loader.load(ui_file, self)
        ui_file.close()

        self.model_year_label = section.findChild(QLabel, "model_year_label")
        self.type_display = section.findChild(QLabel, "type_display")
        self.chassis_value = section.findChild(QLabel, "chassis_value")
        self.engine_value = section.findChild(QLabel, "engine_value")
        self.rta_cert_value = section.findChild(QLabel, "rta_cert_value")
        self.rta_cert_expiry_value = section.findChild(QLabel, "rta_cert_expiry_value")
        self.ad_cert_value = section.findChild(QLabel, "ad_cert_value")
        self.ad_cert_expiry_value = section.findChild(QLabel, "ad_cert_expiry_value")
        self.plate_display = section.findChild(QLabel, "plate_display")
        self.picture_label = section.findChild(QLabel, "picture_label")

        # setColumnStretch() is a method call, not a Qt property -- it
        # isn't captured when Designer/QFormBuilder saves a .ui file, so
        # it has to be re-applied here after loading rather than living in
        # the .ui file itself. Equal stretch (1, 1) on the two outer
        # columns is a sane default for the plate's column to land near
        # center. It is NOT pixel-perfect once the two sides' content
        # differs in width -- a Phase 28o/28p attempt at forcing exact
        # centering by matching column-2's minimum width to column 0's
        # sizeHint worked with short sample text but broke down with
        # realistic, longer field values (measured: column 0 alone wanted
        # 870px, far more than a 1050px dialog has room to mirror on both
        # sides). Given the project owner's whole point in converting this
        # section to a .ui file is to adjust it visually themselves,
        # fighting for exact programmatic centering here would work
        # against that -- if the default position isn't exactly where
        # they want it, that's now theirs to fine-tune in Designer
        # (spacers, column widths, alignment) rather than something to
        # keep re-engineering in Python.
        self._info_grid = section.findChild(QGridLayout, "infoRow")
        if self._info_grid is not None:
            self._info_grid.setColumnStretch(0, 1)
            self._info_grid.setColumnStretch(2, 1)

        return section

    def _divider(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #e4e9f0;")
        return line

    # ------------------------------------------------------------- loading

    def _load_vehicle(self):
        row = db.get_vehicle(self.conn, self.vehicle_id)
        if row is None:
            QMessageBox.warning(self, "Vehicle not found", "This vehicle no longer exists.")
            self.reject()
            return

        self.active_checkbox.blockSignals(True)
        self.active_checkbox.setChecked(not bool(row["excluded_from_planning"]))
        self.active_checkbox.blockSignals(False)

        model = row["vehicle_model"] or ""
        year = str(row["vehicle_year"]) if row["vehicle_year"] else ""
        self.model_year_label.setText(f"{model}   {year}".strip() or "(no model set)")
        self.type_display.setText(row["vehicle_type"] or "")
        self.plate_display.setText(row["plate"] or "")

        self.chassis_value.setText(row["vehicle_chassis"] or "--")
        self.engine_value.setText(row["vehicle_engine"] or "--")
        self.rta_cert_value.setText(row["rta_certificate"] or "--")
        self.rta_cert_expiry_value.setText(_display_date(row["rta_certificate_expiry"]))
        self.ad_cert_value.setText(row["ad_certificate"] or "--")
        self.ad_cert_expiry_value.setText(_display_date(row["ad_certificate_expiry"]))

        for value_label, iso in [
            (self.rta_cert_expiry_value, row["rta_certificate_expiry"]),
            (self.ad_cert_expiry_value, row["ad_certificate_expiry"]),
        ]:
            if _is_expired(iso):
                # setStyleSheet() replaces the whole per-widget stylesheet,
                # not just these two properties -- since Phase 28p, the
                # base field-value style (font-size: 15px etc.) lives
                # directly on each widget (baked into
                # vehicle_info_section.ui) rather than in a shared
                # dialog-level rule, so font-size must be repeated here or
                # it's lost entirely for whichever field happens to be
                # expired.
                value_label.setStyleSheet(
                    f"color: {_EXPIRED_COLOR}; font-size: 15px; font-weight: 700;"
                )

        if row["vehicle_picture"]:
            pixmap = QPixmap()
            pixmap.loadFromData(row["vehicle_picture"])
            if not pixmap.isNull():
                self.picture_label.setPixmap(
                    pixmap.scaled(150, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
        else:
            self.picture_label.setText("No picture")

    def _on_active_toggled(self):
        db.set_vehicle_excluded(self.conn, self.vehicle_id, excluded=not self.active_checkbox.isChecked())

    def accept(self):
        # QDialog.accept() -> done(Accepted) -> hide(). It does NOT call
        # close(), so closeEvent() below was NEVER actually triggered by
        # clicking the "Close" button (which connects straight to
        # accept()) -- confirmed against Qt's own behavior, not assumed.
        # This is the real bug behind the still-empty service list: Phase
        # 28c's flush-on-close safety net only ever ran via closeEvent,
        # which this button never reaches, so it silently never fired for
        # the single most common way this window closes. Overriding
        # accept() directly guarantees the flush actually happens here.
        self._flush_all_rows()
        super().accept()

    def reject(self):
        # Same reasoning as accept() above -- Escape and any programmatic
        # reject() also route through done()/hide(), not close().
        self._flush_all_rows()
        super().reject()

    def closeEvent(self, event):
        # Still kept for the one path that IS a genuine close(): the OS
        # title-bar X / Alt+F4. Harmless if this also runs right after
        # accept()/reject() above (a rare double-flush at most) --
        # _save_service_row() is idempotent, so flushing twice just
        # performs a redundant UPDATE, never a duplicate INSERT.
        self._flush_all_rows()
        super().closeEvent(event)

    # --------------------------------------------------------- summary cards

    def _refresh_cards(self):
        while self.cards_row.count():
            item = self.cards_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        row = db.get_vehicle(self.conn, self.vehicle_id)
        records = db.list_service_records(self.conn, self.vehicle_id)  # oldest first
        last_by_type = {}
        for r in records:
            last_by_type[r["service_type"]] = r  # sorted ascending -> last write wins

        battery_last = last_by_type.get("Battery Change")
        tyre_last = last_by_type.get("Tyre Change")
        oil_last = last_by_type.get("Oil/Filter Change")
        chiller_last = last_by_type.get("Chiller Unit Service")

        cards = [
            # Registration # shown as this card's bottom line (no label,
            # same as every other card's extra_text) -- besides being
            # useful here, this also makes the Vehicle Expiry card the
            # same icon/title/date/extra structure as the other four, so
            # its date lines up at the same height as theirs instead of
            # sitting higher (it previously had no fourth line).
            ("Vehicle Expiry", "vehicle_expiry_icon.jpg", row["vehicle_reg_expiry"],
             row["vehicle_registration"], "#fdeef0"),
            ("Battery Change", "battery_icon.png", battery_last["start_date"] if battery_last else None,
             row["battery_type"], "#eafaf6"),
            ("Tyre Change", "tyre_icon.png", tyre_last["start_date"] if tyre_last else None,
             row["tyre_size"], "#fdf1e8"),
            ("Oil Service", "oilfilter_icon.png", oil_last["start_date"] if oil_last else None,
             f"Next reading: {oil_last['next_reading']}" if oil_last and oil_last["next_reading"] is not None else None,
             "#fdf8e3"),
            ("Chiller Service", "chiller_icon.png", chiller_last["start_date"] if chiller_last else None,
             f"Next reading: {chiller_last['next_reading']}" if chiller_last and chiller_last["next_reading"] is not None else None,
             "#f3eefc"),
        ]

        for title, icon_file, date_iso, extra, bg in cards:
            self.cards_row.addWidget(self._make_card(title, icon_file, date_iso, extra, bg))

    def _make_card(self, title, icon_file, date_iso, extra_text, bg_color):
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(f"QFrame#card {{ background: {bg_color}; border: 1px solid #e2e8f1; border-radius: 12px; }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)

        icon_label = QLabel()
        icon_path = _ASSETS / icon_file
        if icon_path.exists():
            icon_label.setPixmap(QPixmap(str(icon_path)).scaledToHeight(36, Qt.SmoothTransformation))
        layout.addWidget(icon_label, alignment=Qt.AlignCenter)

        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        date_label = QLabel(_display_date(date_iso))
        date_label.setObjectName("cardDate")
        date_label.setAlignment(Qt.AlignCenter)
        if _is_expired(date_iso):
            date_label.setStyleSheet(f"color: {_EXPIRED_COLOR}; font-weight: 700;")
        layout.addWidget(date_label)

        if extra_text:
            extra_label = QLabel(str(extra_text))
            extra_label.setObjectName("cardExtra")
            extra_label.setAlignment(Qt.AlignCenter)
            extra_label.setWordWrap(True)
            layout.addWidget(extra_label)

        return card

    # ------------------------------------------------ service history grid
    # "Continuous Forms" style (project owner's own term, matching MS
    # Access): every row is a live, directly-editable record -- no
    # separate add/edit form. Each row auto-saves to service_records as
    # soon as it's edited (a plain-text cell finishing edit, or the
    # Service Type combo box changing) -- INSERT if the row has no
    # service_records.id yet (stored in column 0's Qt.UserRole), UPDATE
    # otherwise. "+ Add a Record" appends one blank editable row at the
    # bottom; the view always opens scrolled to the bottom (most recent
    # rows), matching the project owner's own reference note ("always
    # show the last rows that fit the window").

    def _reload_service_records(self):
        self._suppress_save = True
        self.service_table.setRowCount(0)
        records = db.list_service_records(self.conn, self.vehicle_id)  # oldest first
        for r in records:
            self._insert_service_row(r)
        self._suppress_save = False
        if self.service_table.rowCount():
            # scrollToBottom() run here (e.g. the __init__ call, before the
            # dialog has ever been shown/laid out) can't reliably reach the
            # true bottom -- the scrollbar's range is computed from the
            # widget's current geometry, which isn't final yet. Deferring
            # via a 0ms QTimer runs it on the next event-loop pass, after
            # layout/show has completed, so it lands on the actual last row
            # instead of stopping partway down.
            QTimer.singleShot(0, self.service_table.scrollToBottom)
        self._refresh_cards()

    def _insert_service_row(self, record=None):
        row = self.service_table.rowCount()
        self.service_table.insertRow(row)
        record_id = record["id"] if record else None

        # A brand-new row (record is None) defaults both dates to today --
        # locks in the correct format and matches the common case of
        # logging a service the day it happens. Existing rows loaded from
        # the DB have their ISO-stored dates converted to DD-MM-YYYY for
        # display here (DB storage itself stays ISO -- see
        # _iso_to_display/_display_to_iso above).
        today_display = date.today().strftime("%d-%m-%Y")
        values = {
            SC_START: _iso_to_display(record["start_date"]) if record else today_display,
            SC_END: _iso_to_display(record["end_date"]) if record else today_display,
            SC_DETAILS: record["details"] if record else "",
            SC_CURRENT: record["current_reading"] if record else None,
            SC_NEXT: record["next_reading"] if record else None,
            SC_QTY: record["qty"] if record else None,
            SC_PERSON: record["person"] if record else "",
            SC_WORKSHOP: record["workshop"] if record else "",
        }
        for col, value in values.items():
            item = QTableWidgetItem("" if value is None else str(value))
            if col == SC_START:
                item.setData(Qt.UserRole, record_id)
            self.service_table.setItem(row, col, item)

        combo = _NoScrollComboBox()
        combo.addItems(SERVICE_TYPES)
        if record and record["service_type"]:
            idx = combo.findText(record["service_type"])
            if idx >= 0:
                combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda _checked=None, r=row: self._save_service_row(r))
        self.service_table.setCellWidget(row, SC_TYPE, combo)
        return row

    def _on_add_record_row(self):
        self._suppress_save = True
        row = self._insert_service_row(record=None)
        self._suppress_save = False
        self.service_table.scrollToBottom()
        self.service_table.setCurrentCell(row, SC_START)
        # Open the Start Date cell for typing immediately, rather than
        # requiring the planner to double-click (or know a keypress alone
        # would also work) before anything they type registers -- a new
        # blank row should be ready to type into right away, matching how
        # a real Access continuous form behaves. Also removes any
        # dependency on edit-trigger detection for the most common path
        # (starting a brand new record) -- editItem() opens the editor
        # directly, unconditionally.
        self.service_table.editItem(self.service_table.item(row, SC_START))

    def _on_service_item_changed(self, item):
        if self._suppress_save:
            return
        self._save_service_row(item.row())

    def _parse_float_or_none(self, text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _cell_text(self, row, col):
        item = self.service_table.item(row, col)
        return item.text().strip() if item else ""

    def _save_service_row(self, row):
        if self._suppress_save or row < 0 or row >= self.service_table.rowCount():
            return

        start_raw = self._cell_text(row, SC_START)
        end_raw = self._cell_text(row, SC_END)
        start_date = _display_to_iso(start_raw)
        end_date = _display_to_iso(end_raw)
        # Lenient, not blocking (this is a live continuous grid, not a
        # modal form with an explicit Save click) -- an unparseable date
        # simply isn't auto-saved yet rather than interrupting typing
        # with a popup; the cell stays editable. The planner types
        # DD-MM-YYYY here; start_date/end_date below are already
        # converted to ISO for storage.
        if start_raw and start_date is None:
            return
        if end_raw and end_date is None:
            return

        combo = self.service_table.cellWidget(row, SC_TYPE)
        service_type = combo.currentText() if combo else ""
        kwargs = dict(
            start_date=start_date,
            end_date=end_date,
            service_type=service_type,
            details=self._cell_text(row, SC_DETAILS),
            current_reading=self._parse_float_or_none(self._cell_text(row, SC_CURRENT)),
            next_reading=self._parse_float_or_none(self._cell_text(row, SC_NEXT)),
            qty=self._parse_float_or_none(self._cell_text(row, SC_QTY)),
            person=self._cell_text(row, SC_PERSON),
            workshop=self._cell_text(row, SC_WORKSHOP),
        )

        id_item = self.service_table.item(row, SC_START)
        record_id = id_item.data(Qt.UserRole) if id_item else None
        if record_id is None:
            new_id = db.add_service_record(self.conn, self.vehicle_id, **kwargs)
            if id_item:
                # setData() on ANY role -- including UserRole, not just the
                # display text -- re-emits itemChanged in Qt. Without
                # blockSignals here, stamping the new id back onto the item
                # immediately re-enters _on_service_item_changed ->
                # _save_service_row for the SAME row while this call is
                # still on the stack -- harmless in that it resolves to a
                # second, redundant UPDATE (record_id is no longer None on
                # the re-entrant call), but wasteful and confusing to trace.
                # Blocked so exactly one write happens per real edit.
                self.service_table.blockSignals(True)
                id_item.setData(Qt.UserRole, new_id)
                self.service_table.blockSignals(False)
        else:
            db.update_service_record(self.conn, record_id, **kwargs)
        self._refresh_cards()

    def _flush_all_rows(self):
        """Force-saves every row's current on-screen values, independent of
        whether itemChanged/currentIndexChanged already fired for it.
        Called before the dialog closes (Close button, Escape, the window
        X) as a safety net -- if a row was edited and the natural
        focus-out/commit sequence didn't fire a save for any reason, this
        guarantees nothing typed is silently lost when the window goes
        away, rather than relying solely on live signal-driven auto-save.

        If a cell editor is still open (the planner typed into a cell but
        never pressed Tab/Enter and never clicked another cell first), its
        text lives only in the editor widget -- the underlying
        QTableWidgetItem.text() below still reads the OLD value until the
        editor commits. Clearing the current cell is what makes Qt commit
        and close a still-open editor (the same thing that happens when a
        user clicks a different cell), so it has to happen before the
        per-row save loop below, not after."""
        if self.service_table.state() == QAbstractItemView.State.EditingState:
            self.service_table.setCurrentCell(-1, -1)
        for row in range(self.service_table.rowCount()):
            self._save_service_row(row)

    def _on_delete_record_row(self):
        row = self.service_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "No selection", "Select a service history row first.")
            return
        id_item = self.service_table.item(row, SC_START)
        record_id = id_item.data(Qt.UserRole) if id_item else None
        if record_id is not None:
            confirm = QMessageBox.question(self, "Delete Record", "Delete this service record?")
            if confirm != QMessageBox.Yes:
                return
            db.delete_service_record(self.conn, record_id)
        self.service_table.removeRow(row)
        self._refresh_cards()
