from PySide6.QtWidgets import QMainWindow, QTabWidget, QInputDialog, QLineEdit, QMessageBox, QPushButton

from app import db
from app.ui.drivers_tab import DriversTab
from app.ui.suppliers_tab import SuppliersTab
from app.ui.vehicles_tab import VehiclesTab
from app.ui.plan_day_tab import PlanDayTab
from app.ui.settings_tab import SettingsTab, pin_is_set, verify_pin
from app.ui.map_tab import MapTab
from app.ui.schedules_tab import SchedulesTab


class MainWindow(QMainWindow):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setWindowTitle("Fleet Planner - Master Data")
        self.resize(1000, 650)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        plan_day_tab = PlanDayTab(conn)
        self.tabs.addTab(plan_day_tab, "Plan a Day")

        drivers_tab = DriversTab(conn)
        self.tabs.addTab(drivers_tab, "Drivers")

        suppliers_tab = SuppliersTab(conn)
        self.tabs.addTab(suppliers_tab, "Suppliers")

        vehicles_tab = VehiclesTab(conn)
        self.tabs.addTab(vehicles_tab, "Vehicles")

        # MapTab takes a read-only reference to the Plan a Day tab so its
        # "Run Locations" button can pull whatever plan is currently loaded
        # there. It never mutates plan_day_tab -- see map_tab.py. Cross-tab
        # wiring has precedent here already (settings_widget.pin_changed
        # below; plan_day_tab importing settings_tab's key constants).
        locations_tab = MapTab(conn, plan_day_tab=plan_day_tab)
        self.tabs.addTab(locations_tab, "Locations")

        schedules_tab = SchedulesTab(conn)
        self.tabs.addTab(schedules_tab, "Schedules")

        settings_tab = SettingsTab(conn)
        self.settings_tab_index = self.tabs.addTab(settings_tab, "Settings")

        # The Settings tab is DISABLED (not just switched-away-from) while
        # locked, so its contents can never be clicked into or flash on
        # screen even briefly before a PIN is checked.
        self.unlock_btn = QPushButton("🔒 Unlock Settings")
        self.unlock_btn.clicked.connect(self._on_unlock_clicked)
        self.tabs.setCornerWidget(self.unlock_btn)
        self._apply_lock_state()

    def _apply_lock_state(self):
        locked = pin_is_set(self.conn)
        self.tabs.setTabEnabled(self.settings_tab_index, not locked)
        self.unlock_btn.setVisible(locked)

    def _on_unlock_clicked(self):
        pin, ok = QInputDialog.getText(self, "Unlock Settings", "Enter PIN:", QLineEdit.Password)
        if not ok:
            return
        if verify_pin(self.conn, pin):
            self.tabs.setTabEnabled(self.settings_tab_index, True)
            self.unlock_btn.setVisible(False)
            self.tabs.setCurrentIndex(self.settings_tab_index)
            # Re-lock next time the PIN changes or the app restarts --
            # if the planner just removed the PIN from within Settings,
            # re-check lock state so the corner button reflects that.
            settings_widget = self.tabs.widget(self.settings_tab_index)
            settings_widget.pin_changed.connect(self._apply_lock_state)
        else:
            QMessageBox.warning(self, "Incorrect PIN", "That PIN is incorrect.")
