"""
settings_tab.py

Where the planner pastes in their Anthropic and Google Maps API keys.
Stored locally in the SQLite file only -- never sent anywhere except
directly to Anthropic/Google when the app makes a real request.

Includes a "Test" button for each key so the planner gets an immediate,
clear pass/fail instead of only finding out a key is wrong in the middle
of running a full day's plan.
"""
from datetime import datetime
import hashlib

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QFormLayout
)
from PySide6.QtGui import QFont
from PySide6.QtCore import Signal

from app import db, maps_client, digest_generator
from app.ai_review import AIReviewError

ANTHROPIC_KEY_SETTING = "anthropic_api_key"
GOOGLE_MAPS_KEY_SETTING = "google_maps_api_key"
GEMINI_TEST_KEY_SETTING = "gemini_test_api_key"
ORS_TEST_KEY_SETTING = "openrouteservice_api_key"
PIN_HASH_SETTING = "settings_pin_hash"


def hash_pin(pin):
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def pin_is_set(conn):
    return bool(db.get_setting(conn, PIN_HASH_SETTING))


def verify_pin(conn, pin):
    stored = db.get_setting(conn, PIN_HASH_SETTING)
    if not stored:
        return True  # no PIN configured yet -- nothing to check against
    return hash_pin(pin) == stored


class SettingsTab(QWidget):
    pin_changed = Signal()

    def __init__(self, conn, parent=None):
        super().__init__(parent)
        self.conn = conn
        self._build_ui()
        self._load_existing()
        self._refresh_digest_status_display()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("API Keys"))

        note = QLabel(
            "Both keys are stored locally on this PC only, in this app's own database file. "
            "They are never sent anywhere except directly to Anthropic or Google when the app "
            "makes an actual request."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(note)

        form = QFormLayout()

        self.anthropic_input = QLineEdit()
        self.anthropic_input.setEchoMode(QLineEdit.Password)
        self.anthropic_input.setPlaceholderText("sk-ant-...")
        anthropic_row = QHBoxLayout()
        anthropic_row.addWidget(self.anthropic_input)
        anthropic_test_btn = QPushButton("Test")
        anthropic_test_btn.clicked.connect(self._on_test_anthropic)
        anthropic_row.addWidget(anthropic_test_btn)
        form.addRow("Anthropic (Claude) API Key:", anthropic_row)

        self.maps_input = QLineEdit()
        self.maps_input.setEchoMode(QLineEdit.Password)
        self.maps_input.setPlaceholderText("AIza...")
        maps_row = QHBoxLayout()
        maps_row.addWidget(self.maps_input)
        maps_test_btn = QPushButton("Test")
        maps_test_btn.clicked.connect(self._on_test_maps)
        maps_row.addWidget(maps_test_btn)
        form.addRow("Google Maps API Key:", maps_row)

        layout.addLayout(form)

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._on_save)
        layout.addWidget(save_btn)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addWidget(QLabel(" "))
        layout.addWidget(QLabel("Free/Testing AI Provider (optional)"))
        gemini_note = QLabel(
            "Only used for AI Review when NO Anthropic key above is set -- Anthropic is always "
            "preferred if both are configured. Google Gemini's free tier (no credit card, generous "
            "daily limit) lets you try the AI Review flow without spending anything, but its "
            "suggestions won't be as strong as Claude's. Get a free key at aistudio.google.com, "
            "then paste it below. Needs the 'google-genai' Python package installed."
        )
        gemini_note.setWordWrap(True)
        gemini_note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(gemini_note)

        gemini_form = QFormLayout()
        self.gemini_input = QLineEdit()
        self.gemini_input.setEchoMode(QLineEdit.Password)
        self.gemini_input.setPlaceholderText("AIza... (free tier)")
        gemini_row = QHBoxLayout()
        gemini_row.addWidget(self.gemini_input)
        gemini_test_btn = QPushButton("Test")
        gemini_test_btn.clicked.connect(self._on_test_gemini)
        gemini_row.addWidget(gemini_test_btn)
        gemini_form.addRow("Google Gemini API Key (free tier):", gemini_row)
        layout.addLayout(gemini_form)

        gemini_save_btn = QPushButton("Save")
        gemini_save_btn.clicked.connect(self._on_save)
        layout.addWidget(gemini_save_btn)

        layout.addWidget(QLabel(" "))
        layout.addWidget(QLabel("Free/Testing Maps Provider (optional)"))
        ors_note = QLabel(
            "Only used for map/travel-time lookups when NO Google Maps key above is set -- Google "
            "is always preferred if both are configured. OpenRouteService's free tier (2,500 "
            "requests/day, no credit card ever) covers both address lookup and routing. "
            "IMPORTANT: its travel times are NOT traffic-aware -- they're average road-speed "
            "estimates -- so a tight rush-hour connection will look more optimistic than it really "
            "is. Fine for a rough picture, not equal to Google. Get a free key at "
            "openrouteservice.org/dev."
        )
        ors_note.setWordWrap(True)
        ors_note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(ors_note)

        ors_form = QFormLayout()
        self.ors_input = QLineEdit()
        self.ors_input.setEchoMode(QLineEdit.Password)
        self.ors_input.setPlaceholderText("5b3ce... (free tier)")
        ors_row = QHBoxLayout()
        ors_row.addWidget(self.ors_input)
        ors_test_btn = QPushButton("Test")
        ors_test_btn.clicked.connect(self._on_test_ors)
        ors_row.addWidget(ors_test_btn)
        ors_form.addRow("OpenRouteService API Key (free tier):", ors_row)
        layout.addLayout(ors_form)

        ors_save_btn = QPushButton("Save")
        ors_save_btn.clicked.connect(self._on_save)
        layout.addWidget(ors_save_btn)

        layout.addWidget(QLabel(" "))
        layout.addWidget(QLabel("Planner Preferences Digest"))
        digest_note = QLabel(
            "A short, fixed-size summary of your accepted/rejected AI suggestions over time. "
            "This is the only history ever sent to the AI for daily reasoning — the full log "
            "stays local and never grows the cost of a daily run. Refresh it periodically "
            "(e.g. monthly) to fold in recent decisions."
        )
        digest_note.setWordWrap(True)
        digest_note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(digest_note)

        self.digest_status_label = QLabel("")
        self.digest_status_label.setWordWrap(True)
        layout.addWidget(self.digest_status_label)

        self.digest_preview = QLabel("")
        self.digest_preview.setWordWrap(True)
        self.digest_preview.setStyleSheet("font-style: italic; color: #aaaaaa;")
        layout.addWidget(self.digest_preview)

        refresh_btn = QPushButton("Refresh Preferences Digest Now")
        refresh_btn.clicked.connect(self._on_refresh_digest)
        layout.addWidget(refresh_btn)

        layout.addWidget(QLabel(" "))
        layout.addWidget(QLabel("Settings PIN"))
        pin_note = QLabel(
            "Not a real security feature -- just a small speed bump so this tab (API keys, "
            "digest refresh) isn't something you or someone else can change by accident. "
            "Leave blank to disable."
        )
        pin_note.setWordWrap(True)
        pin_note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(pin_note)

        pin_form = QFormLayout()
        self.new_pin_input = QLineEdit()
        self.new_pin_input.setEchoMode(QLineEdit.Password)
        self.new_pin_input.setPlaceholderText("Enter a new PIN (blank = remove PIN)")
        pin_form.addRow("Set / Change PIN:", self.new_pin_input)
        layout.addLayout(pin_form)

        set_pin_btn = QPushButton("Save PIN")
        set_pin_btn.clicked.connect(self._on_set_pin)
        layout.addWidget(set_pin_btn)

        self.pin_status_label = QLabel("")
        layout.addWidget(self.pin_status_label)

        layout.addStretch()

    def _on_set_pin(self):
        new_pin = self.new_pin_input.text().strip()
        if new_pin:
            db.set_setting(self.conn, PIN_HASH_SETTING, hash_pin(new_pin))
            self.pin_status_label.setText("PIN set. You'll be asked for it next time you open Settings.")
        else:
            db.set_setting(self.conn, PIN_HASH_SETTING, "")
            self.pin_status_label.setText("PIN removed -- Settings is now open access.")
        self.pin_status_label.setStyleSheet("color: #2a7a2a;")
        self.new_pin_input.clear()
        self.pin_changed.emit()

    def _refresh_digest_status_display(self):
        digest = db.get_digest(self.conn)
        pending = db.count_undigested_decisions(self.conn)
        if digest and digest["last_refreshed_at"]:
            self.digest_status_label.setText(
                f"Last refreshed: {digest['last_refreshed_at']}  |  "
                f"Covers decisions through: {digest['covered_through_date']}  |  "
                f"{pending} new decision(s) not yet folded in"
            )
            self.digest_preview.setText(digest["digest_text"][:300] + ("..." if len(digest["digest_text"]) > 300 else ""))
        else:
            self.digest_status_label.setText(f"Never refreshed yet.  |  {pending} decision(s) logged so far")
            self.digest_preview.setText("")

    def _on_refresh_digest(self):
        key = self.anthropic_input.text().strip()
        if not key:
            QMessageBox.information(self, "No key", "Enter and save your Anthropic API key first.")
            return
        try:
            digest_generator.refresh_digest(key, self.conn, db)
        except digest_generator.DigestError as e:
            QMessageBox.warning(self, "Digest refresh failed", str(e))
            return
        self._refresh_digest_status_display()
        QMessageBox.information(self, "Done", "Preferences digest refreshed.")

    def _load_existing(self):
        existing_anthropic = db.get_setting(self.conn, ANTHROPIC_KEY_SETTING)
        existing_maps = db.get_setting(self.conn, GOOGLE_MAPS_KEY_SETTING)
        existing_gemini = db.get_setting(self.conn, GEMINI_TEST_KEY_SETTING)
        existing_ors = db.get_setting(self.conn, ORS_TEST_KEY_SETTING)
        if existing_anthropic:
            self.anthropic_input.setText(existing_anthropic)
        if existing_maps:
            self.maps_input.setText(existing_maps)
        if existing_gemini:
            self.gemini_input.setText(existing_gemini)
        if existing_ors:
            self.ors_input.setText(existing_ors)

    def _on_save(self):
        db.set_setting(self.conn, ANTHROPIC_KEY_SETTING, self.anthropic_input.text().strip())
        db.set_setting(self.conn, GOOGLE_MAPS_KEY_SETTING, self.maps_input.text().strip())
        db.set_setting(self.conn, GEMINI_TEST_KEY_SETTING, self.gemini_input.text().strip())
        db.set_setting(self.conn, ORS_TEST_KEY_SETTING, self.ors_input.text().strip())
        self.status_label.setText("Saved.")
        self.status_label.setStyleSheet("color: #2a7a2a;")

    def _on_test_anthropic(self):
        key = self.anthropic_input.text().strip()
        if not key:
            QMessageBox.information(self, "No key", "Enter a key first.")
            return
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=key)
            client.messages.create(
                model="claude-sonnet-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "Say OK"}],
            )
            self.status_label.setText("Anthropic key works.")
            self.status_label.setStyleSheet("color: #2a7a2a;")
        except Exception as e:
            self.status_label.setText(f"Anthropic key test failed: {e}")
            self.status_label.setStyleSheet("color: #a03030;")

    def _on_test_gemini(self):
        key = self.gemini_input.text().strip()
        if not key:
            QMessageBox.information(self, "No key", "Enter a key first.")
            return
        try:
            from google import genai
        except ImportError as e:
            self.status_label.setText(f"'google-genai' package not installed: {e}")
            self.status_label.setStyleSheet("color: #a03030;")
            return
        try:
            from app.ai_review import GEMINI_MODEL
            client = genai.Client(api_key=key)
            client.models.generate_content(model=GEMINI_MODEL, contents="Say OK")
            self.status_label.setText("Gemini key works.")
            self.status_label.setStyleSheet("color: #2a7a2a;")
        except Exception as e:
            self.status_label.setText(f"Gemini key test failed: {e}")
            self.status_label.setStyleSheet("color: #a03030;")

    def _on_test_ors(self):
        key = self.ors_input.text().strip()
        if not key:
            QMessageBox.information(self, "No key", "Enter a key first.")
            return
        try:
            # Geocode is the cheapest real round-trip that proves the key
            # works -- same spirit as the Google Maps test below.
            result = maps_client.geocode_address_ors(key, "Dubai World Trade Centre")
            self.status_label.setText(
                f"OpenRouteService key works. Test lookup resolved to "
                f"{result['lat']:.4f}, {result['lon']:.4f}."
            )
            self.status_label.setStyleSheet("color: #2a7a2a;")
        except maps_client.MapsClientError as e:
            self.status_label.setText(f"OpenRouteService key test failed: {e}")
            self.status_label.setStyleSheet("color: #a03030;")

    def _on_test_maps(self):
        key = self.maps_input.text().strip()
        if not key:
            QMessageBox.information(self, "No key", "Enter a key first.")
            return
        try:
            result = maps_client.get_travel_time(key, "Dubai World Trade Centre", "Meydan Dubai", datetime.now())
            self.status_label.setText(f"Google Maps key works. Test lookup: {result['duration_minutes']} min, {result['distance_km']} km.")
            self.status_label.setStyleSheet("color: #2a7a2a;")
        except maps_client.MapsClientError as e:
            self.status_label.setText(f"Google Maps key test failed: {e}")
            self.status_label.setStyleSheet("color: #a03030;")
