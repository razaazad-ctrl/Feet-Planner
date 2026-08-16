"""
plan_day_tab.py

The daily workflow screen:
1. Upload the day's request Excel file
2. Optionally type free-text "day notes" -- context for that specific day
   (a VIP event, expected longer waits somewhere, a one-off rule bend for
   a driver, etc.). These notes don't change the deterministic engine's
   hard rules; once the AI layer is built, it reads this note and can
   propose a specific, visible override for the planner to approve or
   reject -- the note itself is just captured and stored for now.
3. Run the (currently rules-only) allocation engine
4. Review the results in a table, with anything unresolved clearly flagged
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit,
    QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox, QHeaderView,
    QScrollArea, QFrame, QComboBox, QDialog, QGridLayout, QProgressBar, QMenu, QCompleter
)
from PySide6.QtGui import QColor, QPixmap, QPainter, QPen, QBrush, QPolygonF, QIcon
from PySide6.QtCore import Qt, QThread, QObject, Signal, QPointF, QSize
from pathlib import Path
from collections import defaultdict
from itertools import combinations

from app import db
from app.excel_import import load_jobs_from_excel, group_jobs_by_event
from app.allocation_engine import (
    allocate_by_solver, build_driver_profiles, build_vehicle_profiles, build_supplier_offerings,
    _group_key_of, _day_span_hours, _type_matches,
)
from app import maps_client
from app import ai_review
from app import export
from app.ui.settings_tab import ANTHROPIC_KEY_SETTING, GOOGLE_MAPS_KEY_SETTING, GEMINI_TEST_KEY_SETTING

UNRESOLVED_COLOR = QColor("#5a2020")
SUPPLIER_COLOR = QColor("#3a3a20")
RECHECK_WARNING_COLOR = QColor("#ff6b6b")
UNASSIGNED_LABEL = "-- Unassigned --"

# Table column layout. The original 8 stay first/visible by default; the 6
# extra "show every Excel column" columns (request #2, /AI/06_NEXT_SESSION.md
# Section 7.2) are appended before Note so Note (which now also carries
# ReCheck warnings) stays the rightmost column.
COL_SR, COL_TIME, COL_EVENT, COL_VEHICLE_TYPE, COL_PICKUP, COL_DRIVER, COL_VEHICLE = range(7)
COL_ORDER_NO, COL_CONTACT, COL_ORDER_LOCATION, COL_ADDITIONAL_INFO, COL_CHARGE_CODE, COL_SAME_DRIVER = range(7, 13)
COL_NOTE = 13
_COLUMN_HEADERS = [
    "SR", "Time", "Event", "Vehicle Type Required", "Pick Up", "Driver / Supplier", "Vehicle / Unit",
    "Order#", "Contact Person", "Order Location", "Additional Info", "Charge Code", "Same Driver", "Note",
]
_DEFAULT_HIDDEN_COLUMNS = {
    COL_ORDER_NO, COL_CONTACT, COL_ORDER_LOCATION, COL_ADDITIONAL_INFO, COL_CHARGE_CODE, COL_SAME_DRIVER,
}
# Default pixel widths so the table looks the same, balanced way on every
# launch instead of Qt's auto-size-from-header-text default (which doesn't
# reflect how much room each column's actual content needs). Note isn't
# listed -- it stretches to fill leftover width via setStretchLastSection.
# SR/Time are deliberately NOT listed here -- their default width is
# computed from real font metrics in _build_ui() instead of a fixed guess,
# so their text is guaranteed to never clip regardless of font/DPI (the
# project owner's explicit requirement -- every other column here is
# allowed to truncate when narrowed, SR/Time are not). Event/Vehicle Type/
# Pick Up narrowed at the project owner's request -- truncation is fine
# for those.
_DEFAULT_COLUMN_WIDTHS = {
    COL_EVENT: 220, COL_VEHICLE_TYPE: 140,
    COL_PICKUP: 160, COL_DRIVER: 210, COL_VEHICLE: 140,
    COL_ORDER_NO: 90, COL_CONTACT: 150, COL_ORDER_LOCATION: 170,
    COL_ADDITIONAL_INFO: 220, COL_CHARGE_CODE: 110, COL_SAME_DRIVER: 220,
}
_CENTERED_COLUMNS = {COL_SR, COL_TIME, COL_PICKUP, COL_ORDER_LOCATION}


def _draw_badge_icon(letter, bg_color, size=18):
    """Small colored circle + single bold letter -- a generic provider
    badge (not a reproduction of any real logo), used on the AI Suggestions
    header to show which backend produced the current suggestions ("A" for
    Anthropic, "G" for Google Gemini) without spelling it out in a full
    text sentence."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(bg_color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(0, 0, size, size)
    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(max(7, int(size * 0.5)))
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, letter)
    painter.end()
    return pixmap


def _draw_warning_triangle_icon(size=18):
    """Small amber warning triangle -- replaces a full-sentence warning
    label on the AI Suggestions header; hover (QToolTip) carries the actual
    text instead, so the warning is still fully readable without
    permanently eating vertical space."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#e8b93a"))
    painter.setPen(Qt.NoPen)
    triangle = QPolygonF([QPointF(size / 2, 1), QPointF(size - 1, size - 2), QPointF(1, size - 2)])
    painter.drawPolygon(triangle)
    painter.setPen(QColor("#1a1a1a"))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(max(7, int(size * 0.4)))
    painter.setFont(font)
    painter.drawText(pixmap.rect().adjusted(0, 2, 0, 0), Qt.AlignCenter, "!")
    painter.end()
    return pixmap


def _draw_wrap_text_icon(size=20):
    """Three horizontal bars (last one shorter) -- a generic "wrapped
    paragraph" glyph, the same idea as Excel's Wrap Text button icon.
    Toggled state (checked/unchecked) is shown by the button itself
    (setCheckable), not by redrawing this icon. Near-white + thicker
    strokes (not the original muted grey) so it's actually visible against
    this app's dark button chrome -- too faint to spot in the first pass."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor("#f2f4f8"), 2.4)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    y1, y2, y3 = size * 0.26, size * 0.52, size * 0.78
    painter.drawLine(QPointF(2, y1), QPointF(size - 2, y1))
    painter.drawLine(QPointF(2, y2), QPointF(size - 2, y2))
    painter.drawLine(QPointF(2, y3), QPointF(size * 0.6, y3))
    painter.end()
    return pixmap


class _NumericTableWidgetItem(QTableWidgetItem):
    """Sorts numerically when both items' text parse as a number, otherwise
    falls back to the normal string comparison. Used for the SR column --
    QTableWidgetItem's default sort compares displayed text as a plain
    string, so ascending SR order came out "1", "10", "11", "12", "2", ...
    instead of 1, 2, 10, 11, 12. Only changes sort order, never the
    displayed text."""

    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except (ValueError, TypeError):
            return super().__lt__(other)


class _NoScrollComboBox(QComboBox):
    """A QComboBox that ignores mouse-wheel events -- same fix as
    vehicle_maintenance_dialog.py's _NoScrollComboBox (duplicated locally
    rather than shared, to avoid new cross-file coupling for one small
    class): a plain QComboBox changes its selection on any wheel scroll
    while the cursor happens to be over it, including just scrolling the
    results table past that row -- which could silently reassign a job to
    the wrong driver/vehicle. Ignoring the event lets Qt propagate it up to
    the table's viewport instead, so table scrolling still works normally."""

    def wheelEvent(self, event):
        event.ignore()


class _SolverWorker(QObject):
    """Runs allocate_by_solver() on a background thread. Added 2026-08-14
    so Run Planning stops blocking the GUI event loop for the whole solve
    (previously up to ~15s) -- that's why the app used to look frozen
    rather than working: nothing could paint or animate while the solve
    ran synchronously on the main thread. Mutates the jobs list it's given
    in place, same as allocate_by_solver() always has; the caller must not
    touch that list again until `finished` fires."""
    finished = Signal(dict)
    missing_dependency = Signal(str)
    failed = Signal(str)

    def __init__(self, jobs, drivers, vehicles, supplier_offerings):
        super().__init__()
        self.jobs = jobs
        self.drivers = drivers
        self.vehicles = vehicles
        self.supplier_offerings = supplier_offerings

    def run(self):
        solver_status = {}
        try:
            allocate_by_solver(
                self.jobs, self.drivers, self.vehicles, self.supplier_offerings,
                solver_status_out=solver_status,
            )
        except ImportError as e:
            self.missing_dependency.emit(str(e))
            return
        except Exception as e:
            # Never let a background-thread exception vanish silently --
            # surface it the same way a synchronous crash would have,
            # rather than leaving the UI stuck in "Running..." forever.
            self.failed.emit(str(e))
            return
        self.finished.emit(solver_status)


class _AIReviewWorker(QObject):
    """Runs the actual AI Review API call (Anthropic or the free Gemini
    testing provider) on a background thread -- same reasoning as
    _SolverWorker above: this is a blocking network call, now made worse
    by ai_review.py's own retry-on-transient-failure loop (up to 3
    attempts with a few seconds' delay between them), so it could
    previously freeze the whole window for well over 10 seconds on a
    single overloaded/rate-limited response. Only the API call itself
    runs here -- the maps_client travel-time lookups that happen before
    it stay on the main thread, since they need self.conn (a sqlite3
    connection, which -- unlike this call -- cannot safely be used from a
    different thread than it was created on)."""
    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, use_gemini, api_key, context):
        super().__init__()
        self.use_gemini = use_gemini
        self.api_key = api_key
        self.context = context

    def run(self):
        try:
            if self.use_gemini:
                suggestions = ai_review.review_plan_gemini(self.api_key, self.context)
            else:
                suggestions = ai_review.review_plan(self.api_key, self.context)
        except ai_review.AIReviewError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.finished.emit(suggestions)


class PlanDayTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.jobs = []
        self.uploaded_path = None
        self.last_drivers = []
        self._run_thread = None
        self._run_worker = None
        self._pending_drivers = None
        self._recheck_issues = {}  # job.sr -> list[str], from the last ReCheck run
        self._ai_review_thread = None
        self._ai_review_worker = None
        # Stashed across the AI Review background call so _on_ai_review_finished/
        # _on_ai_review_failed can get back to them (same pattern as _pending_drivers).
        self._pending_ai_review_state = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        upload_row = QHBoxLayout()
        self.upload_btn = QPushButton("Upload Excel File...")
        self.upload_btn.clicked.connect(self._on_upload)
        self.file_label = QLabel("No file uploaded yet")
        upload_row.addWidget(self.upload_btn)
        upload_row.addWidget(self.file_label, stretch=1)
        layout.addLayout(upload_row)

        day_notes_label = QLabel(
            "Day notes (optional)"
            "<span style=\"color: #888888;\"> -- anything about tomorrow the planner wants considered, "
            "e.g. \"VIP event at Zabeel today, expect longer waits\" or "
            "\"Deepak can go over his usual hours today if needed\":</span>"
        )
        layout.addWidget(day_notes_label)
        self.notes_input = QTextEdit()
        self.notes_input.setPlaceholderText("Type any notes for this planning day here...")
        self.notes_input.setMaximumHeight(48)
        layout.addWidget(self.notes_input)

        run_row = QHBoxLayout()
        self.run_btn = QPushButton("Run Planning")
        self.run_btn.clicked.connect(self._on_run)
        self.run_btn.setEnabled(False)
        self.ai_review_btn = QPushButton("AI Review (event chains + day notes)")
        self.ai_review_btn.clicked.connect(self._on_ai_review)
        self.ai_review_btn.setEnabled(False)
        self.recheck_btn = QPushButton("ReCheck")
        self.recheck_btn.setToolTip(
            "Re-scans the current sheet (including any manual driver/vehicle reassignments) for "
            "clashes and rule breaks -- driver/vehicle double-booking, hours over the hard limits, "
            "and wrong vehicle type -- and flags them in red in the Note column. Never changes any "
            "assignment. Safe to run again after every further edit."
        )
        self.recheck_btn.clicked.connect(self._on_recheck)
        self.recheck_btn.setEnabled(False)
        self.finalize_btn = QPushButton("Finalize Day (save to history)")
        self.finalize_btn.clicked.connect(self._on_finalize)
        self.finalize_btn.setEnabled(False)
        self.export_btn = QPushButton("Export Filled Excel")
        self.export_btn.clicked.connect(self._on_export)
        self.export_btn.setEnabled(False)
        self.summary_btn = QPushButton("Summary")
        self.summary_btn.clicked.connect(self._on_summary)
        self.summary_btn.setEnabled(False)
        self.summary_label = QLabel("")
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.ai_review_btn)
        run_row.addWidget(self.recheck_btn)
        run_row.addWidget(self.finalize_btn)
        run_row.addWidget(self.export_btn)
        run_row.addWidget(self.summary_btn)
        run_row.addWidget(self.summary_label, stretch=1)
        layout.addLayout(run_row)

        # Indeterminate ("busy") progress bar -- Run Planning's solve time
        # (a few seconds, up to a 15s time limit) doesn't have a real linear
        # percentage to report, so a marquee-style bar is the honest choice
        # over a fake determinate one. Hidden until a run is in progress.
        self.run_progress = QProgressBar()
        self.run_progress.setRange(0, 0)
        self.run_progress.setTextVisible(False)
        self.run_progress.setMaximumHeight(6)
        self.run_progress.setVisible(False)
        layout.addWidget(self.run_progress)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter by driver/supplier:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All")
        self.filter_combo.currentTextChanged.connect(self._apply_filters)
        self.filter_combo.setMaximumWidth(220)
        filter_row.addWidget(self.filter_combo)
        filter_row.addWidget(QLabel("Filter by event:"))
        self.event_filter_combo = QComboBox()
        self.event_filter_combo.addItem("All")
        self.event_filter_combo.currentTextChanged.connect(self._apply_filters)
        self.event_filter_combo.setMaximumWidth(220)
        filter_row.addWidget(self.event_filter_combo)
        filter_row.addStretch(1)
        self.wrap_text_btn = QPushButton()
        self.wrap_text_btn.setIcon(QIcon(_draw_wrap_text_icon()))
        self.wrap_text_btn.setIconSize(QSize(20, 20))
        self.wrap_text_btn.setToolTip("Wrap text -- toggle row text between single-line and 2-line wrapped")
        self.wrap_text_btn.setCheckable(True)
        self.wrap_text_btn.setFixedWidth(34)
        self.wrap_text_btn.toggled.connect(self._on_wrap_text_toggled)
        filter_row.addWidget(self.wrap_text_btn)
        self.columns_btn = QPushButton("Columns ▾")
        self.columns_btn.clicked.connect(self._show_columns_menu)
        filter_row.addWidget(self.columns_btn)
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, len(_COLUMN_HEADERS))
        self.table.setHorizontalHeaderLabels(_COLUMN_HEADERS)
        # Every column is manually resizable (Interactive, the default) --
        # the Event/Note columns used to be pinned to Stretch, which blocks
        # drag-resize entirely; stretchLastSection keeps the rightmost
        # visible column filling any leftover width without losing that.
        self.table.horizontalHeader().setStretchLastSection(True)
        # Explicit default widths so the table looks the same, balanced way
        # every time the app launches -- without this, Qt falls back to
        # auto-sizing from header text alone, which doesn't match how much
        # content each column actually needs (approximated from a real
        # 81-row sheet, not guessed from header text length).
        for col, width in _DEFAULT_COLUMN_WIDTHS.items():
            self.table.horizontalHeader().resizeSection(col, width)
        # SR and Time must always show their full text (the project owner's
        # explicit requirement, unlike the other columns above) -- computed
        # from the table's real font metrics rather than a fixed guess, so
        # it stays correct regardless of font/DPI. "999" covers any
        # realistic SR; the time range is always the fixed "HH:MM - HH:MM"
        # shape, so its widest real value is exactly this length.
        fm = self.table.fontMetrics()
        self.table.horizontalHeader().resizeSection(COL_SR, fm.horizontalAdvance("999") + 26)
        self.table.horizontalHeader().resizeSection(COL_TIME, fm.horizontalAdvance("23:59 - 23:59") + 20)
        for col in _DEFAULT_HIDDEN_COLUMNS:
            self.table.setColumnHidden(col, True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._rebind_row_widgets)
        self._wrap_text_enabled = False
        self._default_row_height = self.table.verticalHeader().defaultSectionSize()
        layout.addWidget(self.table)

        # Wrapped in its own widget (rather than adding the label/scroll area
        # directly to `layout`) so the whole section can be shown/hidden as
        # one unit -- starts hidden to give the results table more row
        # space, and only reveals itself once AI Review is actually used
        # (see _on_ai_review). A hidden QWidget takes no layout space, so
        # hiding this doesn't leave a gap.
        self.suggestions_section = QWidget()
        suggestions_section_layout = QVBoxLayout(self.suggestions_section)
        suggestions_section_layout.setContentsMargins(0, 0, 0, 0)

        # Header row: label on the left, then (only when relevant) a small
        # warning-triangle icon and a provider badge on the far right --
        # replaces what used to be full-sentence warning labels eating a
        # whole row each; the same information is now a hover tooltip.
        suggestions_header_row = QHBoxLayout()
        suggestions_header_row.addWidget(QLabel("AI Suggestions (accept or reject each one):"))
        suggestions_header_row.addStretch(1)
        self.suggestions_warning_icon = QLabel()
        self.suggestions_warning_icon.setPixmap(_draw_warning_triangle_icon())
        self.suggestions_warning_icon.setVisible(False)
        suggestions_header_row.addWidget(self.suggestions_warning_icon)
        self.suggestions_provider_icon = QLabel()
        self.suggestions_provider_icon.setVisible(False)
        suggestions_header_row.addWidget(self.suggestions_provider_icon)
        suggestions_section_layout.addLayout(suggestions_header_row)

        self.suggestions_container = QVBoxLayout()
        self.suggestions_container.setSpacing(6)
        suggestions_scroll_widget = QWidget()
        suggestions_scroll_widget.setLayout(self.suggestions_container)
        self.suggestions_scroll = QScrollArea()
        self.suggestions_scroll.setWidgetResizable(True)
        self.suggestions_scroll.setWidget(suggestions_scroll_widget)
        # Tall enough for ~3 compact suggestion cards at once (see the
        # tightened margins/spacing in _add_suggestion_widget) instead of
        # the ~1 that fit before.
        self.suggestions_scroll.setMaximumHeight(300)
        suggestions_section_layout.addWidget(self.suggestions_scroll)
        self.no_suggestions_label = QLabel("Run planning, then click \"AI Review\" to see suggestions here.")
        self.no_suggestions_label.setStyleSheet("color: #888888;")
        self.suggestions_container.addWidget(self.no_suggestions_label)
        self.suggestions_section.setVisible(False)
        layout.addWidget(self.suggestions_section)

    def _on_upload(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Requests Excel File", "", "Excel Files (*.xlsx)")
        if not path:
            return
        try:
            jobs = load_jobs_from_excel(path)
        except Exception as e:
            QMessageBox.warning(self, "Error reading file", f"Could not read this file:\n{e}")
            return
        if not jobs:
            QMessageBox.information(self, "No rows found",
                                     "No job rows were recognized in this file. Check the column headers match the expected format.")
            return
        self.jobs = jobs
        self.uploaded_path = path
        self.file_label.setText(f"{path.split('/')[-1].split(chr(92))[-1]}  —  {len(jobs)} jobs loaded")
        self.run_btn.setEnabled(True)
        self.summary_label.setText("")
        self._recheck_issues = {}
        self.table.setRowCount(0)

    def _on_run(self):
        drivers = build_driver_profiles(self.conn, db)
        vehicles = build_vehicle_profiles(self.conn, db)
        supplier_offerings = build_supplier_offerings(self.conn, db)

        if not drivers and not supplier_offerings:
            QMessageBox.information(
                self, "No master data",
                "No drivers or supplier offerings are set up yet. Add them in the Drivers/Suppliers "
                "tabs first, then come back and run planning."
            )
            return

        # Immediate feedback (within a second of the click) BEFORE the solve
        # even starts, then the actual solve runs on a background thread so
        # this bar can genuinely animate instead of the whole window
        # freezing for the next few seconds.
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Running...")
        self.upload_btn.setEnabled(False)
        self.run_progress.setVisible(True)
        self.summary_label.setStyleSheet("")
        self.summary_label.setText("Planning your day... this can take a few seconds.")

        self._pending_drivers = drivers
        self._run_thread = QThread(self)
        self._run_worker = _SolverWorker(self.jobs, drivers, vehicles, supplier_offerings)
        self._run_worker.moveToThread(self._run_thread)
        self._run_thread.started.connect(self._run_worker.run)
        self._run_worker.finished.connect(self._on_run_finished)
        self._run_worker.missing_dependency.connect(self._on_run_missing_dependency)
        self._run_worker.failed.connect(self._on_run_failed)
        for signal in (self._run_worker.finished, self._run_worker.missing_dependency, self._run_worker.failed):
            signal.connect(self._run_thread.quit)
            signal.connect(self._run_worker.deleteLater)
        self._run_thread.finished.connect(self._run_thread.deleteLater)
        self._run_thread.start()

    def _reset_run_controls(self):
        self.run_progress.setVisible(False)
        self.run_btn.setText("Run Planning")
        self.run_btn.setEnabled(True)
        self.upload_btn.setEnabled(True)

    def _on_run_finished(self, solver_status):
        self.last_drivers = self._pending_drivers
        self._pending_drivers = None
        self._reset_run_controls()

        self._recheck_issues = {}
        self._render_results()
        self.ai_review_btn.setEnabled(True)
        self.recheck_btn.setEnabled(True)
        self.finalize_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.summary_btn.setEnabled(True)

        unresolved_count = sum(1 for j in self.jobs if j.unresolved)
        in_house_count = sum(1 for j in self.jobs if j.assigned_driver_id is not None)
        supplier_count = sum(1 for j in self.jobs if j.assigned_supplier_unit is not None)

        # Surface OPTIMAL vs. FEASIBLE plainly (Rule 8, explainable decisions):
        # OPTIMAL means a mathematical guarantee no better in-house plan exists
        # under the hard rules; FEASIBLE means the 15-second time limit was hit
        # and this is only the best plan found so far, not a proven best one.
        status_word = solver_status.get("status", "")
        status_text = {
            "OPTIMAL": "Solved optimally (proven best plan).",
            "FEASIBLE": "Best plan found within the time limit — may not be the true optimum.",
        }.get(status_word, status_word)

        self.summary_label.setText(
            f"{len(self.jobs)} jobs total  |  {in_house_count} in-house  |  "
            f"{supplier_count} supplier  |  {unresolved_count} unresolved  |  {status_text}"
        )
        self.summary_label.setStyleSheet("color: #a08030;" if status_word == "FEASIBLE" else "")
        if self.notes_input.toPlainText().strip():
            note_preview = self.notes_input.toPlainText().strip()
            self.summary_label.setText(
                self.summary_label.text() + "   (day notes recorded — will be used once the AI review layer is added)"
            )

    def _on_run_missing_dependency(self, detail):
        self._pending_drivers = None
        self._reset_run_controls()
        self.summary_label.setText("")
        QMessageBox.critical(
            self, "Planning engine not installed",
            "Run Planning needs the 'ortools' package, which isn't installed on this "
            "computer.\n\nAsk whoever set up this app to run:\n\n    pip install ortools\n\n"
            "then try Run Planning again.\n\n" + detail
        )

    def _on_run_failed(self, detail):
        self._pending_drivers = None
        self._reset_run_controls()
        self.summary_label.setText("")
        QMessageBox.critical(self, "Run Planning failed", f"Run Planning failed unexpectedly:\n\n{detail}")

    def _on_ai_review(self):
        anthropic_key = db.get_setting(self.conn, ANTHROPIC_KEY_SETTING)
        gemini_key = db.get_setting(self.conn, GEMINI_TEST_KEY_SETTING)
        maps_key = db.get_setting(self.conn, GOOGLE_MAPS_KEY_SETTING)

        if not anthropic_key and not gemini_key:
            QMessageBox.information(
                self, "No AI key configured",
                "Add your Anthropic API key in the Settings tab first -- or, for free testing, "
                "a Google Gemini key in the same tab's \"Free/Testing AI Provider\" section."
            )
            return

        # AI Suggestions starts hidden (more row space for the results
        # table) and only reveals itself once AI Review is actually used.
        self.suggestions_section.setVisible(True)

        event_groups = group_jobs_by_event(self.jobs)
        multi_stage_groups = {eid: g for eid, g in event_groups.items() if len(g) >= 2}

        # Travel-time lookups stay on the main thread (below, synchronous):
        # they need self.conn (a sqlite3 connection, which can only safely
        # be used from the thread it was created on) via db.resolve_location.
        #
        # Every lookup goes through db.travel_time_cache FIRST (2026-08-16) --
        # the same cache the Locations/map screen fills. So if the planner
        # already ran that screen for this day, AI Review costs zero extra
        # API calls, and vice versa: the two features share one cache
        # instead of each paying separately for identical routes.
        travel_lookups = {}
        maps_warning = ""
        if maps_key:
            for event_id, stage_jobs in multi_stage_groups.items():
                for prev_job, next_job in zip(stage_jobs, stage_jobs[1:]):
                    origin_raw = prev_job.order_location or prev_job.pickup_location
                    destination_raw = next_job.pickup_location or next_job.order_location
                    if not origin_raw or not destination_raw or not prev_job.end_dt:
                        continue
                    origin = db.resolve_location(self.conn, origin_raw)
                    destination = db.resolve_location(self.conn, destination_raw)
                    key = f"SR{prev_job.sr} -> SR{next_job.sr}"
                    confidence = "exact" if (origin["exact"] and destination["exact"]) else "approximate (area-level)"
                    hour = prev_job.end_dt.hour
                    cached = db.get_cached_travel_time(
                        self.conn, origin["address"], destination["address"], hour
                    )
                    if cached is not None:
                        travel_lookups[key] = {
                            "duration_minutes": cached["duration_minutes"],
                            "confidence": confidence,
                        }
                        continue
                    try:
                        result = maps_client.get_travel_time(
                            maps_key, origin["address"], destination["address"], prev_job.end_dt
                        )
                        db.save_travel_time(
                            self.conn, origin["address"], destination["address"], hour,
                            result["duration_minutes"], result.get("distance_km"),
                            result.get("polyline"),
                        )
                        travel_lookups[key] = {
                            "duration_minutes": result["duration_minutes"],
                            "confidence": confidence,
                        }
                    except maps_client.MapsClientError as e:
                        travel_lookups[key] = {"error": str(e), "confidence": confidence}
        else:
            maps_warning = "(No Google Maps key set — proceeding without real travel-time data.)\n"

        # Per-driver connection feasibility, built purely from what's ALREADY
        # cached -- never triggers a fetch of its own, so enabling this cost
        # nothing. Gives the AI real geography across each driver's whole day
        # (not just within event chains): where a driver physically cannot
        # make the next pickup in the gap available.
        driver_chain_gaps = self._build_driver_chain_gaps()

        driver_hours_summary = {d.name: round(d.occupied_seconds / 3600.0, 1) for d in self.last_drivers}
        day_notes = self.notes_input.toPlainText().strip()

        digest_row = db.get_digest(self.conn)
        preferences_digest = digest_row["digest_text"] if digest_row else ""

        context = ai_review.build_review_context(
            self.jobs, multi_stage_groups, driver_hours_summary, travel_lookups, day_notes,
            preferences_digest=preferences_digest,
            driver_chain_gaps=driver_chain_gaps,
        )

        # The actual AI API call (the slow, blocking part -- now with up to
        # 3 retries on a transient failure, several seconds apart) runs on
        # a background thread via _AIReviewWorker, same reasoning and same
        # pattern as Run Planning's _SolverWorker: without this, a single
        # overloaded/rate-limited response could freeze the whole window
        # for 10+ seconds.
        self._pending_ai_review_state = {
            "anthropic_key": anthropic_key,
            "maps_warning": maps_warning,
        }
        self.ai_review_btn.setEnabled(False)
        self.ai_review_btn.setText("Reviewing...")
        self.run_btn.setEnabled(False)
        self.upload_btn.setEnabled(False)
        self.run_progress.setVisible(True)

        self._ai_review_thread = QThread(self)
        self._ai_review_worker = _AIReviewWorker(not anthropic_key, anthropic_key or gemini_key, context)
        self._ai_review_worker.moveToThread(self._ai_review_thread)
        self._ai_review_thread.started.connect(self._ai_review_worker.run)
        self._ai_review_worker.finished.connect(self._on_ai_review_finished)
        self._ai_review_worker.failed.connect(self._on_ai_review_failed)
        for signal in (self._ai_review_worker.finished, self._ai_review_worker.failed):
            signal.connect(self._ai_review_thread.quit)
            signal.connect(self._ai_review_worker.deleteLater)
        self._ai_review_thread.finished.connect(self._ai_review_thread.deleteLater)
        self._ai_review_thread.start()

    def _build_driver_chain_gaps(self):
        """Each driver's consecutive job-to-job connections, with the gap
        available vs. the real drive time between those two places -- for
        the AI Review context (2026-08-16).

        Reads the travel-time cache ONLY -- never fetches. A connection with
        no cached route is simply omitted rather than triggering a paid
        lookup, so turning this on added zero API cost; it gets richer
        automatically as the Locations/map screen fills the cache.
        """
        by_driver = {}
        for job in self.jobs:
            if job.start_dt is None or job.end_dt is None or job.assigned_driver_id is None:
                continue
            by_driver.setdefault(job.assigned_driver_name or "driver", []).append(job)

        gaps = {}
        for driver, driver_jobs in sorted(by_driver.items()):
            driver_jobs.sort(key=lambda j: j.start_dt)
            legs = []
            for prev_job, next_job in zip(driver_jobs, driver_jobs[1:]):
                origin_raw = prev_job.order_location or prev_job.pickup_location
                destination_raw = next_job.pickup_location or next_job.order_location
                if not origin_raw or not destination_raw:
                    continue
                origin = db.resolve_location(self.conn, origin_raw)["address"]
                destination = db.resolve_location(self.conn, destination_raw)["address"]
                gap_minutes = round((next_job.start_dt - prev_job.end_dt).total_seconds() / 60.0, 1)
                if origin == destination:
                    drive_minutes = 0.0   # same place, no drive needed
                else:
                    cached = db.get_cached_travel_time(
                        self.conn, origin, destination, prev_job.end_dt.hour
                    )
                    if cached is None:
                        continue          # not cached -> skip, never fetch here
                    drive_minutes = cached["duration_minutes"]
                legs.append({
                    "from_sr": prev_job.sr,
                    "to_sr": next_job.sr,
                    "gap_minutes": gap_minutes,
                    "drive_minutes": drive_minutes,
                    "feasible": drive_minutes is not None and drive_minutes <= gap_minutes,
                })
            if legs:
                gaps[driver] = legs
        return gaps

    def _reset_ai_review_controls(self):
        self.run_progress.setVisible(False)
        self.ai_review_btn.setText("AI Review (event chains + day notes)")
        self.ai_review_btn.setEnabled(True)
        self.run_btn.setEnabled(True)
        self.upload_btn.setEnabled(True)

    def _on_ai_review_finished(self, suggestions):
        state = self._pending_ai_review_state
        self._pending_ai_review_state = None
        self._reset_ai_review_controls()

        anthropic_key = state["anthropic_key"]
        maps_warning = state["maps_warning"]

        self._clear_suggestions()

        # Provider badge -- which backend actually produced these
        # suggestions (top-right of the AI Suggestions header).
        if anthropic_key:
            self.suggestions_provider_icon.setPixmap(_draw_badge_icon("A", "#d97757"))
            self.suggestions_provider_icon.setToolTip("Suggestions generated by Anthropic Claude")
        else:
            self.suggestions_provider_icon.setPixmap(_draw_badge_icon("G", "#4285f4"))
            self.suggestions_provider_icon.setToolTip("Suggestions generated by Google Gemini (free/testing provider)")
        self.suggestions_provider_icon.setVisible(True)

        # Warning triangle -- combines whatever caveats apply into one
        # hover tooltip instead of permanent full-sentence labels.
        warning_parts = []
        if maps_warning:
            warning_parts.append(maps_warning.strip())
        if not anthropic_key:
            warning_parts.append(
                "Using the free Google Gemini testing provider, not Anthropic -- suggestion quality "
                "may be weaker. Add an Anthropic key in Settings for the real thing."
            )
        if warning_parts:
            self.suggestions_warning_icon.setToolTip("\n\n".join(warning_parts))
            self.suggestions_warning_icon.setVisible(True)
        else:
            self.suggestions_warning_icon.setVisible(False)

        if not suggestions:
            label = QLabel("No suggestions — the current plan looks fine as is.")
            self.suggestions_container.addWidget(label)
            return

        plan_date = self.jobs[0].date.isoformat() if self.jobs and self.jobs[0].date else ""
        for s in suggestions:
            self._add_suggestion_widget(s, plan_date)

    def _on_ai_review_failed(self, detail):
        self._pending_ai_review_state = None
        self._reset_ai_review_controls()
        QMessageBox.warning(self, "AI Review failed", detail)

    def _clear_suggestions(self):
        while self.suggestions_container.count():
            item = self.suggestions_container.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _add_suggestion_widget(self, suggestion, plan_date):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame_layout = QVBoxLayout(frame)
        # Tightened from Qt's default (~9px margins, ~11px spacing) so
        # roughly 3 cards fit in the scroll area at once instead of ~1.
        frame_layout.setContentsMargins(8, 5, 8, 5)
        frame_layout.setSpacing(3)

        jobs_str = ", ".join(f"SR{j}" for j in suggestion.get("affected_jobs", []))
        header = QLabel(f"[{suggestion.get('type', 'suggestion')}] {jobs_str}")
        header.setStyleSheet("font-weight: bold;")
        frame_layout.addWidget(header)

        reasoning_label = QLabel(suggestion.get("reasoning", ""))
        reasoning_label.setWordWrap(True)
        frame_layout.addWidget(reasoning_label)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        accept_btn = QPushButton("Accept")
        reject_btn = QPushButton("Reject")
        status_label = QLabel("")
        btn_row.addWidget(accept_btn)
        btn_row.addWidget(reject_btn)
        btn_row.addWidget(status_label, stretch=1)
        frame_layout.addLayout(btn_row)

        def make_handler(action):
            def handler():
                db.log_decision(
                    self.conn, plan_date,
                    suggestion.get("affected_jobs", []),
                    suggestion.get("type", "suggestion"),
                    suggestion.get("reasoning", ""),
                    action,
                )
                status_label.setText(f"{action.capitalize()} — logged")
                accept_btn.setEnabled(False)
                reject_btn.setEnabled(False)
            return handler

        accept_btn.clicked.connect(make_handler("accepted"))
        reject_btn.clicked.connect(make_handler("rejected"))

        self.suggestions_container.addWidget(frame)

    def _on_summary(self):
        if not self.jobs:
            return
        dialog = DriverSupplierSummaryDialog(self.jobs, self.last_drivers, self)
        dialog.exec()

    def _on_export(self):
        if not self.jobs or not self.uploaded_path:
            return
        suggested_name = self.uploaded_path.rsplit(".", 1)[0] + "_PLANNED.xlsx"
        output_path, _ = QFileDialog.getSaveFileName(self, "Save Filled Excel", suggested_name, "Excel Files (*.xlsx)")
        if not output_path:
            return
        try:
            export.export_filled_excel(self.uploaded_path, self.jobs, output_path)
        except ValueError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, "Export failed", f"Could not save the file: {e}")
            return
        QMessageBox.information(self, "Exported", f"Saved to:\n{output_path}\n\nOnly the Vehicle and Driver columns were changed -- everything else matches your original file exactly.")

    def _on_finalize(self):
        if not self.jobs:
            return
        plan_date = None
        for j in self.jobs:
            if j.date:
                plan_date = j.date.isoformat()
                break
        if not plan_date:
            QMessageBox.warning(self, "Cannot finalize", "No valid date found in this plan.")
            return

        confirm = QMessageBox.question(
            self, "Finalize Day",
            f"Save this plan for {plan_date} to history? This is what future monthly overtime "
            f"checks and supplier fairness will be based on. Re-finalizing the same date later "
            f"will overwrite it."
        )
        if confirm != QMessageBox.Yes:
            return

        job_rows = []
        for j in self.jobs:
            if j.unresolved or not j.start_dt or not j.end_dt:
                continue
            hours = (j.end_dt - j.start_dt).total_seconds() / 3600.0
            job_rows.append({
                "sr": j.sr,
                "driver_id": j.assigned_driver_id,
                "vehicle_id": j.assigned_vehicle_id,
                "supplier_id": j.assigned_supplier_id,
                "supplier_label": j.assigned_supplier_unit,
                "start_dt": j.start_dt.isoformat(),
                "end_dt": j.end_dt.isoformat(),
                "hours": hours,
                # Context snapshots for the Schedules tab (2026-08-16) --
                # captured once, here, so a finalized day's record stays
                # self-contained/readable even if the driver/vehicle is
                # later renamed or removed from the roster. Already on the
                # Job object, no extra query needed.
                "event_text": j.event_text,
                "pickup_location": j.pickup_location,
                "vehicle_type_required": j.vehicle_type_required,
                "driver_name": j.assigned_driver_name,
                "vehicle_plate": j.assigned_vehicle_plate,
                # Caught as a real gap the same day ("for complete reference
                # of past"): these four (plus charge_code/same_driver_key)
                # were missed from the first pass above, even though
                # they're already Job fields shown as optional Plan a Day
                # columns.
                "order_no": j.order_no,
                "contact_person": j.contact_person,
                "order_location": j.order_location,
                "additional_info": j.additional_info,
                "charge_code": j.charge_code,
                "same_driver_key": j.same_driver_key,
            })

        db.save_finalized_jobs(self.conn, plan_date, job_rows)
        QMessageBox.information(self, "Finalized", f"Saved {len(job_rows)} assignments to history for {plan_date}.")

    def _load_reassignment_options(self):
        """Refreshes the driver/vehicle/supplier lists the reassignment combo
        boxes are built from. Deliberately unfiltered by excluded_from_planning
        (confirmed with the project owner: the planner must be able to
        deliberately pull an off-day driver, or an excluded vehicle, onto a
        busy day) -- active_only=True (the default) only filters each
        table's own 'active' (still-on-roster) flag."""
        self._driver_rows = list(db.list_drivers(self.conn))
        self._supplier_rows = list(db.list_suppliers(self.conn))
        self._vehicle_rows = list(db.list_vehicles(self.conn))
        self._driver_id_by_name = {r["name"]: r["id"] for r in self._driver_rows}
        self._supplier_id_by_name = {r["name"]: r["id"] for r in self._supplier_rows}
        self._vehicle_id_by_plate = {r["plate"]: r["id"] for r in self._vehicle_rows}
        self._vehicle_type_by_id = {r["id"]: r["vehicle_type"] for r in self._vehicle_rows}

    def _driver_supplier_combo_items(self):
        return (
            [UNASSIGNED_LABEL]
            + sorted(self._driver_id_by_name, key=str.upper)
            + sorted(self._supplier_id_by_name, key=str.upper)
        )

    def _vehicle_combo_items(self):
        return [UNASSIGNED_LABEL] + sorted(self._vehicle_id_by_plate, key=str.upper)

    def _make_editable_combo(self, items, current_text):
        """A type-ahead-filtered, free-text-capable combo box: lists every
        option with no restriction (the planner can assign anything,
        including an off-day driver -- confirmed), but also lets typing the
        first few letters narrow the popup instead of scrolling a long list,
        and lets the planner type an exact label (e.g. a specific supplier
        hired-unit number) that isn't in the base list."""
        combo = _NoScrollComboBox()
        combo.addItems(items)
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.lineEdit().setAlignment(Qt.AlignLeft)
        completer = combo.completer()
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setFilterMode(Qt.MatchStartsWith)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        idx = combo.findText(current_text)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(current_text)
        # A long name in a narrow column otherwise scrolls to show the END
        # of the text (the QLineEdit cursor defaults to the end after
        # setEditText/setCurrentIndex) -- the planner needs to see the
        # START of the name (e.g. the supplier) to identify the row at a
        # glance, so pin the visible scroll position back to the start.
        combo.lineEdit().setCursorPosition(0)
        return combo

    def _row_tint(self, job):
        if job.unresolved:
            return UNRESOLVED_COLOR
        if job.assigned_supplier_unit:
            return SUPPLIER_COLOR
        return None

    def _note_display(self, job):
        """(text, is_warning) for the Note column -- ReCheck results take
        priority when present (in red), otherwise the engine's own
        assignment_note is shown unchanged. ReCheck never mutates
        assignment_note itself, only this display."""
        issues = self._recheck_issues.get(job.sr)
        if issues:
            return "; ".join(issues), True
        return job.assignment_note, False

    def _render_results(self):
        self.table.setSortingEnabled(False)  # must be off while populating, or rows scramble mid-insert
        self.table.setRowCount(0)
        self._load_reassignment_options()
        driver_combo_items = self._driver_supplier_combo_items()
        vehicle_combo_items = self._vehicle_combo_items()

        for job in self.jobs:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._populate_row(row, job, driver_combo_items, vehicle_combo_items)
        self.table.setSortingEnabled(True)
        if self._wrap_text_enabled:
            self.table.resizeRowsToContents()

        self._refresh_filter_choices()

    def _populate_row(self, row, job, driver_combo_items, vehicle_combo_items):
        tint = self._row_tint(job)

        time_str = ""
        if job.start_dt and job.end_dt:
            time_str = f"{job.start_dt.strftime('%H:%M')} - {job.end_dt.strftime('%H:%M')}"

        driver_or_supplier = UNASSIGNED_LABEL
        vehicle_or_unit = UNASSIGNED_LABEL
        if job.assigned_driver_id is not None:
            driver_or_supplier = job.assigned_driver_name or UNASSIGNED_LABEL
            vehicle_or_unit = job.assigned_vehicle_plate or UNASSIGNED_LABEL
        elif job.assigned_supplier_unit:
            driver_or_supplier = job.assigned_supplier_unit
            vehicle_or_unit = job.assigned_supplier_unit

        note_text, note_is_warning = self._note_display(job)

        plain_values = {
            COL_SR: job.sr, COL_TIME: time_str, COL_EVENT: job.event_text,
            COL_VEHICLE_TYPE: job.vehicle_type_required, COL_PICKUP: job.pickup_location,
            COL_ORDER_NO: job.order_no, COL_CONTACT: job.contact_person,
            COL_ORDER_LOCATION: job.order_location, COL_ADDITIONAL_INFO: job.additional_info,
            COL_CHARGE_CODE: job.charge_code, COL_SAME_DRIVER: job.same_driver_key,
            COL_NOTE: note_text,
        }
        for col, val in plain_values.items():
            item = _NumericTableWidgetItem(str(val)) if col == COL_SR else QTableWidgetItem(str(val))
            if col in _CENTERED_COLUMNS:
                item.setTextAlignment(Qt.AlignCenter)
            if col == COL_NOTE and note_is_warning:
                item.setForeground(RECHECK_WARNING_COLOR)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            elif tint is not None:
                item.setBackground(tint)
            self.table.setItem(row, col, item)

        driver_combo = self._make_editable_combo(driver_combo_items, driver_or_supplier)
        driver_combo.currentTextChanged.connect(lambda text, j=job: self._on_driver_combo_changed(j, text))
        vehicle_combo = self._make_editable_combo(vehicle_combo_items, vehicle_or_unit)
        vehicle_combo.currentTextChanged.connect(lambda text, j=job: self._on_vehicle_combo_changed(j, text))
        if tint is not None:
            for combo in (driver_combo, vehicle_combo):
                combo.setStyleSheet(f"QComboBox {{ background-color: {tint.name()}; }}")
        self.table.setCellWidget(row, COL_DRIVER, driver_combo)
        self.table.setCellWidget(row, COL_VEHICLE, vehicle_combo)

    def _job_by_sr(self, sr):
        for j in self.jobs:
            if j.sr == sr:
                return j
        return None

    def _on_driver_combo_changed(self, job, text):
        text = text.strip()
        if text == "" or text == UNASSIGNED_LABEL:
            job.assigned_driver_id = None
            job.assigned_driver_name = ""
            job.assigned_supplier_id = None
            job.assigned_supplier_unit = None
            job.unresolved = True
        elif text in self._driver_id_by_name:
            job.assigned_driver_id = self._driver_id_by_name[text]
            job.assigned_driver_name = text
            job.assigned_supplier_id = None
            job.assigned_supplier_unit = None
            job.unresolved = False
        else:
            # A supplier name/label -- either a known base supplier name, or
            # a hand-typed hired-unit label (e.g. "ABC Rentals 2").
            job.assigned_driver_id = None
            job.assigned_driver_name = ""
            job.assigned_vehicle_id = None
            job.assigned_vehicle_plate = ""
            job.assigned_supplier_id = self._supplier_id_by_name.get(
                text.rstrip("0123456789").strip(), self._supplier_id_by_name.get(text)
            )
            job.assigned_supplier_unit = text
            job.unresolved = False
            # Convenience sync: mirror the supplier label into the Vehicle/Unit
            # cell too (matches the existing on-screen convention that a
            # supplier's name shows in both columns) -- the planner can still
            # edit it separately afterward.
            self._set_vehicle_cell_text(job, text)
        job.assignment_note = "Manually reassigned by planner"
        self._retint_job_row(job)
        self._refresh_filter_choices()

    def _on_vehicle_combo_changed(self, job, text):
        text = text.strip()
        if text == "" or text == UNASSIGNED_LABEL:
            job.assigned_vehicle_id = None
            job.assigned_vehicle_plate = ""
        elif text in self._vehicle_id_by_plate:
            job.assigned_vehicle_id = self._vehicle_id_by_plate[text]
            job.assigned_vehicle_plate = text
        else:
            # Free-typed text on a supplier row (vehicle_plate isn't used for
            # supplier rows -- assigned_supplier_unit already carries the label).
            job.assigned_vehicle_id = None
            job.assigned_vehicle_plate = ""
        job.assignment_note = "Manually reassigned by planner"
        self._retint_job_row(job)

    def _set_vehicle_cell_text(self, job, text):
        for row in range(self.table.rowCount()):
            sr_item = self.table.item(row, COL_SR)
            if sr_item is not None and sr_item.text() == job.sr:
                combo = self.table.cellWidget(row, COL_VEHICLE)
                if combo is not None:
                    combo.blockSignals(True)
                    idx = combo.findText(text)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                    else:
                        combo.setEditText(text)
                    combo.lineEdit().setCursorPosition(0)
                    combo.blockSignals(False)
                return

    def _retint_job_row(self, job):
        """Refreshes one row's background tint + Note cell in place after a
        manual reassignment, without rebuilding the whole table (keeps scroll
        position/sort order intact -- same reasoning as the Vehicle
        Maintenance Log's card-refresh, see vehicle_maintenance_dialog.py)."""
        tint = self._row_tint(job)
        for row in range(self.table.rowCount()):
            sr_item = self.table.item(row, COL_SR)
            if sr_item is None or sr_item.text() != job.sr:
                continue
            note_text, note_is_warning = self._note_display(job)
            for col in range(self.table.columnCount()):
                if col in (COL_DRIVER, COL_VEHICLE):
                    combo = self.table.cellWidget(row, col)
                    if combo is not None:
                        combo.setStyleSheet(
                            f"QComboBox {{ background-color: {tint.name()}; }}" if tint is not None else ""
                        )
                    continue
                item = self.table.item(row, col)
                if item is None:
                    continue
                if col == COL_NOTE:
                    item.setText(note_text)
                    font = item.font()
                    if note_is_warning:
                        item.setForeground(RECHECK_WARNING_COLOR)
                        font.setBold(True)
                    else:
                        item.setData(Qt.ForegroundRole, None)  # back to the default (theme) text color
                        font.setBold(False)
                    item.setFont(font)
                if tint is not None:
                    item.setBackground(tint)
                else:
                    item.setData(Qt.BackgroundRole, None)  # back to the default (theme) background
            return

    def _rebind_row_widgets(self, *_args):
        """QTableWidget cell widgets don't automatically follow Qt's built-in
        row sort -- re-run after every header-click sort so each row's combo
        boxes stay attached to the correct job (looked up by SR, not by row
        position).

        Sorting MUST be off for the whole loop, not just the initial
        population in _render_results: with sortingEnabled still True, each
        setItem() call inside _populate_row() can trigger Qt to re-sort the
        table again immediately (it auto-resorts on any data change while
        sorting is enabled) -- mid-loop, out from under `range(rowCount())`,
        which was corrupting row/job association (a row's SR cell would show
        one job's SR while its other cells belonged to a different job)."""
        self.table.setSortingEnabled(False)
        driver_combo_items = self._driver_supplier_combo_items()
        vehicle_combo_items = self._vehicle_combo_items()
        for row in range(self.table.rowCount()):
            sr_item = self.table.item(row, COL_SR)
            if sr_item is None:
                continue
            job = self._job_by_sr(sr_item.text())
            if job is None:
                continue
            self._populate_row(row, job, driver_combo_items, vehicle_combo_items)
        self.table.setSortingEnabled(True)
        if self._wrap_text_enabled:
            self.table.resizeRowsToContents()

    def _refresh_filter_choices(self):
        driver_supplier_values = set()
        event_values = set()
        for job in self.jobs:
            label = ""
            if job.assigned_driver_id is not None:
                label = job.assigned_driver_name
            elif job.assigned_supplier_unit:
                label = job.assigned_supplier_unit.removeprefix("SAME ")
            if label:
                driver_supplier_values.add(label)
            if job.event_text:
                event_values.add(job.event_text)

        for combo, values in (
            (self.filter_combo, driver_supplier_values),
            (self.event_filter_combo, event_values),
        ):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All")
            for v in sorted(values):
                combo.addItem(v)
            combo.setCurrentText(current if combo.findText(current) >= 0 else "All")
            combo.blockSignals(False)

    def _apply_filters(self, *_args):
        driver_filter = self.filter_combo.currentText()
        event_filter = self.event_filter_combo.currentText()
        for row in range(self.table.rowCount()):
            driver_combo = self.table.cellWidget(row, COL_DRIVER)
            driver_text = driver_combo.currentText().removeprefix("SAME ") if driver_combo is not None else ""
            event_item = self.table.item(row, COL_EVENT)
            event_text = event_item.text() if event_item is not None else ""
            match = (driver_filter == "All" or driver_text == driver_filter) and \
                    (event_filter == "All" or event_text == event_filter)
            self.table.setRowHidden(row, not match)

    def _show_columns_menu(self):
        menu = QMenu(self)
        for col, label in enumerate(_COLUMN_HEADERS):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(col))
            action.toggled.connect(lambda checked, c=col: self.table.setColumnHidden(c, not checked))
        menu.exec(self.columns_btn.mapToGlobal(self.columns_btn.rect().bottomLeft()))

    def _on_wrap_text_toggled(self, checked):
        """Excel-style Wrap Text toggle: on = every row grows to fit its
        wrapped (multi-line) cell text, off = back to single-line rows
        with the usual truncated/elided text. self._wrap_text_enabled is
        also read by _render_results()/_rebind_row_widgets() so newly
        (re)built rows pick up whichever state is currently active."""
        self._wrap_text_enabled = checked
        self.table.setWordWrap(checked)
        if checked:
            self.table.resizeRowsToContents()
        else:
            for row in range(self.table.rowCount()):
                self.table.setRowHeight(row, self._default_row_height)

    def _on_recheck(self):
        if not self.jobs:
            return
        self._recheck_issues = _compute_recheck_issues(self.jobs, self.last_drivers, self._vehicle_type_by_id)
        for row in range(self.table.rowCount()):
            sr_item = self.table.item(row, COL_SR)
            if sr_item is None:
                continue
            job = self._job_by_sr(sr_item.text())
            if job is None:
                continue
            note_text, note_is_warning = self._note_display(job)
            item = self.table.item(row, COL_NOTE)
            if item is None:
                continue
            item.setText(note_text)
            font = item.font()
            if note_is_warning:
                item.setForeground(RECHECK_WARNING_COLOR)
                font.setBold(True)
            else:
                item.setData(Qt.ForegroundRole, None)  # back to the default (theme) text color
                font.setBold(False)
            item.setFont(font)
        flagged = len({sr for sr, issues in self._recheck_issues.items() if issues})
        if flagged:
            QMessageBox.warning(
                self, "ReCheck found issues",
                f"{flagged} row(s) have a clash or rule break -- see the red Note text in the table."
            )
        else:
            QMessageBox.information(self, "ReCheck", "No clashes or rule breaks found in the current sheet.")


# ---------------------------------------------------------------------------
# ReCheck: clash/rule-break detection over the CURRENT in-memory results,
# including any manual driver/vehicle reassignment. Pure and Qt-free (same
# separation-of-concerns pattern as build_summary() below) so it's directly
# unit-testable, and -- critically -- it never mutates a Job. It only
# reports; the planner's own assignments are always the final word (Rule 11).
# ---------------------------------------------------------------------------
def _overlap(a, b):
    return a.start_dt < b.end_dt and b.start_dt < a.end_dt


def _compute_recheck_issues(jobs, driver_profiles, vehicle_type_by_id):
    """Returns {job.sr: [warning, ...]} for every row with a detected clash
    or rule break. driver_profiles: the DriverProfile list from the last Run
    Planning (self.last_drivers) -- only used for each driver's hour-rule
    CONFIGURATION (working_hours_per_day, max_working_hours_per_day,
    max_overtime_hours_per_month, month_overtime_so_far); the actual hours
    worked are always recomputed fresh from `jobs` here, so a manual
    reassignment is reflected immediately. vehicle_type_by_id: {vehicle_id:
    vehicle_type} from the current database state."""
    issues = defaultdict(list)
    valid = [j for j in jobs if j.start_dt is not None and j.end_dt is not None]

    # 1. Driver double-booking. Also covers "one driver on two trips with two
    # different vehicles at the same time" -- same overlap check regardless
    # of which vehicle each trip used. Same-Driver-grouped rows are exempt,
    # matching allocation_engine._overlaps_with_buffer's own
    # ignore_group_key exemption for legitimate back-and-forth rows.
    by_driver = defaultdict(list)
    for j in valid:
        if j.assigned_driver_id is not None:
            by_driver[j.assigned_driver_id].append(j)
    for driver_jobs in by_driver.values():
        for a, b in combinations(driver_jobs, 2):
            if _group_key_of(a) is not None and _group_key_of(a) == _group_key_of(b):
                continue
            if _overlap(a, b):
                name = a.assigned_driver_name or "This driver"
                issues[a.sr].append(
                    f"Clash: {name} is also on SR{b.sr} ({b.start_dt:%H:%M}-{b.end_dt:%H:%M}), an overlapping time."
                )
                issues[b.sr].append(
                    f"Clash: {name} is also on SR{a.sr} ({a.start_dt:%H:%M}-{a.end_dt:%H:%M}), an overlapping time."
                )

    # 2. Vehicle double-booking: in-house vehicles by id, hired supplier
    # units by their exact label (same label + overlapping time = the same
    # physical unit double-booked).
    by_vehicle = defaultdict(list)
    for j in valid:
        if j.assigned_vehicle_id is not None:
            by_vehicle[j.assigned_vehicle_id].append(j)
    for vehicle_jobs in by_vehicle.values():
        for a, b in combinations(vehicle_jobs, 2):
            if _group_key_of(a) is not None and _group_key_of(a) == _group_key_of(b):
                continue
            if _overlap(a, b):
                plate = a.assigned_vehicle_plate or "This vehicle"
                issues[a.sr].append(
                    f"Clash: vehicle {plate} is also on SR{b.sr} ({b.start_dt:%H:%M}-{b.end_dt:%H:%M}), an overlapping time."
                )
                issues[b.sr].append(
                    f"Clash: vehicle {plate} is also on SR{a.sr} ({a.start_dt:%H:%M}-{a.end_dt:%H:%M}), an overlapping time."
                )

    by_supplier_unit = defaultdict(list)
    for j in valid:
        if j.assigned_supplier_unit:
            by_supplier_unit[j.assigned_supplier_unit].append(j)
    for unit_jobs in by_supplier_unit.values():
        for a, b in combinations(unit_jobs, 2):
            if _group_key_of(a) is not None and _group_key_of(a) == _group_key_of(b):
                continue
            if _overlap(a, b):
                label = a.assigned_supplier_unit
                issues[a.sr].append(
                    f"Clash: supplier unit \"{label}\" is also on SR{b.sr} ({b.start_dt:%H:%M}-{b.end_dt:%H:%M}), an overlapping time."
                )
                issues[b.sr].append(
                    f"Clash: supplier unit \"{label}\" is also on SR{a.sr} ({a.start_dt:%H:%M}-{a.end_dt:%H:%M}), an overlapping time."
                )

    # 3. Driver hard-rule hour violations -- duty SPAN (first job start to
    # last job end that day), the same measure allocation_engine itself
    # enforces (see _day_span_hours), re-verified against the planner's
    # edited output rather than the engine's own original assignment.
    profile_by_id = {d.id: d for d in (driver_profiles or [])}
    for driver_id, driver_jobs in by_driver.items():
        profile = profile_by_id.get(driver_id)
        if profile is None or profile.working_hours_per_day is None:
            continue
        by_date = defaultdict(list)
        for j in driver_jobs:
            by_date[j.start_dt.date()].append(j)
        for the_date, day_jobs in by_date.items():
            span_hours = _day_span_hours([(j.start_dt, j.end_dt) for j in day_jobs])
            baseline = profile.working_hours_per_day
            ceiling = profile.max_working_hours_per_day if profile.max_working_hours_per_day is not None else baseline
            overtime_today = max(0.0, span_hours - baseline)
            if span_hours > ceiling + 1e-9:
                msg = (
                    f"Hard rule: {profile.name}'s shift on {the_date:%d-%b} spans {span_hours:.1f}h, "
                    f"over the {ceiling:.1f}h daily limit."
                )
                for j in day_jobs:
                    issues[j.sr].append(msg)
            if profile.max_overtime_hours_per_month is not None:
                projected = profile.month_overtime_so_far + overtime_today
                if projected > profile.max_overtime_hours_per_month + 1e-9:
                    msg = (
                        f"Hard rule: {profile.name}'s monthly overtime would reach {projected:.1f}h, "
                        f"over the {profile.max_overtime_hours_per_month:.1f}h monthly cap."
                    )
                    for j in day_jobs:
                        issues[j.sr].append(msg)

    # NOTE: an earlier version of this function also flagged a Same-Driver
    # group split across more than one driver as a violation. Removed
    # 2026-08-15 (Phase 29b) -- confirmed against AI/04_BUSINESS_RULES.md's
    # "Same Driver Column" section that this is a documented SOFT
    # preference ("if one driver truly cannot cover the whole flagged
    # group... the system brings in as few additional drivers as
    # possible"), not a hard rule. A real run flagged 47 legitimate,
    # solver-optimal splits as false-positive "clashes" on a fresh,
    # 0-unresolved, provably-optimal plan the planner hadn't even touched
    # yet -- there's no reliable way to tell a deliberate/necessary split
    # from a planner mistake from the data alone, so the check was dropped
    # rather than kept as a source of noise.

    # 4. Wrong vehicle type -- in-house rows only; a Job doesn't record which
    # offering/vehicle-type a supplier unit used, so supplier rows aren't
    # checkable here.
    for j in valid:
        if j.assigned_vehicle_id is None:
            continue
        if (j.vehicle_type_required or "").strip().lower() == "driver only":
            continue
        actual_type = vehicle_type_by_id.get(j.assigned_vehicle_id)
        if actual_type and j.vehicle_type_required and not _type_matches(j.vehicle_type_required, actual_type):
            issues[j.sr].append(
                f"Wrong vehicle type: needs \"{j.vehicle_type_required}\" but "
                f"{j.assigned_vehicle_plate or 'the assigned vehicle'} is a \"{actual_type}\"."
            )

    return dict(issues)


# ---------------------------------------------------------------------------
# Result-only summary helpers
# ---------------------------------------------------------------------------
def _summary_merged_hours(jobs):
    intervals = sorted(
        (j.start_dt, j.end_dt)
        for j in jobs
        if j.start_dt is not None and j.end_dt is not None and j.end_dt > j.start_dt
    )
    if not intervals:
        return 0.0
    total_seconds = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            if end > current_end:
                current_end = end
        else:
            total_seconds += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    total_seconds += (current_end - current_start).total_seconds()
    return total_seconds / 3600.0


def _summary_span(jobs):
    valid = [j for j in jobs if j.start_dt is not None and j.end_dt is not None]
    if not valid:
        return "--", 0.0
    start = min(j.start_dt for j in valid)
    end = max(j.end_dt for j in valid)
    span_hours = (end - start).total_seconds() / 3600.0
    return f"{start.strftime('%H:%M')} – {end.strftime('%H:%M')}", span_hours


def _summary_hours_text(hours):
    if abs(hours - round(hours)) < 1e-9:
        return f"{int(round(hours))} hrs."
    return f"{hours:.1f} hrs."


def _summary_supplier_name(job):
    import re
    label = (job.assigned_supplier_unit or "").strip()
    label = re.sub(r"^SAME\s+", "", label, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+\d+$", "", label).strip() or label


def build_summary(jobs, working_hours_by_driver_id=None):
    """Build the popup report entirely from current in-memory Job results.

    working_hours_by_driver_id: optional {driver_id: working_hours_per_day}
    lookup, taken from the last Run Planning's driver profiles
    (PlanDayTab.last_drivers), used only to compute each driver's overtime
    for THIS day: shift span minus working_hours_per_day, floored at zero
    -- the same span-based method the database's monthly overtime figure
    uses (db.get_driver_month_overtime_hours, corrected Phase 23). This
    does NOT query the database -- last_drivers is already in memory from
    Run Planning, preserving this dialog's existing design principle that
    it never reads SQLite/master-data tables directly (see ARCHITECTURE.md
    Section 3.1)."""
    working_hours_by_driver_id = working_hours_by_driver_id or {}
    from collections import OrderedDict
    assigned_jobs = [j for j in jobs if not j.unresolved]
    driver_groups = OrderedDict()
    supplier_groups = OrderedDict()

    for job in assigned_jobs:
        if job.assigned_driver_id is not None:
            key = job.assigned_driver_id
            name = job.assigned_driver_name or "Unknown driver"
            driver_groups.setdefault(key, {"name": name, "jobs": []})["jobs"].append(job)
        elif job.assigned_supplier_unit:
            key = job.assigned_supplier_id
            if key is None:
                key = _summary_supplier_name(job)
            supplier_groups.setdefault(
                key, {"name": _summary_supplier_name(job), "jobs": []}
            )["jobs"].append(job)

    drivers = []
    for driver_id, entry in driver_groups.items():
        driver_jobs = entry["jobs"]
        span, span_hours = _summary_span(driver_jobs)
        working_hours_per_day = working_hours_by_driver_id.get(driver_id)
        # None (not just 0) when the driver's working_hours_per_day isn't
        # known here -- e.g. Run Planning hasn't been run this session, or
        # the driver was removed from the roster after planning -- shown
        # as "--" rather than a misleading 0.0.
        overtime_hours = (
            max(0.0, span_hours - working_hours_per_day)
            if working_hours_per_day is not None else None
        )
        drivers.append({
            "name": entry["name"],
            "span": span,
            "span_hours": span_hours,
            "overtime_hours": overtime_hours,
            "trips": len(driver_jobs),
            "worked_hours": _summary_merged_hours(driver_jobs),
        })
    drivers.sort(key=lambda x: x["name"].upper())

    suppliers_detail = []
    for entry in supplier_groups.values():
        supplier_jobs = entry["jobs"]
        units = OrderedDict()
        for job in supplier_jobs:
            unit_label = (job.assigned_supplier_unit or "").strip()
            unit_key = unit_label.removeprefix("SAME ") or unit_label
            units.setdefault(unit_key, []).append(job)
        supplier_hours = sum(_summary_merged_hours(unit_jobs) for unit_jobs in units.values())
        span, span_hours = _summary_span(supplier_jobs)
        suppliers_detail.append({
            "name": entry["name"],
            "span": span,
            "span_hours": span_hours,
            "overtime_hours": None,  # overtime is a driver hard-rule concept (working_hours_per_day) -- not applicable to suppliers
            "trips": len(supplier_jobs),
            "worked_hours": supplier_hours,
        })
    suppliers_detail.sort(key=lambda x: x["name"].upper())

    return {
        "in_house_drivers": len(drivers),
        "in_house_trips": sum(x["trips"] for x in drivers),
        "total_trips": len(jobs),
        "suppliers": len(suppliers_detail),
        "supplier_trips": sum(x["trips"] for x in suppliers_detail),
        "unresolved": sum(1 for j in jobs if j.unresolved),
        "drivers": drivers,
        "suppliers_detail": suppliers_detail,
    }


class DriverSupplierSummaryDialog(QDialog):
    """Read-only summary calculated exclusively from the current in-memory results."""

    def __init__(self, jobs, drivers=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Driver & Supplier Summary")
        # Frameless: the window already has its own in-content "x" close
        # button below, so the native OS title bar (with its own close/
        # minimize controls) is redundant -- removing it also frees up
        # vertical space, which matters now that the dialog is shorter.
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        # Reduced from 900 -- the previous height could get its bottom
        # rows (including the Close button) clipped under the taskbar on
        # smaller/laptop screens. The table scrolls internally, so a
        # shorter dialog just means fewer rows visible before scrolling,
        # not lost content.
        self.resize(980, 720)
        self.setMinimumSize(900, 620)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background: #ffffff; color: #161616; }
            QLabel { color: #161616; }
            QFrame#statCard {
                border: 1px solid #e2e8f1; border-radius: 14px;
            }
            QLabel#statTitle { color: #46505f; font-size: 12px; }
            QLabel#statValue { color: #111827; font-size: 24px; font-weight: 650; }
            QFrame#iconBadge {
                border: none; border-radius: 12px;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #fbfcfe;
                border: 1px solid #e1e6ef;
                border-radius: 12px;
                gridline-color: #e4e9f0;
                color: #161616;
                font-size: 12px;
                selection-background-color: #eef4ff;
                selection-color: #161616;
            }
            QTableWidget::item { padding: 5px 7px; border: none; }
            QHeaderView::section {
                background: #f5f7fa;
                color: #222222;
                border: none;
                border-bottom: 1px solid #dfe4ec;
                padding: 8px 7px;
                font-size: 11px;
                font-weight: 650;
            }
            QScrollBar:vertical { width: 9px; background: transparent; }
            QScrollBar::handle:vertical { background: #cbd3df; border-radius: 4px; min-height: 28px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QFrame#footerCard {
                background: #f8fafc; border: 1px solid #e4e9f2; border-radius: 11px;
            }
            QLabel#footerTitle { color: #555555; font-size: 12px; }
            QLabel#footerValue { color: #111111; font-size: 16px; font-weight: 650; }
            QPushButton#closeButton {
                background: #3f7ee8; color: white; border: none; border-radius: 10px;
                padding: 9px 24px; font-size: 13px;
            }
            QPushButton#closeButton:hover { background: #336dcc; }
        """)

        working_hours_by_driver_id = {d.id: d.working_hours_per_day for d in (drivers or [])}
        data = build_summary(jobs, working_hours_by_driver_id)
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 20)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        title_icon = self._draw_title_icon()
        title_row.addWidget(title_icon)

        title = QLabel("Driver & Supplier Summary")
        title.setStyleSheet("font-size: 24px; font-weight: 650; color: #111827;")
        title_row.addWidget(title)
        title_row.addStretch()

        close_x = QPushButton("×")
        close_x.setFixedSize(36, 36)
        close_x.setStyleSheet(
            "font-size: 27px; color: #707070; border: none; background: transparent;"
        )
        close_x.clicked.connect(self.accept)
        title_row.addWidget(close_x)
        root.addLayout(title_row)

        # Four modern cards. Drivers/suppliers use the same line-art icon family;
        # both trip cards use the planner's supplied trip clipart.
        stats = QGridLayout()
        stats.setHorizontalSpacing(10)
        stats.setColumnStretch(0, 1)
        stats.setColumnStretch(1, 1)
        stats.setColumnStretch(2, 1)
        stats.setColumnStretch(3, 1)
        stat_values = [
            ("In-house drivers", data["in_house_drivers"], "drivers", "#eef4ff", "#3f7ee8"),
            ("In-house trips", data["in_house_trips"], "trip", "#effaf2", "#39a96b"),
            ("Suppliers", data["suppliers"], "supplier", "#f5efff", "#8750c9"),
            ("Supplier trips", data["supplier_trips"], "trip", "#fff8e8", "#d89a16"),
        ]
        for col, (label_text, value, icon_kind, bg, accent) in enumerate(stat_values):
            card = QFrame()
            card.setObjectName("statCard")
            card.setStyleSheet(
                f"QFrame#statCard {{ background: {bg}; border: 1px solid #e2e8f1; border-radius: 14px; }}"
            )
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(13, 10, 13, 10)
            card_layout.setSpacing(10)
            icon_badge = QFrame()
            icon_badge.setObjectName("iconBadge")
            icon_badge.setFixedSize(44, 44)
            icon_badge.setStyleSheet(
                f"QFrame#iconBadge {{ background: rgba(255,255,255,0.55); border-radius: 11px; }}"
            )
            badge_layout = QVBoxLayout(icon_badge)
            badge_layout.setContentsMargins(4, 4, 4, 4)
            badge_layout.setAlignment(Qt.AlignCenter)
            icon_label = QLabel()
            icon_label.setAlignment(Qt.AlignCenter)
            if icon_kind == "trip":
                icon_label.setPixmap(self._trip_clipart())
            elif icon_kind == "drivers":
                icon_label.setPixmap(self._draw_people_icon(accent))
            else:
                icon_label.setPixmap(self._draw_supplier_icon(accent))
            badge_layout.addWidget(icon_label)
            card_layout.addWidget(icon_badge)

            text_layout = QVBoxLayout()
            text_layout.setSpacing(1)
            label = QLabel(label_text)
            label.setObjectName("statTitle")
            value_label = QLabel(str(value))
            value_label.setObjectName("statValue")
            text_layout.addWidget(label)
            text_layout.addWidget(value_label)
            card_layout.addLayout(text_layout)
            stats.addWidget(card, 0, col)
        root.addLayout(stats)

        # Both groups are always displayed; no filter checkbox is needed because
        # the table itself explicitly separates in-house resources and suppliers.
        self.summary_table = QTableWidget()
        table = self.summary_table
        table.setColumnCount(8)
        table.setHorizontalHeaderLabels([
            "#", "Driver / Supplier", "Shift Start", "Shift End",
            "Shift Span", "Overtime", "Trips", "Total Hours",
        ])
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(True)
        table.setMinimumHeight(380)
        table.verticalHeader().setDefaultSectionSize(32)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        header.setSectionResizeMode(7, QHeaderView.Fixed)
        header.resizeSection(0, 45)
        header.resizeSection(2, 112)
        header.resizeSection(3, 112)
        header.resizeSection(4, 105)
        header.resizeSection(5, 85)
        header.resizeSection(6, 58)
        header.resizeSection(7, 92)
        root.addWidget(table, 1)

        self._summary_data = data
        self._refresh_summary_table()

        # Bottom summary: keep the overall totals visible independently of the
        # top resource cards. These values are calculated from the current
        # planning results only.
        footer = QHBoxLayout()
        footer.setSpacing(10)
        total_card = self._footer_card("Total trips", data["total_trips"])
        unresolved_card = self._footer_card("Unresolved trips", data["unresolved"])
        footer.addWidget(total_card)
        footer.addWidget(unresolved_card)
        footer.addStretch(1)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("closeButton")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

    def _footer_card(self, title_text, value):
        card = QFrame()
        card.setObjectName("footerCard")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 7, 14, 7)
        title = QLabel(title_text)
        title.setObjectName("footerTitle")
        value_label = QLabel(str(value))
        value_label.setObjectName("footerValue")
        layout.addWidget(title)
        layout.addSpacing(10)
        layout.addWidget(value_label)
        return card

    def _draw_title_icon(self):
        label = QLabel()
        label.setFixedSize(48, 48)
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#3f7ee8"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 48, 48, 12, 12)
        pen = QPen(QColor("#ffffff"), 2)
        painter.setPen(pen)
        painter.drawLine(15, 16, 33, 16)
        painter.drawLine(15, 22, 33, 22)
        painter.drawLine(15, 28, 33, 28)
        painter.drawRect(13, 13, 22, 22)
        painter.end()
        label.setPixmap(pixmap)
        return label

    def _draw_people_icon(self, accent):
        pixmap = QPixmap(34, 34)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(accent), 2.4)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(11, 5, 9, 9)
        painter.drawEllipse(21, 9, 7, 7)
        painter.drawArc(6, 16, 19, 14, 20 * 16, 140 * 16)
        painter.drawArc(19, 17, 13, 12, 15 * 16, 135 * 16)
        painter.end()
        return pixmap

    def _draw_supplier_icon(self, accent):
        pixmap = QPixmap(34, 34)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(accent), 2.2)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(4, 10, 18, 12)
        painter.drawLine(22, 14, 28, 14)
        painter.drawLine(28, 14, 31, 18)
        painter.drawLine(31, 18, 31, 22)
        painter.drawLine(22, 22, 4, 22)
        painter.drawEllipse(8, 19, 7, 7)
        painter.drawEllipse(23, 19, 7, 7)
        painter.end()
        return pixmap

    def _trip_clipart(self):
        """Load the supplied trip clipart bundled beside this module."""
        pixmap = QPixmap(str(Path(__file__).with_name("trip_clipart.png")))
        if pixmap.isNull():
            # Keep the UI usable if the optional visual asset is missing.
            pixmap = QPixmap(34, 34)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor("#39a96b"), 2.2))
            painter.drawArc(4, 5, 24, 24, 30 * 16, 300 * 16)
            painter.drawLine(18, 5, 18, 11)
            painter.end()
        return pixmap.scaled(34, 34, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _refresh_summary_table(self):
        """Refresh the summary rows without re-reading the database."""
        data = self._summary_data
        table = self.summary_table
        table.setRowCount(0)

        # Group 1: in-house drivers, always first.
        rows = [("driver", driver) for driver in data["drivers"]]
        # Group 2: suppliers, always second.
        rows.extend(("supplier", supplier) for supplier in data["suppliers_detail"])

        if not rows:
            table.setRowCount(1)
            empty = QTableWidgetItem("No assigned records in the current results.")
            empty.setTextAlignment(Qt.AlignCenter)
            table.setItem(0, 0, empty)
            table.setSpan(0, 0, 1, 8)
            return

        display_index = 0
        inhouse_group_inserted = False
        supplier_group_inserted = False
        for kind, record in rows:
            if kind == "driver" and not inhouse_group_inserted:
                inhouse_group_inserted = True
                table.insertRow(table.rowCount())
                group_row = table.rowCount() - 1
                group_item = QTableWidgetItem("IN-HOUSE DRIVERS")
                group_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                group_item.setForeground(QColor("#3f7ee8"))
                group_item.setBackground(QColor("#eef4ff"))
                table.setItem(group_row, 0, group_item)
                table.setSpan(group_row, 0, 1, 8)
            elif kind == "supplier" and not supplier_group_inserted:
                supplier_group_inserted = True
                table.insertRow(table.rowCount())
                group_row = table.rowCount() - 1
                group_item = QTableWidgetItem("SUPPLIERS")
                group_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                group_item.setForeground(QColor("#7b4bc4"))
                group_item.setBackground(QColor("#f7f0ff"))
                table.setItem(group_row, 0, group_item)
                table.setSpan(group_row, 0, 1, 8)

            table.insertRow(table.rowCount())
            row = table.rowCount() - 1
            display_index += 1
            span = record["span"]
            if " – " in span:
                span_start, span_end = span.split(" – ", 1)
            else:
                span_start = span_end = "--"
            overtime_hours = record.get("overtime_hours")
            overtime_text = "--" if overtime_hours is None else _summary_hours_text(overtime_hours)
            values = [
                f"{display_index:02d}",
                record["name"],
                span_start,
                span_end,
                _summary_hours_text(record["span_hours"]),
                overtime_text,
                str(record["trips"]),
                _summary_hours_text(record["worked_hours"]),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in (0, 2, 3, 4, 5, 6, 7):
                    item.setTextAlignment(Qt.AlignCenter)
                else:
                    item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                if col == 5 and overtime_hours is not None and overtime_hours > 0:
                    # Flag today's overtime the same way the Drivers tab
                    # flags an over-budget monthly balance -- explainable,
                    # not hidden (Rule 8).
                    item.setForeground(QColor("#c0392b"))
                if kind == "supplier":
                    item.setBackground(QColor("#fcfaff"))
                table.setItem(row, col, item)

