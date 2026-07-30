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
    QScrollArea, QFrame, QComboBox
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
        self.summary_label = QLabel("")
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.ai_review_btn)
        run_row.addWidget(self.finalize_btn)
        run_row.addWidget(self.export_btn)
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
