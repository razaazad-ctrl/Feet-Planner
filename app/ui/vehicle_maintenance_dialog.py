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
    QLineEdit, QFileDialog,
)
from PySide6.QtGui import (
    QPixmap, QGuiApplication, QIcon, QPainter, QPdfWriter, QPageSize,
    QPageLayout, QFont, QColor, QPen,
)
from PySide6.QtCore import Qt, QTimer, QFile, QSize, QMarginsF, QRectF
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


def _format_number(value):
    """Current Reading/Next Reading/Qty are stored as REAL in the DB
    (service_records), so a whole-number value like 1.0 or 357565.0
    would otherwise display with a trailing ".0" -- str(int(value))
    drops that whenever the value truly is a whole number, while still
    showing a real decimal (e.g. 1.5) as-is rather than silently
    rounding it away."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cards_data_for(row, records):
    """Computes the 5 summary cards' (title, icon_file, date_iso, extra_text,
    bg_color) tuples from one vehicle row + its service records. Shared by
    the on-screen _refresh_cards() (Phase 28gg) and the Phase 28hh PDF
    export, so the exported PDF's cards are always guaranteed to show
    exactly the same data/formatting as the live window -- there is only
    one place this logic lives."""
    last_by_type = {}
    for r in records:
        last_by_type[r["service_type"]] = r  # sorted ascending -> last write wins

    battery_last = last_by_type.get("Battery Change")
    tyre_last = last_by_type.get("Tyre Change")
    oil_last = last_by_type.get("Oil/Filter Change")
    chiller_last = last_by_type.get("Chiller Unit Service")

    return [
        # Registration # shown as this card's bottom line (no label, same
        # as every other card's extra_text) -- besides being useful here,
        # this also makes the Vehicle Expiry card the same icon/title/
        # date/extra structure as the other four, so its date lines up at
        # the same height as theirs instead of sitting higher (it
        # previously had no fourth line).
        ("Vehicle Expiry", "vehicle_expiry_icon.jpg", row["vehicle_reg_expiry"],
         row["vehicle_registration"], "#fdeef0"),
        ("Battery Change", "battery_icon.png", battery_last["start_date"] if battery_last else None,
         row["battery_type"], "#eafaf6"),
        ("Tyre Change", "tyre_icon.png", tyre_last["start_date"] if tyre_last else None,
         row["tyre_size"], "#fdf1e8"),
        ("Oil Service", "oilfilter_icon.png", oil_last["start_date"] if oil_last else None,
         f"Next reading: {_format_number(oil_last['next_reading'])}"
         if oil_last and oil_last["next_reading"] is not None else None,
         "#fdf8e3"),
        ("Chiller Service", "chiller_icon.png", chiller_last["start_date"] if chiller_last else None,
         f"Next reading: {_format_number(chiller_last['next_reading'])}"
         if chiller_last and chiller_last["next_reading"] is not None else None,
         "#f3eefc"),
    ]


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
        # Widened from 1050 (Phase 28y): measured that Chassis/Engine/RTA/
        # Ad. Certificate's real content (even after Phase 28x's maxWidth
        # caps) reaches ~1045px on its own, and the free-floating picture
        # needs to start past that (1065, see vehicle_info_section.ui) to
        # avoid overlapping it -- 1065 + 314px picture + ~20px right
        # margin needs ~1400px minimum; 1450 leaves a bit of breathing
        # room rather than sitting exactly at the wall.
        #
        # Phase 28z briefly doubled the picture to 628x290 and widened
        # this to 1800 to match -- reverted at the project owner's
        # request (pushed the cards/table row down too far, leaving too
        # little vertical room for the service-history table); the
        # picture's dynamic-scaling fix from that phase was kept (see
        # _load_vehicle()).
        #
        # Phase 28bb: the project owner asked for as much of a 30-40%
        # increase as achievable without disturbing anything else --
        # landed on 40% (440x203) and widened this to 1600 to match --
        # reintroduced the height reservation for this, sized to match.
        #
        # Phase 28dd/28ee: the picture's height turned out to leave a
        # real 87px gap above the cards row no repositioning could close
        # (see vehicle_info_section.ui and _build_vehicle_info() for the
        # measurements) -- traded back down to 286x132 (computed as
        # "just enough" two independent ways: matching the text content's
        # own natural bottom edge, and separately reducing the excess gap
        # to roughly this dialog's normal inter-section spacing -- both
        # converged on ~130px). At that width, 1400px is enough; 1450
        # leaves the same small margin Phase 28y originally chose.
        target_w, target_h = 1450, 950
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
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border: 2px solid #3f7ee8; border-radius: 3px; background: #ffffff;
            }
            QCheckBox::indicator:checked { background: #3f7ee8; }
            QCheckBox::indicator:hover { border-color: #336dcc; }
            QPushButton {
                background: #f3f5f8; color: #161616; border: 1px solid #d6dce4;
                border-radius: 6px; padding: 6px 14px;
            }
            QPushButton:hover { background: #e7ebf1; }
            QFrame#card { border: 1px solid #e2e8f1; border-radius: 12px; }
            QLabel#cardTitle { font-size: 14px; color: #46505f; font-weight: 650; }
            QLabel#cardDate { font-size: 15px; color: #111827; font-weight: 650; }
            QLabel#cardExtra { font-size: 14px; color: #555555; }
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
        self._build_cards()

        root.addWidget(self._divider())

        history_header = QHBoxLayout()
        history_header.addWidget(QLabel("Service history (double-click a cell to edit -- saves automatically):"))
        # Phase 28hh (revised): "+ Add a Record" / "Delete Selected Row"
        # stay in their original position (right after the stretch, same
        # as before this phase) -- only shifted left as far as needed to
        # make room for the one new PDF-export button beside them, not
        # all the way over to the label.
        history_header.addStretch(1)
        add_record_btn = QPushButton("+ Add a Record")
        add_record_btn.clicked.connect(self._on_add_record_row)
        history_header.addWidget(add_record_btn)
        delete_record_btn = QPushButton("Delete Selected Row")
        delete_record_btn.clicked.connect(self._on_delete_record_row)
        history_header.addWidget(delete_record_btn)
        history_header.addSpacing(10)
        pdf_export_btn = QPushButton()
        pdf_icon_path = _ASSETS / "pdf_icon.png"
        if pdf_icon_path.exists():
            pdf_export_btn.setIcon(QIcon(str(pdf_icon_path)))
            pdf_export_btn.setIconSize(QSize(20, 20))
        pdf_export_btn.setToolTip("Export maintenance log to PDF")
        pdf_export_btn.setFixedSize(34, 34)
        pdf_export_btn.clicked.connect(self._on_export_pdf_clicked)
        history_header.addWidget(pdf_export_btn)
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

        # Phase 28u: RTA/Ad. Certificate Expiry both show a fixed-format
        # short date ("13-Aug-2026", never long enough to need wrapping)
        # but wordWrap was left on (true for every field value since
        # Phase 28b's original _field_row() helper) -- when the column
        # wasn't quite wide enough, Qt reserved 2 lines of height for
        # these specifically, inflating that row and visually reading as
        # a gap between the #/Expiry rows even though nothing else moved.
        # Chassis/Engine/RTA#/Ad.Cert# are left wrapping (unchanged) --
        # those can legitimately hold a long VIN/certificate number.
        self.rta_cert_expiry_value.setWordWrap(False)
        self.ad_cert_expiry_value.setWordWrap(False)

        # Phase 28x: with the plate and picture both pulled out of this
        # grid (below), column 0 became the row's only real content, and
        # Qt gives leftover row width to whichever column(s) have stretch
        # -- tested several stretch-factor combinations trying to keep
        # these tight and none reliably worked (one attempt made things
        # visibly worse). setMaximumWidth() is a hard constraint Qt
        # always honors regardless of stretch, unlike stretch itself
        # (a soft hint) -- used here instead, so these six labels stay
        # sized to their actual content rather than ballooning into
        # wherever the picture used to be, which is what the picture's
        # new free-floating position (below) needs to be measured
        # against reliably. Expiry dates are always a short fixed format
        # ("13-Aug-2026") so get a tighter cap than the certificate/VIN
        # number fields, which can vary more in length.
        for value_label in (self.chassis_value, self.engine_value,
                             self.rta_cert_value, self.ad_cert_value):
            value_label.setMaximumWidth(200)
        for value_label in (self.rta_cert_expiry_value, self.ad_cert_expiry_value):
            value_label.setMaximumWidth(130)

        # Phase 28r: plateBox is now a plain QLabel used as an image
        # container (previously a QFrame styled with CSS border-image --
        # that left the frame's own opaque palette background painted
        # underneath the plate PNG's rounded outline, since the CSS never
        # set background:transparent, producing what looked like two
        # stacked plate-shaped rectangles). The pixmap itself is set here
        # in Python, the same Path(__file__)-relative way every other
        # image in this dialog already loads, rather than baked into the
        # .ui file (a <pixmap> property there would depend on a Qt
        # resource system this project doesn't use). plate_display is a
        # genuine child widget of plateBox in the .ui file now (not a
        # layout item, no spacer) -- children always paint on top of
        # their parent in Qt, so the text is guaranteed to render over
        # the image with nothing else behind it.
        #
        # Phase 28w: plateBox was pulled out of infoRow's grid entirely
        # (it used to occupy column 1, between the left info column and
        # the picture) and is now a free-floating child of `section`
        # itself, positioned via its own explicit (x, y) rather than a
        # grid cell. This was the actual fix for "push the plate left" --
        # Phase 28u found there was only ~3px of genuine free space left
        # in that column before shifting it further would compress
        # column 0's real content, since the picture (column 2) was
        # already overflowing this dialog's right edge. Taking the plate
        # out of the grid removes it from that contest for space
        # entirely -- it can now sit anywhere, including overlapping
        # into the blank area to the right of the Model/Type text, with
        # zero effect on Chassis/Engine/RTA/Ad Certificate or the
        # picture's own layout-managed sizing. findChild() still finds
        # it identically either way -- searching the widget tree doesn't
        # care whether a widget is layout-managed or free-floating.
        plate_box_label = section.findChild(QLabel, "plateBox")
        if plate_box_label is not None:
            plate_bg_path = _ASSETS / "plate_background.png"
            if plate_bg_path.exists():
                plate_box_label.setPixmap(
                    QPixmap(str(plate_bg_path)).scaled(
                        plate_box_label.width(), plate_box_label.height(),
                        Qt.IgnoreAspectRatio, Qt.SmoothTransformation,
                    )
                )

        # setColumnStretch() is a method call, not a Qt property -- it
        # isn't captured when Designer/QFormBuilder saves a .ui file, so
        # it has to be re-applied here after loading rather than living in
        # the .ui file itself.
        #
        # Phase 28x: both the plate (column 1) and the picture (column 2)
        # were pulled out of this grid entirely and are now free-floating
        # (see the plateBox/picture_label handling above) -- column 0
        # (`left`, the Chassis/Engine/RTA/Ad Certificate stack) is the
        # only column with real content left in it. With column 1 and 2
        # now empty, Qt was handing column 0 ALL of the row's leftover
        # width regardless of its own stretch factor -- confirmed by
        # testing, setting column 0's stretch to 0 alone made no
        # difference. Giving column 2 (now empty) an explicit stretch
        # instead is what actually redirects that leftover space away
        # from column 0 -- Qt distributes leftover width to whichever
        # column(s) have stretch set, not preferentially to columns with
        # real content in them.
        self._info_grid = section.findChild(QGridLayout, "infoRow")
        if self._info_grid is not None:
            self._info_grid.setColumnStretch(0, 0)
            self._info_grid.setColumnStretch(2, 1)

        # Phase 28z/28bb/28cc: `section`'s own reported height comes from
        # `infoRow`'s grid sizeHint, which only accounts for column 0 --
        # the plate and picture are free-floating (Phase 28w/28x), so
        # growing either one doesn't grow `section`'s reported height on
        # its own, and the divider/cards/table below it (each a separate
        # root.addWidget()/addLayout() item in the dialog's outer
        # QVBoxLayout) won't move down to make room without being told.
        # Reserving the real lowest edge here is what makes that happen
        # correctly. The +2 buffer (down from Phase 28bb's +10) is
        # deliberately minimal -- just enough to avoid the picture's
        # bottom edge visually touching the divider, no more -- since the
        # project owner asked for the cards/table pushed up as much as
        # possible; `root.setSpacing(14)` (the same spacing used between
        # every item in this dialog, not something to special-case
        # remove for just this one gap) still applies on top of this.
        lowest_edge = 0
        if plate_box_label is not None:
            lowest_edge = max(lowest_edge, plate_box_label.y() + plate_box_label.height())
        if self.picture_label is not None:
            lowest_edge = max(lowest_edge, self.picture_label.y() + self.picture_label.height())
        if lowest_edge > 0:
            section.setMinimumHeight(lowest_edge + 2)

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
                # Scaled to picture_label's own actual current size, not a
                # hardcoded value -- a real, previously undiscovered bug:
                # this was still hardcoded to 150x100 (the box's original
                # Phase 28-era size) even after later phases enlarged the
                # label itself to 314x145 and beyond, so the real photo
                # was always rendering small and centered inside a much
                # bigger, mostly-empty box. Reading the label's own size
                # here means it automatically stays correct through any
                # future resize of picture_label, in code or in Designer.
                self.picture_label.setPixmap(
                    pixmap.scaled(
                        self.picture_label.width(), self.picture_label.height(),
                        Qt.KeepAspectRatio, Qt.SmoothTransformation,
                    )
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

    def _build_cards(self):
        """Builds the 5 summary cards ONCE, keeping references to each
        card's labels (self._card_widgets) so _refresh_cards() can update
        their text/style in place afterward instead of rebuilding them.

        Phase 28gg fix for a real bug: the previous _refresh_cards()
        destroyed and recreated all 5 cards on every call -- which
        happens after every single service-history cell edit, since
        _save_service_row() calls it each time. Deleting+recreating
        widgets in the cards_row layout briefly churns the outer
        QVBoxLayout (the table sits right below it), and that churn was
        confirmed (via a controlled before/after scrollbar-value test,
        not assumed) to reset the service table's scroll position --
        editing a cell in a scrolled-down row would snap the table back
        toward the top, forcing the planner to re-scroll to find the row
        again. Updating existing labels' text in place touches nothing
        in the layout tree above the table, so no reflow happens and the
        table's scroll position is left alone."""
        self._card_widgets = []
        for _ in range(5):
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(10, 8, 10, 8)

            icon_label = QLabel()
            layout.addWidget(icon_label, alignment=Qt.AlignCenter)

            title_label = QLabel("")
            title_label.setObjectName("cardTitle")
            title_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(title_label)

            date_label = QLabel("")
            date_label.setObjectName("cardDate")
            date_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(date_label)

            extra_label = QLabel("")
            extra_label.setObjectName("cardExtra")
            extra_label.setAlignment(Qt.AlignCenter)
            extra_label.setWordWrap(True)
            layout.addWidget(extra_label)

            self.cards_row.addWidget(card)
            self._card_widgets.append((card, icon_label, title_label, date_label, extra_label))

    def _refresh_cards(self):
        row = db.get_vehicle(self.conn, self.vehicle_id)
        records = db.list_service_records(self.conn, self.vehicle_id)  # oldest first
        cards_data = _cards_data_for(row, records)

        for i, ((card, icon_label, title_label, date_label, extra_label), (title, icon_file, date_iso, extra_text, bg_color)) \
                in enumerate(zip(self._card_widgets, cards_data)):
            card.setStyleSheet(f"QFrame#card {{ background: {bg_color}; border: 1px solid #e2e8f1; border-radius: 12px; }}")

            icon_path = _ASSETS / icon_file
            if icon_path.exists():
                icon_label.setPixmap(QPixmap(str(icon_path)).scaledToHeight(36, Qt.SmoothTransformation))

            title_label.setText(title)

            date_label.setText(_display_date(date_iso))
            # Only the Vehicle Expiry card (index 0, vehicle_reg_expiry) is an
            # actual expiry date -- the other cards show the last service
            # date, which is informational and always in the past, so it
            # must not turn red just because _is_expired() is true.
            date_label.setStyleSheet(
                f"color: {_EXPIRED_COLOR}; font-weight: 700;" if i == 0 and _is_expired(date_iso) else ""
            )

            extra_label.setText(str(extra_text) if extra_text else "")
            extra_label.setVisible(bool(extra_text))

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
            if col in (SC_CURRENT, SC_NEXT, SC_QTY):
                text = _format_number(value)
            else:
                text = "" if value is None else str(value)
            item = QTableWidgetItem(text)
            if col in (SC_START, SC_END, SC_QTY):
                item.setTextAlignment(Qt.AlignCenter)
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

    # -------------------------------------------------------------- PDF export

    def _on_export_pdf_clicked(self):
        # Reads directly from the database (see _generate_maintenance_pdf)
        # rather than from self.service_table -- the live on-screen table
        # is never touched by this, per the project owner's explicit
        # requirement that the date-range filter only affects the PDF.
        dlg = _ExportPdfDialog(self.conn, self.vehicle_id, parent=self)
        dlg.exec()


class _ExportPdfDialog(QDialog):
    """Phase 28hh: small dialog opened by the Vehicle Maintenance Log
    window's new PDF-icon button. Lets the planner pick a From/Till date
    range, then writes a PDF (vehicle info + picture + all 5 summary
    cards + the service history table, filtered to that range) straight
    to a file the planner chooses -- a genuine export, not a
    print/printer dialog. Reads the vehicle/service data itself via
    db.*; never touches the parent VehicleMaintenanceDialog's
    service_table, so the live window's own (unfiltered) view is
    completely unaffected."""

    def __init__(self, conn, vehicle_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.vehicle_id = vehicle_id

        self.setWindowTitle("Export Maintenance Log to PDF")
        self.setFixedSize(320, 210)
        self.setStyleSheet("""
            QDialog { background: #ffffff; color: #161616; }
            QLabel { color: #161616; background: transparent; }
            QLineEdit {
                background: #ffffff; color: #161616;
                border: 1px solid #cfd6e0; border-radius: 4px; padding: 5px 6px;
                font-size: 13px;
            }
            QPushButton {
                background: #f3f5f8; color: #161616; border: 1px solid #d6dce4;
                border-radius: 6px; padding: 6px 14px;
            }
            QPushButton:hover { background: #e7ebf1; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        heading = QLabel("Export service history between two dates:")
        heading.setAlignment(Qt.AlignCenter)
        heading.setWordWrap(True)
        layout.addWidget(heading)

        today_display = date.today().strftime("%d-%m-%Y")

        from_label = QLabel("From Date")
        from_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(from_label)
        self.from_edit = QLineEdit(today_display)
        self.from_edit.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.from_edit)

        till_label = QLabel("Till Date")
        till_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(till_label)
        self.till_edit = QLineEdit(today_display)
        self.till_edit.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.till_edit)

        layout.addStretch(1)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons_row.addWidget(cancel_btn)

        pdf_btn = QPushButton(" Create PDF")
        pdf_icon_path = _ASSETS / "pdf_icon.png"
        if pdf_icon_path.exists():
            pdf_btn.setIcon(QIcon(str(pdf_icon_path)))
            pdf_btn.setIconSize(QSize(18, 18))
        pdf_btn.clicked.connect(self._on_create_pdf)
        buttons_row.addWidget(pdf_btn)
        layout.addLayout(buttons_row)

    def _on_create_pdf(self):
        from_date = _parse_display_date(self.from_edit.text())
        till_date = _parse_display_date(self.till_edit.text())
        if from_date is None or till_date is None:
            QMessageBox.warning(self, "Invalid date", "Enter both dates as DD-MM-YYYY.")
            return
        if from_date > till_date:
            QMessageBox.warning(self, "Invalid range", "'From' date must not be after 'Till' date.")
            return

        row = db.get_vehicle(self.conn, self.vehicle_id)
        plate = (row["plate"] or "vehicle").strip().replace(" ", "_") if row else "vehicle"
        default_name = f"{plate}_MaintenanceLog_{from_date.isoformat()}_to_{till_date.isoformat()}.pdf"
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Export Maintenance Log PDF", default_name, "PDF Files (*.pdf)"
        )
        if not save_path:
            return
        if not save_path.lower().endswith(".pdf"):
            save_path += ".pdf"

        try:
            _generate_maintenance_pdf(self.conn, self.vehicle_id, from_date, till_date, save_path)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not create the PDF:\n{exc}")
            return

        QMessageBox.information(self, "Export complete", f"PDF saved to:\n{save_path}")
        self.accept()


# --------------------------------------------------------------- PDF export
# Phase 28hh: draws the Vehicle Maintenance Log's content (vehicle info +
# picture + all 5 summary cards + the service history table) directly with
# QPainter onto a QPdfWriter, rather than grabbing the live on-screen
# widgets -- this keeps the exported layout's proportions independent of
# whatever size/DPI the window happens to be showing at on the exporting
# machine, so it reliably fits a fixed A4 portrait page every time. Every
# piece of text/formatting reuses the exact same helpers the live window
# uses (_display_date, _format_number, _is_expired, _cards_data_for) so
# the PDF always matches what's on screen.

# Column (header text, column const, width weight, data-cell alignment).
# Header text uses "\n" for the project owner's reference PDF's two-line
# wrapped headers (e.g. "Start"/"Date") -- QPainter's rect drawText splits
# on embedded newlines on its own, no QTextDocument needed. Weights sum to
# 1.0; Details is deliberately the dominant column, matching the sample.
_PDF_TABLE_COLUMNS = [
    ("Start\nDate", SC_START, 0.08, Qt.AlignCenter),
    ("End\nDate", SC_END, 0.08, Qt.AlignCenter),
    ("Service\nType", SC_TYPE, 0.12, Qt.AlignLeft | Qt.AlignVCenter),
    ("Details", SC_DETAILS, 0.36, Qt.AlignLeft | Qt.AlignVCenter),
    ("Current\nReading", SC_CURRENT, 0.09, Qt.AlignRight | Qt.AlignVCenter),
    ("Next\nReading", SC_NEXT, 0.09, Qt.AlignRight | Qt.AlignVCenter),
    ("Qty", SC_QTY, 0.05, Qt.AlignCenter),
    ("Person", SC_PERSON, 0.07, Qt.AlignLeft | Qt.AlignVCenter),
    ("Workshop", SC_WORKSHOP, 0.06, Qt.AlignLeft | Qt.AlignVCenter),
]

# The project owner's reference PDF (MISC/pdf_sample.pdf) always shows a
# minimum number of ruled rows, padding with blank shaded/bordered rows
# when there are fewer real records -- a printed log convention (rows
# ready for the planner to hand-fill later), not a live-table behavior.
_PDF_MIN_TABLE_ROWS = 10


def _record_in_range(record, from_date, till_date):
    """The project owner's own confirmed rule: the From field is compared
    against each record's Start Date, the Till field against its End
    Date -- a record only appears in the PDF if both hold. Records with
    no parseable start/end date (blank rows) never match a range and are
    excluded, same as they'd contribute nothing meaningful to a printed
    report."""
    start = _parse_iso_date(record["start_date"])
    end = _parse_iso_date(record["end_date"])
    if start is None or end is None:
        return False
    return start >= from_date and end <= till_date


def _pdf_draw_header(painter, page_w, y):
    # Matches MISC/pdf_sample.pdf exactly: just the bold title, top-left --
    # no registration number or anything else on this line.
    title_font = QFont()
    title_font.setPointSize(15)
    title_font.setBold(True)
    painter.setFont(title_font)
    painter.setPen(QColor("#161616"))
    # 15pt measures 31 device px tall on this writer (150 DPI) --
    # confirmed via QFontMetricsF against a real QPdfWriter, not guessed;
    # a too-short rect here clips the text's top/bottom.
    title_h = 34
    painter.drawText(QRectF(0, y, page_w, title_h), Qt.AlignLeft | Qt.AlignVCenter, "VEHICLE MAINTENANCE LOG")
    return y + title_h + 8


def _pdf_draw_vehicle_info(painter, page_w, y, row):
    """Matches MISC/pdf_sample.pdf: two label/value column-pairs on plain
    text lines (no boxes/cards here) -- a 5-row left pair (Model/Year,
    Type, Plate, Chassis #, Engine #) and a 4-row right pair (RTA Cert.,
    RTA Cert Expiry, Ad Cert., Ad Cert. Expiry), sharing the same row
    grid. Only Model/Year and Plate are bold in the sample -- every other
    value is regular weight. The sample shows Plate as plain text (no
    plate-image graphic), so this intentionally does not draw
    plate_background.png -- that graphic is a screen-only affordance.
    The picture sits top-right, sized to roughly span the same height as
    the 5-row text block, same as the sample."""
    picture_w, picture_h = 200, 100
    col1_label_w = 78
    col1_value_x = col1_label_w + 4
    col2_label_x = 300
    col2_label_w = 110
    col2_value_x = col2_label_x + col2_label_w + 4
    text_right = page_w - picture_w - 16

    label_font = QFont()
    label_font.setPointSize(9)
    bold_value_font = QFont()
    bold_value_font.setPointSize(9)
    bold_value_font.setBold(True)
    plain_value_font = QFont()
    plain_value_font.setPointSize(9)

    left_fields = [
        ("Model/Year:", f"{row['vehicle_model'] or ''} {row['vehicle_year'] or ''}".strip() or "--", True),
        ("Type:", row["vehicle_type"] or "--", False),
        ("Plate:", row["plate"] or "--", True),
        ("Chasis #:", row["vehicle_chassis"] or "--", False),
        ("Engine #:", row["vehicle_engine"] or "--", False),
    ]
    right_fields = [
        ("RTA Cert.", row["rta_certificate"] or "--"),
        ("RTA Cert Expiry", _display_date(row["rta_certificate_expiry"])),
        ("Ad Cert.", row["ad_certificate"] or "--"),
        ("Ad Cert. Expiry", _display_date(row["ad_certificate_expiry"])),
    ]

    # 9pt measures 19 device px tall on this writer (150 DPI) -- confirmed
    # via QFontMetricsF, not guessed; 20 leaves a 1px buffer.
    line_h = 20
    start_y = y

    for i, (label, value, is_bold) in enumerate(left_fields):
        row_y = start_y + i * line_h
        painter.setFont(label_font)
        painter.setPen(QColor("#161616"))
        painter.drawText(QRectF(0, row_y, col1_label_w, line_h), Qt.AlignLeft | Qt.AlignVCenter, label)
        painter.setFont(bold_value_font if is_bold else plain_value_font)
        painter.drawText(QRectF(col1_value_x, row_y, col2_label_x - col1_value_x - 8, line_h),
                          Qt.AlignLeft | Qt.AlignVCenter, str(value))

    for i, (label, value) in enumerate(right_fields):
        row_y = start_y + i * line_h
        painter.setFont(label_font)
        painter.setPen(QColor("#161616"))
        painter.drawText(QRectF(col2_label_x, row_y, col2_label_w, line_h), Qt.AlignLeft | Qt.AlignVCenter, label)
        painter.setFont(plain_value_font)
        painter.drawText(QRectF(col2_value_x, row_y, text_right - col2_value_x, line_h),
                          Qt.AlignLeft | Qt.AlignVCenter, str(value))

    if row["vehicle_picture"]:
        pixmap = QPixmap()
        pixmap.loadFromData(row["vehicle_picture"])
        if not pixmap.isNull():
            scaled = pixmap.scaled(picture_w, picture_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            px = page_w - scaled.width()
            painter.drawPixmap(int(px), int(start_y), scaled)

    return start_y + max(len(left_fields) * line_h, picture_h) + 12


def _pdf_draw_cards(painter, page_w, y, cards_data):
    """Matches MISC/pdf_sample.pdf: borderless pastel rounded rects, bold
    near-black (not the live window's grey) centered title, bold date
    (red only when _is_expired -- same rule the live cards use, via
    _cards_data_for), and a plain-weight centered extra line."""
    gap = 6
    card_w = (page_w - gap * 4) / 5
    # Internal rect heights below are sized from QFontMetricsF measured
    # against a real QPdfWriter at this 150 DPI (7pt=15px, 8pt=17px), not
    # guessed -- a too-short rect clips the text's top/bottom.
    card_h = 66

    x = 0.0
    for i, (title, icon_file, date_iso, extra_text, bg_color) in enumerate(cards_data):
        rect = QRectF(x, y, card_w, card_h)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(bg_color))
        painter.drawRoundedRect(rect, 6, 6)

        title_font = QFont()
        title_font.setPointSize(7)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor("#1a1f29"))
        painter.drawText(QRectF(x + 3, y + 4, card_w - 6, 16), Qt.AlignCenter, title)

        date_font = QFont()
        date_font.setPointSize(8)
        date_font.setBold(True)
        painter.setFont(date_font)
        # Only card 0 (Vehicle Expiry) is a real expiry date -- see the
        # matching note on _refresh_cards().
        painter.setPen(QColor(_EXPIRED_COLOR) if i == 0 and _is_expired(date_iso) else QColor("#1a1f29"))
        painter.drawText(QRectF(x + 3, y + 21, card_w - 6, 18), Qt.AlignCenter, _display_date(date_iso))

        if extra_text:
            extra_font = QFont()
            extra_font.setPointSize(7)
            painter.setFont(extra_font)
            painter.setPen(QColor("#1a1f29"))
            painter.drawText(QRectF(x + 3, y + 41, card_w - 6, card_h - 45),
                              Qt.AlignCenter | Qt.TextWordWrap, str(extra_text))

        x += card_w + gap

    return y + card_h + 14


def _pdf_col_widths(page_w):
    return [(label, w, page_w * weight, align) for label, w, weight, align in _PDF_TABLE_COLUMNS]


def _pdf_row_values(r):
    return {
        SC_START: _display_date(r["start_date"]),
        SC_END: _display_date(r["end_date"]),
        SC_TYPE: r["service_type"] or "",
        SC_DETAILS: r["details"] or "",
        SC_CURRENT: _format_number(r["current_reading"]),
        SC_NEXT: _format_number(r["next_reading"]),
        SC_QTY: _format_number(r["qty"]),
        SC_PERSON: r["person"] or "",
        SC_WORKSHOP: r["workshop"] or "",
    }


def _pdf_draw_service_table(painter, writer, page_w, page_h, y, records):
    """Matches MISC/pdf_sample.pdf: a fully-ruled grid (both horizontal
    and vertical border lines, header and data cells alike), a light
    gray-blue header with two-line wrapped column titles, and every data
    row (real or padding) uniformly tinted light blue -- no caption line
    above it and no alternating white/tint banding, unlike the live
    on-screen table. Pads with blank ruled rows up to
    _PDF_MIN_TABLE_ROWS when there are fewer real records, same as the
    sample -- a printed-log convention, see _PDF_MIN_TABLE_ROWS."""
    cols = _pdf_col_widths(page_w)
    # 8pt measures 17 device px tall on this writer (150 DPI), confirmed
    # via QFontMetricsF, not guessed -- row_h covers one line with a 1px
    # buffer; header_h covers the two-line wrapped headers (2x17=34) with
    # a small buffer.
    row_h = 18
    header_h = 36
    grid_pen = QPen(QColor("#b9c2d6"))
    grid_pen.setWidthF(0.6)

    header_font = QFont()
    header_font.setPointSize(8)
    header_font.setBold(True)
    row_font = QFont()
    row_font.setPointSize(8)

    def draw_table_header(y):
        painter.setPen(grid_pen)
        painter.setBrush(QColor("#e1e5f0"))
        painter.drawRect(QRectF(0, y, page_w, header_h))
        painter.setFont(header_font)
        painter.setPen(QColor("#1a1f29"))
        x = 0.0
        for label, _col, w, _align in cols:
            painter.drawText(QRectF(x, y, w, header_h), Qt.AlignCenter, label)
            x += w
        # vertical separators
        painter.setPen(grid_pen)
        x = 0.0
        for _label, _col, w, _align in cols:
            painter.drawLine(QRectF(x, y, 0, header_h).topLeft(), QRectF(x, y, 0, header_h).bottomLeft())
            x += w
        painter.drawLine(QRectF(page_w, y, 0, header_h).topLeft(), QRectF(page_w, y, 0, header_h).bottomLeft())
        return y + header_h

    y = draw_table_header(y)
    painter.setFont(row_font)

    display_rows = list(records)
    if len(display_rows) < _PDF_MIN_TABLE_ROWS:
        display_rows += [None] * (_PDF_MIN_TABLE_ROWS - len(display_rows))

    for r in display_rows:
        if y + row_h > page_h:
            writer.newPage()
            y = 0
            y = draw_table_header(y)
            painter.setFont(row_font)

        painter.setPen(grid_pen)
        painter.setBrush(QColor("#eef1fa"))
        painter.drawRect(QRectF(0, y, page_w, row_h))
        x = 0.0
        for _label, _col, w, _align in cols:
            painter.drawLine(QRectF(x, y, 0, row_h).topLeft(), QRectF(x, y, 0, row_h).bottomLeft())
            x += w
        painter.drawLine(QRectF(page_w, y, 0, row_h).topLeft(), QRectF(page_w, y, 0, row_h).bottomLeft())

        if r is not None:
            values = _pdf_row_values(r)
            painter.setPen(QColor("#161616"))
            x = 0.0
            for _label, col, w, align in cols:
                pad = 4
                painter.drawText(QRectF(x + pad, y, w - pad * 2, row_h), align, str(values[col]))
                x += w
        y += row_h


def _generate_maintenance_pdf(conn, vehicle_id, from_date, till_date, save_path):
    """Writes the Vehicle Maintenance Log (vehicle info + picture + the 5
    summary cards + the service history table, the table filtered to
    [from_date, till_date] per _record_in_range's rule) to save_path as a
    single A4-portrait PDF. Purely a reader of the database -- takes no
    reference to any live dialog widget, so it can never affect the
    on-screen (unfiltered) service table."""
    row = db.get_vehicle(conn, vehicle_id)
    if row is None:
        raise ValueError("Vehicle not found.")
    records = db.list_service_records(conn, vehicle_id)
    cards_data = _cards_data_for(row, records)
    filtered = [r for r in records if _record_in_range(r, from_date, till_date)]

    writer = QPdfWriter(save_path)
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageOrientation(QPageLayout.Portrait)
    writer.setResolution(150)
    writer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Millimeter)

    painter = QPainter(writer)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        page_w = writer.width()
        page_h = writer.height()

        y = 0
        y = _pdf_draw_header(painter, page_w, y)
        y = _pdf_draw_vehicle_info(painter, page_w, y, row)
        y = _pdf_draw_cards(painter, page_w, y, cards_data)
        _pdf_draw_service_table(painter, writer, page_w, page_h, y, filtered)
    finally:
        painter.end()
