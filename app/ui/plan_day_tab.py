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
    QScrollArea, QFrame, QComboBox, QDialog, QGridLayout
)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt

from app import db
from app.excel_import import load_jobs_from_excel, group_jobs_by_event
from app.allocation_engine import (
    allocate, build_driver_profiles, build_vehicle_profiles, build_supplier_offerings
)
from app import maps_client
from app import ai_review
from app import export
from app.ui.settings_tab import ANTHROPIC_KEY_SETTING, GOOGLE_MAPS_KEY_SETTING

UNRESOLVED_COLOR = QColor("#5a2020")
SUPPLIER_COLOR = QColor("#3a3a20")


class PlanDayTab(QWidget):
    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.jobs = []
        self.uploaded_path = None
        self.last_drivers = []
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

        layout.addWidget(QLabel(
            "Day notes (optional) -- anything about tomorrow the planner wants considered, "
            "e.g. \"VIP event at Zabeel today, expect longer waits\" or "
            "\"Deepak can go over his usual hours today if needed\":"
        ))
        self.notes_input = QTextEdit()
        self.notes_input.setPlaceholderText("Type any notes for this planning day here...")
        self.notes_input.setMaximumHeight(80)
        layout.addWidget(self.notes_input)

        run_row = QHBoxLayout()
        self.run_btn = QPushButton("Run Planning")
        self.run_btn.clicked.connect(self._on_run)
        self.run_btn.setEnabled(False)
        self.ai_review_btn = QPushButton("AI Review (event chains + day notes)")
        self.ai_review_btn.clicked.connect(self._on_ai_review)
        self.ai_review_btn.setEnabled(False)
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
        run_row.addWidget(self.finalize_btn)
        run_row.addWidget(self.export_btn)
        run_row.addWidget(self.summary_btn)
        run_row.addWidget(self.summary_label, stretch=1)
        layout.addLayout(run_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter by driver/supplier:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All")
        self.filter_combo.currentTextChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_combo, stretch=1)
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["SR", "Time", "Event", "Vehicle Type Required", "Pick Up", "Driver / Supplier", "Vehicle / Unit", "Note"]
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table)

        layout.addWidget(QLabel("AI Suggestions (accept or reject each one):"))
        self.suggestions_container = QVBoxLayout()
        suggestions_scroll_widget = QWidget()
        suggestions_scroll_widget.setLayout(self.suggestions_container)
        self.suggestions_scroll = QScrollArea()
        self.suggestions_scroll.setWidgetResizable(True)
        self.suggestions_scroll.setWidget(suggestions_scroll_widget)
        self.suggestions_scroll.setMaximumHeight(180)
        layout.addWidget(self.suggestions_scroll)
        self.no_suggestions_label = QLabel("Run planning, then click \"AI Review\" to see suggestions here.")
        self.no_suggestions_label.setStyleSheet("color: #888888;")
        self.suggestions_container.addWidget(self.no_suggestions_label)

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

        allocate(self.jobs, drivers, vehicles, supplier_offerings)
        self.last_drivers = drivers
        self._render_results()
        self.ai_review_btn.setEnabled(True)
        self.finalize_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.summary_btn.setEnabled(True)

        unresolved_count = sum(1 for j in self.jobs if j.unresolved)
        in_house_count = sum(1 for j in self.jobs if j.assigned_driver_id is not None)
        supplier_count = sum(1 for j in self.jobs if j.assigned_supplier_unit is not None)
        self.summary_label.setText(
            f"{len(self.jobs)} jobs total  |  {in_house_count} in-house  |  "
            f"{supplier_count} supplier  |  {unresolved_count} unresolved"
        )
        if self.notes_input.toPlainText().strip():
            note_preview = self.notes_input.toPlainText().strip()
            self.summary_label.setText(
                self.summary_label.text() + "   (day notes recorded — will be used once the AI review layer is added)"
            )

    def _on_ai_review(self):
        anthropic_key = db.get_setting(self.conn, ANTHROPIC_KEY_SETTING)
        maps_key = db.get_setting(self.conn, GOOGLE_MAPS_KEY_SETTING)

        if not anthropic_key:
            QMessageBox.information(self, "No Anthropic key",
                                     "Add your Anthropic API key in the Settings tab first.")
            return

        event_groups = group_jobs_by_event(self.jobs)
        multi_stage_groups = {eid: g for eid, g in event_groups.items() if len(g) >= 2}

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
                    try:
                        result = maps_client.get_travel_time(
                            maps_key, origin["address"], destination["address"], prev_job.end_dt
                        )
                        travel_lookups[key] = {
                            "duration_minutes": result["duration_minutes"],
                            "confidence": confidence,
                        }
                    except maps_client.MapsClientError as e:
                        travel_lookups[key] = {"error": str(e), "confidence": confidence}
        else:
            maps_warning = "(No Google Maps key set — proceeding without real travel-time data.)\n"

        driver_hours_summary = {d.name: round(d.occupied_seconds / 3600.0, 1) for d in self.last_drivers}
        day_notes = self.notes_input.toPlainText().strip()

        digest_row = db.get_digest(self.conn)
        preferences_digest = digest_row["digest_text"] if digest_row else ""

        context = ai_review.build_review_context(
            self.jobs, multi_stage_groups, driver_hours_summary, travel_lookups, day_notes,
            preferences_digest=preferences_digest,
        )

        try:
            suggestions = ai_review.review_plan(anthropic_key, context)
        except ai_review.AIReviewError as e:
            QMessageBox.warning(self, "AI Review failed", str(e))
            return

        self._clear_suggestions()
        if maps_warning:
            warn_label = QLabel(maps_warning)
            warn_label.setStyleSheet("color: #a08030;")
            self.suggestions_container.addWidget(warn_label)

        if not suggestions:
            label = QLabel("No suggestions — the current plan looks fine as is.")
            self.suggestions_container.addWidget(label)
            return

        plan_date = self.jobs[0].date.isoformat() if self.jobs and self.jobs[0].date else ""
        for s in suggestions:
            self._add_suggestion_widget(s, plan_date)

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

        jobs_str = ", ".join(f"SR{j}" for j in suggestion.get("affected_jobs", []))
        header = QLabel(f"[{suggestion.get('type', 'suggestion')}] {jobs_str}")
        header.setStyleSheet("font-weight: bold;")
        frame_layout.addWidget(header)

        reasoning_label = QLabel(suggestion.get("reasoning", ""))
        reasoning_label.setWordWrap(True)
        frame_layout.addWidget(reasoning_label)

        btn_row = QHBoxLayout()
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
        dialog = DriverSupplierSummaryDialog(self.jobs, self)
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
            })

        db.save_finalized_jobs(self.conn, plan_date, job_rows)
        QMessageBox.information(self, "Finalized", f"Saved {len(job_rows)} assignments to history for {plan_date}.")

    def _render_results(self):
        self.table.setSortingEnabled(False)  # must be off while populating, or rows scramble mid-insert
        self.table.setRowCount(0)
        driver_supplier_values = set()
        for job in self.jobs:
            row = self.table.rowCount()
            self.table.insertRow(row)

            time_str = ""
            if job.start_dt and job.end_dt:
                time_str = f"{job.start_dt.strftime('%H:%M')} - {job.end_dt.strftime('%H:%M')}"

            driver_or_supplier = ""
            vehicle_or_unit = ""
            if job.assigned_driver_id is not None:
                driver_or_supplier = job.assignment_note.replace("In-house: ", "")
                vehicle_or_unit = job.assigned_vehicle_plate
            elif job.assigned_supplier_unit:
                # "Supplier Name - #1" -> split company from unit label
                driver_or_supplier = job.assigned_supplier_unit
                vehicle_or_unit = job.assigned_supplier_unit
            elif job.unresolved:
                driver_or_supplier = "UNRESOLVED"
                vehicle_or_unit = "UNRESOLVED"

            if driver_or_supplier and driver_or_supplier != "UNRESOLVED":
                # Group "SAME X" with "X" for filtering -- same physical
                # truck/driver, just reused later in the day.
                driver_supplier_values.add(driver_or_supplier.removeprefix("SAME "))

            values = [
                job.sr, time_str, job.event_text, job.vehicle_type_required,
                job.pickup_location, driver_or_supplier, vehicle_or_unit, job.assignment_note,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                if job.unresolved:
                    item.setBackground(UNRESOLVED_COLOR)
                elif job.assigned_supplier_unit:
                    item.setBackground(SUPPLIER_COLOR)
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)

        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItem("All")
        for name in sorted(driver_supplier_values):
            self.filter_combo.addItem(name)
        self.filter_combo.blockSignals(False)

    def _apply_filter(self, selected_text):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 5)  # Driver / Supplier column
            cell_text = item.text().removeprefix("SAME ") if item is not None else ""
            match = selected_text == "All" or cell_text == selected_text
            self.table.setRowHidden(row, not match)


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


def build_summary(jobs):
    """Build the popup report entirely from current in-memory Job results."""
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
    for entry in driver_groups.values():
        driver_jobs = entry["jobs"]
        span, span_hours = _summary_span(driver_jobs)
        drivers.append({
            "name": entry["name"],
            "span": span,
            "span_hours": span_hours,
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
        suppliers_detail.append({
            "name": entry["name"],
            "trips": len(supplier_jobs),
            "worked_hours": supplier_hours,
        })
    suppliers_detail.sort(key=lambda x: x["name"].upper())

    return {
        "in_house_drivers": len(drivers),
        "total_trips": len(jobs),
        "suppliers": len(suppliers_detail),
        "supplier_trips": sum(x["trips"] for x in suppliers_detail),
        "unresolved": sum(1 for j in jobs if j.unresolved),
        "drivers": drivers,
        "suppliers_detail": suppliers_detail,
    }


class DriverSupplierSummaryDialog(QDialog):
    """Read-only summary calculated exclusively from the current in-memory results."""

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Driver & Supplier Summary")
        self.resize(1050, 720)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background: #ffffff; color: #161616; }
            QLabel { color: #161616; }
            QFrame#headerCard, QFrame#statCard {
                background: #f5f8fd; border: 1px solid #e4e9f2; border-radius: 14px;
            }
            QFrame#statCard { min-height: 82px; }
            QLabel#statTitle { color: #444444; font-size: 13px; }
            QLabel#statValue { color: #111111; font-size: 21px; font-weight: 600; }
            QLabel#sectionTitle { font-size: 17px; font-weight: 600; }
            QLabel#driverName, QLabel#supplierName { font-size: 14px; font-weight: 500; }
            QLabel#driverDetail, QLabel#supplierDetail { font-size: 14px; }
            QPushButton#closeButton {
                background: #3f7ee8; color: white; border: none; border-radius: 10px;
                padding: 10px 24px; font-size: 14px;
            }
            QPushButton#closeButton:hover { background: #336dcc; }
        """)

        data = build_summary(jobs)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)

        title_row = QHBoxLayout()
        icon = QLabel("▤")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(48, 48)
        icon.setStyleSheet("background: #3f7ee8; color: white; border-radius: 12px; font-size: 25px;")
        title_row.addWidget(icon)
        title = QLabel("Driver & Supplier Summary")
        title.setStyleSheet("font-size: 25px; font-weight: 600;")
        title_row.addWidget(title)
        title_row.addStretch()
        close_x = QPushButton("×")
        close_x.setFixedSize(38, 38)
        close_x.setStyleSheet("font-size: 28px; color: #707070; border: none; background: transparent;")
        close_x.clicked.connect(self.accept)
        title_row.addWidget(close_x)
        root.addLayout(title_row)

        stats = QGridLayout()
        stats.setHorizontalSpacing(12)
        stat_values = [
            ("In-house drivers", data["in_house_drivers"]),
            ("Trips", data["total_trips"]),
            ("Suppliers", data["suppliers"]),
            ("Supplier trips", data["supplier_trips"]),
        ]
        for col, (label_text, value) in enumerate(stat_values):
            card = QFrame()
            card.setObjectName("statCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(18, 12, 18, 12)
            label = QLabel(label_text)
            label.setObjectName("statTitle")
            value_label = QLabel(str(value))
            value_label.setObjectName("statValue")
            card_layout.addWidget(label)
            card_layout.addWidget(value_label)
            stats.addWidget(card, 0, col)
        root.addLayout(stats)

        body = QScrollArea()
        body.setWidgetResizable(True)
        body.setFrameShape(QFrame.NoFrame)
        body_widget = QWidget()
        body_layout = QVBoxLayout(body_widget)
        body_layout.setContentsMargins(4, 2, 4, 2)
        body_layout.setSpacing(10)

        section = QLabel("In-house drivers")
        section.setObjectName("sectionTitle")
        body_layout.addWidget(section)
        for driver in data["drivers"]:
            row = QFrame()
            row.setObjectName("headerCard")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(16, 11, 16, 11)
            name = QLabel(driver["name"])
            name.setObjectName("driverName")
            name.setMinimumWidth(390)
            detail = QLabel(
                f'{driver["span"]} = {_summary_hours_text(driver["span_hours"])}  |  '
                f'{driver["trips"]} trips = {_summary_hours_text(driver["worked_hours"])}'
            )
            detail.setObjectName("driverDetail")
            row_layout.addWidget(name)
            row_layout.addWidget(detail, 1)
            body_layout.addWidget(row)

        if data["suppliers_detail"]:
            supplier_section = QLabel("Suppliers used")
            supplier_section.setObjectName("sectionTitle")
            body_layout.addSpacing(8)
            body_layout.addWidget(supplier_section)
            for supplier in data["suppliers_detail"]:
                row = QFrame()
                row.setObjectName("headerCard")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(16, 11, 16, 11)
                name = QLabel(supplier["name"])
                name.setObjectName("supplierName")
                name.setMinimumWidth(390)
                detail = QLabel(
                    f'{supplier["trips"]} trips = {_summary_hours_text(supplier["worked_hours"])}'
                )
                detail.setObjectName("supplierDetail")
                row_layout.addWidget(name)
                row_layout.addWidget(detail, 1)
                body_layout.addWidget(row)

        if not data["drivers"] and not data["suppliers_detail"]:
            body_layout.addWidget(QLabel("No assigned drivers or suppliers in the current results."))
        body_layout.addStretch()
        body.setWidget(body_widget)
        root.addWidget(body, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setObjectName("closeButton")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)
