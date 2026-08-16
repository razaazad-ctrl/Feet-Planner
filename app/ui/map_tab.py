"""
map_tab.py

The Locations screen, rebuilt (2026-08-16) into a three-panel operations
view modeled on the voyage-planning UI the project owner supplied as
reference:

  LEFT   -- the original short-code -> address editor (unchanged in
            purpose), now also holding each location's map coordinates
            (paste-able from Google Maps, or found via "Search Place").
  CENTER -- an interactive MapLibre map (OpenFreeMap tiles: free, no key,
            no billing) drawing pins and real road-following routes.
  RIGHT  -- two stacked lists: every trip's pickup -> destination travel
            time, and each driver's whole day as a sequential chain with
            impossible connections flagged red.

Design constraints that shaped this file, all deliberate:

* COST. Routing/geocoding APIs bill per call and an 81-trip day re-run a
  few times would blow past any free tier. Every lookup goes through
  db.geocode_cache / db.travel_time_cache first, so recurring pairs (CPK
  -> Zabeel runs constantly) are fetched once and reused for good. The
  footer shows live cache stats so the saving is visible.
* NOTHING RUNS AUTOMATICALLY. Opening this tab costs nothing; lookups
  happen only when the planner clicks "Run Locations" (the project
  owner's explicit requirement -- running on every open would be
  wasteful).
* THE MAP IS DISPLAY ONLY. Nothing here feeds the deterministic
  allocation engine (Rule 10) -- allocation_engine.py is untouched. This
  informs the planner and the AI Review layer, nothing more.
* PROVIDER IS ALWAYS VISIBLE. Google (traffic-aware) and OpenRouteService
  (free, average-speed only) produce meaningfully different numbers, so
  every result is labeled with which one produced it.
"""
from datetime import datetime, timedelta
import json

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QProgressBar, QTreeWidget,
    QTreeWidgetItem, QComboBox, QDialog
)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt, QThread, QObject, Signal

from app import db, maps_client
from app.ui.settings_tab import GOOGLE_MAPS_KEY_SETTING, ORS_TEST_KEY_SETTING

INFEASIBLE_COLOR = QColor("#c0392b")
TIGHT_COLOR = QColor("#a08030")
OK_COLOR = QColor("#2a7a2a")

# A connection is "tight" (amber) rather than outright impossible when the
# drive fits, but with less than this much slack. Surfaces the connections
# worth a second look before they become tomorrow's problem.
TIGHT_MARGIN_MINUTES = 15

# Automatic geocoding rejects any result further than this from the fleet's
# usual operating area (which is derived from the predefined locations'
# own coordinates -- see MapTab._operating_focus). Added 2026-08-16 after
# raw Excel strings resolved to entirely wrong continents: "ON SITE - COCA
# COLA ARENA" -> Atlanta, "ON SITE - PALM JUMEIRAH" -> North Carolina,
# "Dubai - MENA PORT RASHID" -> Paraguay. A silently-wrong coordinate is
# far worse than a missing one: it gets cached and then poisons every
# travel time and map pin derived from it, while looking perfectly
# plausible in the UI. 500km is generous enough for any legitimate
# long-haul job while still catching a wrong-continent match.
MAX_GEOCODE_DISTANCE_KM = 500

# Hard country restriction for every place lookup -- the fleet operates in
# the UAE, so a "Coca Cola Arena" in Atlanta should never even be offered
# as a candidate. This is stronger than the distance check above (which
# stays as a second line of defence, and still catches a wrong result
# INSIDE the country). ISO 3166-1 alpha-2; both Google (components=country)
# and OpenRouteService (boundary.country) accept this form.
#
# If the business ever runs cross-border work -- Oman and Saudi are a
# realistic day trip from Dubai -- widen or clear this, otherwise a
# genuine cross-border location will silently fail to resolve. It is a
# single constant precisely so that stays a one-line change.
OPERATING_COUNTRY = "AE"

LOC_CODE, LOC_ADDRESS, LOC_LAT, LOC_LON = range(4)
TRIP_SR, TRIP_TIME, TRIP_FROM, TRIP_TO, TRIP_TRAVEL, TRIP_DIST = range(6)


def _fmt_minutes(minutes):
    if minutes is None:
        return "--"
    if minutes < 60:
        return f"{int(round(minutes))} min"
    hours, mins = divmod(int(round(minutes)), 60)
    return f"{hours}h {mins:02d}m"


def _parse_latlon(text):
    """Recognizes a coordinate pair pasted straight out of Google Maps --
    right-clicking a spot there copies exactly "25.223732, 55.288312".
    Returns {"lat": float, "lon": float} or None.

    Added 2026-08-16 after the project owner (reasonably) pasted Google
    Maps coordinates into the Address field, because that was the only
    editable field at the time. Accepting them is strictly better than
    geocoding an address: it's exact, the planner knows the precise gate
    rather than the general area, and it costs zero API calls.

    Deliberately strict about ranges so a genuine street address that
    happens to contain two numbers (e.g. "Unit 12, 45 Sheikh Zayed Rd")
    is never mistaken for coordinates.
    """
    if not text:
        return None
    parts = [p.strip() for p in str(text).replace(";", ",").split(",")]
    if len(parts) != 2:
        parts = str(text).split()
        if len(parts) != 2:
            return None
        parts = [p.strip() for p in parts]
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return {"lat": lat, "lon": lon}


def _job_label(job):
    who = ""
    if getattr(job, "assigned_driver_name", ""):
        who = job.assigned_driver_name
    elif getattr(job, "assigned_supplier_unit", None):
        who = job.assigned_supplier_unit
    return who or "(unassigned)"


class _LookupWorker(QObject):
    """Runs the network lookups (geocoding + routing) off the GUI thread --
    same QThread + worker-object pattern already proven by
    plan_day_tab.py's _SolverWorker/_AIReviewWorker.

    Takes plain pre-resolved data (never the sqlite3 connection): a
    sqlite3 connection can only be used from the thread that created it,
    so all cache reads happen on the main thread BEFORE this starts, and
    all cache writes happen on the main thread after `finished` fires.
    That's why the worker returns results rather than persisting them.
    """
    finished = Signal(list)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, provider, api_key, pending_geocodes, pending_routes,
                  focus_point=None, max_km=None, country=None):
        super().__init__()
        self.provider = provider
        self.api_key = api_key
        self.pending_geocodes = pending_geocodes   # [address, ...]
        self.pending_routes = pending_routes       # [{"origin":..,"destination":..,"hour":..,"origin_coords":..,"destination_coords":..}]
        # Bias automatic geocoding toward where this fleet actually works,
        # and REJECT anything absurdly far away rather than caching it --
        # see the note in maps_client.geocode(). Without this, raw Excel
        # strings resolved to entirely wrong continents.
        self.focus_point = focus_point
        self.max_km = max_km
        self.country = country

    def run(self):
        results = []
        total = len(self.pending_geocodes) + len(self.pending_routes)
        done = 0
        try:
            geocoded = {}
            for address in self.pending_geocodes:
                try:
                    point = maps_client.geocode(
                        self.provider, self.api_key, address,
                        focus_point=self.focus_point, max_km=self.max_km,
                        country=self.country,
                    )
                    geocoded[address] = point
                    results.append({"kind": "geocode", "address": address, "point": point})
                except maps_client.MapsClientError as e:
                    # One unresolvable address must not abort the whole run --
                    # record it and carry on, so the planner still gets every
                    # other trip's numbers.
                    results.append({"kind": "geocode", "address": address, "error": str(e)})
                done += 1
                self.progress.emit(done, total)

            for route in self.pending_routes:
                origin_coords = route.get("origin_coords") or _coords_of(geocoded, route["origin"])
                destination_coords = route.get("destination_coords") or _coords_of(geocoded, route["destination"])
                try:
                    data = maps_client.travel_time(
                        self.provider, self.api_key, route["origin"], route["destination"],
                        route["departure_dt"],
                        origin_coords=origin_coords, destination_coords=destination_coords,
                    )
                    results.append({"kind": "route", "route": route, "data": data})
                except maps_client.MapsClientError as e:
                    results.append({"kind": "route", "route": route, "error": str(e)})
                done += 1
                self.progress.emit(done, total)
        except Exception as e:  # never let a background exception vanish
            self.failed.emit(str(e))
            return
        self.finished.emit(results)


def _coords_of(geocoded, address):
    point = geocoded.get(address)
    if not point or "lat" not in point:
        return None
    return {"lat": point["lat"], "lon": point["lon"]}


# MapLibre GL + OpenFreeMap rather than Leaflet + the standard OSM raster
# tiles (changed 2026-08-16 on the project owner's first visual inspection):
# the default OSM tile server renders place labels in each region's LOCAL
# language, so the whole map came out in Arabic while every location in this
# app is written in English. Those tiles have no language switch.
#
# OpenFreeMap was picked over the usual alternatives on LICENSING, not looks:
# CARTO's hosted basemap tiles are enterprise-only, and Esri's are a
# commercial offering with restrictive terms -- neither is appropriate for a
# commercial fleet business without an agreement. OpenFreeMap is genuinely
# free: no key, no usage limits, no registration, commercial use allowed.
#
# Being vector tiles, it also lets labels be forced to English EXPLICITLY
# (see forceEnglishLabels below) rather than hoping a style's defaults
# happen to be Latin-script.
_MAP_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" />
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
  html, body, #map { height: 100%; margin: 0; background: #1e1e1e; }
  #hint { position:absolute; z-index:1000; top:8px; left:50%; transform:translateX(-50%);
          background:rgba(0,0,0,0.65); color:#eee; padding:5px 12px; border-radius:12px;
          font:12px sans-serif; pointer-events:none; }
  #err  { position:absolute; z-index:1001; top:0; left:0; right:0; bottom:0; display:none;
          align-items:center; justify-content:center; text-align:center; color:#e8b93a;
          background:#1e1e1e; font:13px sans-serif; padding:24px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="hint">Click &quot;Run Locations&quot; to plot trips</div>
<div id="err"></div>
<script>
  var map = null, loaded = false, pending = null, markers = [];

  function setHint(text) {
      var h = document.getElementById('hint');
      if (!text) { h.style.display = 'none'; return; }
      h.style.display = 'block'; h.textContent = text;
  }
  function showError(msg) {
      var e = document.getElementById('err');
      e.style.display = 'flex'; e.textContent = msg;
  }

  // Force every label layer to English. OpenFreeMap uses the OpenMapTiles
  // schema: name:en is the English name, name:latin the Latin-script
  // transliteration -- fall through both before the raw local name, so a
  // place with no English name still is not left in Arabic script.
  function forceEnglishLabels() {
      var layers = map.getStyle().layers || [];
      layers.forEach(function (layer) {
          if (layer.layout && layer.layout['text-field']) {
              map.setLayoutProperty(layer.id, 'text-field', [
                  'coalesce', ['get', 'name:en'], ['get', 'name:latin'], ['get', 'name']
              ]);
          }
      });
  }

  function emptyFC() { return {type: 'FeatureCollection', features: []}; }

  try {
      map = new maplibregl.Map({
          container: 'map',
          style: 'https://tiles.openfreemap.org/styles/liberty',
          center: [55.27, 25.20],   // Dubai. NOTE: MapLibre order is [lon, lat]
          zoom: 9
      });
      map.addControl(new maplibregl.NavigationControl({showCompass: false}), 'top-right');
      map.on('load', function () {
          loaded = true;
          forceEnglishLabels();
          map.addSource('routes', {type: 'geojson', data: emptyFC()});
          map.addLayer({
              id: 'routes-line', type: 'line', source: 'routes',
              layout: {'line-join': 'round', 'line-cap': 'round'},
              filter: ['!', ['get', 'dashed']],
              paint: {'line-color': ['get', 'color'], 'line-width': 4, 'line-opacity': 0.85}
          });
          // Dashed variant, drawn as its own layer because line-dasharray
          // cannot be data-driven in MapLibre -- it must be a paint constant.
          map.addLayer({
              id: 'routes-line-dashed', type: 'line', source: 'routes',
              layout: {'line-join': 'round', 'line-cap': 'round'},
              filter: ['==', ['get', 'dashed'], true],
              paint: {'line-color': ['get', 'color'], 'line-width': 3,
                      'line-opacity': 0.9, 'line-dasharray': [2, 1.5]}
          });
          if (pending) { applyDraw(pending); pending = null; }
      });
      map.on('error', function (e) {
          var msg = (e && e.error) ? String(e.error) : '';
          if (/webgl/i.test(msg)) {
              showError('This map needs WebGL, which this system did not provide. '
                        + 'Travel times still work -- only the map picture is missing.');
          }
      });
  } catch (e) {
      showError('Map could not start: ' + e
                + '. Travel times still work -- only the map picture is missing.');
  }

  function clearMarkers() {
      markers.forEach(function (m) { m.remove(); });
      markers = [];
  }

  // Python sends {lat, lon} and [[lat, lon], ...]; MapLibre wants [lon, lat]
  // -- the reverse. That conversion lives HERE, in one place, rather than
  // being scattered through the Python side.
  function applyDraw(payload) {
      var points = payload.points || [], lines = payload.lines || [];
      clearMarkers();
      var features = lines.filter(function (ln) { return ln.points && ln.points.length; })
          .map(function (ln) {
              return {
                  type: 'Feature',
                  properties: {color: ln.color || '#3f7ee8', dashed: !!ln.dashed},
                  geometry: {
                      type: 'LineString',
                      coordinates: ln.points.map(function (p) { return [p[1], p[0]]; })
                  }
              };
          });
      map.getSource('routes').setData({type: 'FeatureCollection', features: features});

      var bounds = new maplibregl.LngLatBounds();
      var any = false;
      features.forEach(function (f) {
          f.geometry.coordinates.forEach(function (c) { bounds.extend(c); any = true; });
      });
      points.forEach(function (p) {
          var el = document.createElement('div');
          var size = p.seq ? 20 : 14;
          el.style.cssText = 'width:' + size + 'px;height:' + size + 'px;border-radius:50%;'
                           + 'border:2px solid #fff;background:' + (p.color || '#3f7ee8')
                           + ';box-shadow:0 0 3px #000;color:#fff;font:bold 11px sans-serif;'
                           + 'display:flex;align-items:center;justify-content:center;';
          if (p.seq) { el.textContent = p.seq; }   // running order of the day
          var marker = new maplibregl.Marker({element: el}).setLngLat([p.lon, p.lat]);
          if (p.label) { marker.setPopup(new maplibregl.Popup({offset: 12}).setText(p.label)); }
          marker.addTo(map);
          markers.push(marker);
          bounds.extend([p.lon, p.lat]);
          any = true;
      });
      if (any) { map.fitBounds(bounds, {padding: 60, maxZoom: 14, duration: 400}); }
      setHint(payload.hint || '');
  }

  // Called from Python. Queues if the style has not finished loading yet --
  // an early call would otherwise throw on the not-yet-added 'routes' source.
  function draw(points, lines, hint) {
      var payload = {points: points, lines: lines, hint: hint};
      if (!map || !loaded) { pending = payload; return; }
      applyDraw(payload);
  }
</script>
</body>
</html>
"""


class _PlaceSearchDialog(QDialog):
    """Type a place name, see the real candidates, pick the right one.

    Exists because silently trusting a geocoder's first guess produced
    genuinely wrong coordinates for ambiguous names (COCA COLA ARENA ->
    Atlanta, PALM JUMEIRAH -> North Carolina). For a name that exists in
    several countries there is no algorithm that reliably picks the right
    one -- but a planner glancing at "Coca-Cola Arena, City Walk, Dubai"
    versus "World of Coca-Cola, Atlanta" gets it right instantly. Results
    are biased toward the fleet's operating area and each shows how far
    away it is, so a wrong continent is obvious at a glance.
    """

    def __init__(self, provider, api_key, focus_point, initial_text="", country=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Search Place")
        self.resize(620, 420)
        self.provider = provider
        self.api_key = api_key
        self.focus_point = focus_point
        self.country = country
        self.chosen = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Search for a place, then pick the correct match:"))

        row = QHBoxLayout()
        self.search_input = QLineEdit(initial_text)
        self.search_input.setPlaceholderText("e.g. Coca Cola Arena")
        self.search_input.returnPressed.connect(self._on_search)
        row.addWidget(self.search_input, 1)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._on_search)
        row.addWidget(search_btn)
        layout.addLayout(row)

        self.results = QTableWidget(0, 3)
        self.results.setHorizontalHeaderLabels(["Place", "Distance", "Coordinates"])
        self.results.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.results.setColumnWidth(1, 90)
        self.results.setColumnWidth(2, 170)
        self.results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.results.setSelectionBehavior(QTableWidget.SelectRows)
        self.results.doubleClicked.connect(self._on_use)
        layout.addWidget(self.results, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(self.note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        use_btn = QPushButton("Use Selected")
        use_btn.clicked.connect(self._on_use)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(use_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        self._candidates = []
        if initial_text.strip():
            self._on_search()

    def _on_search(self):
        text = self.search_input.text().strip()
        if not text:
            return
        self.note.setText("Searching...")
        try:
            self._candidates = maps_client.geocode_candidates(
                self.provider, self.api_key, text,
                focus_point=self.focus_point, country=self.country,
            )
        except maps_client.MapsClientError as e:
            self.note.setText(str(e))
            self.note.setStyleSheet("color: #c0392b; font-size: 11px;")
            return
        self.note.setStyleSheet("color: #888888; font-size: 11px;")
        self.results.setRowCount(0)
        for cand in self._candidates:
            row = self.results.rowCount()
            self.results.insertRow(row)
            distance_text = "--"
            if self.focus_point:
                km = maps_client.haversine_km(
                    self.focus_point["lat"], self.focus_point["lon"], cand["lat"], cand["lon"]
                )
                distance_text = f"{km:,.0f} km"
            label_item = QTableWidgetItem(cand["label"])
            dist_item = QTableWidgetItem(distance_text)
            coord_item = QTableWidgetItem(f"{cand['lat']:.6f}, {cand['lon']:.6f}")
            dist_item.setTextAlignment(Qt.AlignCenter)
            coord_item.setTextAlignment(Qt.AlignCenter)
            # Anything absurdly far from where this fleet works is almost
            # certainly the wrong continent -- flag it rather than letting
            # it look like an equally valid option.
            if self.focus_point and km > 500:
                for item in (label_item, dist_item, coord_item):
                    item.setForeground(INFEASIBLE_COLOR)
            self.results.setItem(row, 0, label_item)
            self.results.setItem(row, 1, dist_item)
            self.results.setItem(row, 2, coord_item)
        if not self._candidates:
            self.note.setText("No matches found. Try a simpler name, or paste coordinates from Google Maps.")
        else:
            self.results.selectRow(0)
            self.note.setText(
                f"{len(self._candidates)} match(es). Distances are from your usual operating area -- "
                f"anything far away (red) is very likely the wrong place."
            )

    def _on_use(self):
        row = self.results.currentRow()
        if row < 0 or row >= len(self._candidates):
            return
        self.chosen = self._candidates[row]
        self.accept()


class MapTab(QWidget):
    def __init__(self, conn, plan_day_tab=None, parent=None):
        super().__init__(parent)
        self.conn = conn
        # Read-only reference to the Plan a Day tab so "Run Locations" can
        # pull the current in-memory plan. Never mutated from here.
        self.plan_day_tab = plan_day_tab
        self._trip_rows = []      # parallel to the per-trip table
        self._chain_data = []     # [{driver, legs: [...], jobs: [...]}]
        self._current_jobs = []   # the plan currently being displayed
        # These two are kept alive between runs on purpose -- see the long
        # note in _reset_controls() about the double-free crash. Ask
        # self._lookup_running whether a run is in flight; never poke these.
        self._thread = None
        self._worker = None
        self._lookup_running = False
        self._pending_state = None
        self._suppress_loc_save = False
        self._build_ui()
        self._backfill_coords_from_addresses()
        self.refresh_locations()

    # ----------------------------------------------------------- build UI

    def _build_ui(self):
        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.run_btn = QPushButton("Run Locations")
        self.run_btn.setToolTip(
            "Look up travel times for the current plan's trips and plot them.\n"
            "Nothing is fetched until you click this -- results are cached, so\n"
            "re-running mostly costs nothing."
        )
        self.run_btn.clicked.connect(self._on_run)
        toolbar.addWidget(self.run_btn)

        self.geocode_btn = QPushButton("Geocode Missing Locations")
        self.geocode_btn.setToolTip("Fill in coordinates for any predefined location that doesn't have them yet.")
        self.geocode_btn.clicked.connect(self._on_geocode_missing)
        toolbar.addWidget(self.geocode_btn)

        toolbar.addWidget(QLabel("Show:"))
        self.view_combo = QComboBox()
        self.view_combo.addItems(["All trips", "One driver at a time"])
        self.view_combo.setToolTip(
            "81 routes drawn at once is unreadable -- 'One driver at a time' is\n"
            "usually what you want when checking whether a day is actually drivable."
        )
        toolbar.addWidget(self.view_combo)

        toolbar.addStretch(1)
        self.provider_label = QLabel("")
        self.provider_label.setStyleSheet("color: #888888;")
        toolbar.addWidget(self.provider_label)
        clear_cache_btn = QPushButton("Clear Cache")
        clear_cache_btn.setToolTip("Drop stored travel times so the next run re-fetches them.")
        clear_cache_btn.clicked.connect(self._on_clear_cache)
        toolbar.addWidget(clear_cache_btn)
        root.addLayout(toolbar)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(6)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        splitter = QSplitter(Qt.Horizontal)

        splitter.addWidget(self._build_locations_panel())
        splitter.addWidget(self._build_map_panel())
        splitter.addWidget(self._build_trips_panel())
        # Map gets the lion's share; the editor is reference data.
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        splitter.setStretchFactor(2, 3)
        root.addWidget(splitter, 1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888888;")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self._refresh_cache_stats()

    def _build_locations_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.addWidget(QLabel("Predefined Locations"))
        note = QLabel(
            "Short codes from your Excel file mapped to a real address. Anything not listed "
            "is still looked up as typed -- just treated as a rougher, area-level estimate."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(note)

        self.loc_table = QTableWidget(0, 4)
        self.loc_table.setHorizontalHeaderLabels(["Code", "Address", "Lat", "Lon"])
        self.loc_table.horizontalHeader().setSectionResizeMode(LOC_ADDRESS, QHeaderView.Stretch)
        self.loc_table.setColumnWidth(LOC_CODE, 80)
        self.loc_table.setColumnWidth(LOC_LAT, 70)
        self.loc_table.setColumnWidth(LOC_LON, 70)
        # Lat/Lon are editable (the Code/Address columns stay read-only and
        # are edited through the boxes below). Paste the whole
        # "25.223732, 55.288312" string Google Maps gives you into EITHER
        # cell and it splits itself across both -- that's how the
        # coordinates actually arrive in practice.
        self.loc_table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        self.loc_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.loc_table.itemSelectionChanged.connect(self._on_location_selected)
        self.loc_table.itemChanged.connect(self._on_location_cell_edited)
        layout.addWidget(self.loc_table, 1)

        coord_hint = QLabel(
            "Tip: right-click a spot in Google Maps, copy the coordinates, and paste them into "
            "the Lat cell — it splits across Lat/Lon automatically. Exact, and costs no API calls."
        )
        coord_hint.setWordWrap(True)
        coord_hint.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(coord_hint)

        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Short code, e.g. CPK")
        layout.addWidget(self.code_input)
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("Real address, e.g. Central Production Kitchen, Al Quoz, Dubai")
        layout.addWidget(self.address_input)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add / Update")
        add_btn.clicked.connect(self._on_add_location)
        search_btn = QPushButton("Search Place...")
        search_btn.setToolTip(
            "Search for a place by name and pick the right match.\n"
            "Use this for anywhere you don't have coordinates for -- it shows\n"
            "the real candidates instead of guessing, and biases results to\n"
            "your operating area so you don't get a same-named place abroad."
        )
        search_btn.clicked.connect(self._on_search_place)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._on_delete_location)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(search_btn)
        btn_row.addWidget(del_btn)
        layout.addLayout(btn_row)
        return panel

    def _on_search_place(self):
        """Search for a place, then save the chosen match against the code
        typed in the Code box (or the selected row's code)."""
        provider, api_key = self._active_provider()
        if not provider:
            QMessageBox.information(
                self, "No maps key",
                "Add a Google Maps key in Settings -- or an OpenRouteService key in the same tab's "
                "\"Free/Testing Maps Provider\" section -- to search for places."
            )
            return

        code = self.code_input.text().strip()
        seed = self.address_input.text().strip()
        if not code:
            row = self.loc_table.currentRow()
            if row >= 0:
                code = self.loc_table.item(row, LOC_CODE).text()
                seed = seed or self.loc_table.item(row, LOC_ADDRESS).text()
        if not code:
            QMessageBox.information(
                self, "Which location?",
                "Type a short code in the Code box (or select a row) first -- the place you find "
                "will be saved against it."
            )
            return

        dialog = _PlaceSearchDialog(
            provider, api_key, self._operating_focus(), seed or code,
            country=OPERATING_COUNTRY, parent=self,
        )
        if dialog.exec() != QDialog.Accepted or not dialog.chosen:
            return
        chosen = dialog.chosen
        db.add_location(self.conn, code, chosen["label"])
        db.set_location_coords(self.conn, code, chosen["lat"], chosen["lon"])
        # Cache it under the ORIGINAL raw text too, so the same string
        # appearing in the Excel file resolves without another lookup.
        if seed:
            db.save_geocode(self.conn, seed, chosen["lat"], chosen["lon"])
        self.code_input.clear()
        self.address_input.clear()
        self.refresh_locations()
        self.status_label.setText(f"Saved {code}: {chosen['label']} ({chosen['lat']:.6f}, {chosen['lon']:.6f})")

    def _build_map_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Map"))
        # QWebEngineView is imported lazily and guarded: it ships with
        # PySide6 here, but if a future environment lacks it the rest of
        # this tab (locations editor, travel-time lists) must still work --
        # the map is the only part that genuinely needs it.
        self.map_view = None
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            from PySide6.QtWebEngineCore import QWebEnginePage

            class _QuietPage(QWebEnginePage):
                """Keeps the terminal readable. Qt prints every JS console
                message to stdout by default, and the OpenFreeMap Liberty
                style emits a handful of harmless "Expected value to be of
                type number, but found null instead." warnings on load --
                confirmed to come from the upstream style itself, NOT from
                this app's code (a control page loading the same style with
                all of our own JS removed produces them identically). Only
                that one known-benign message is filtered; everything else
                still prints, so a real map error is never hidden."""

                _MUTED = ("Expected value to be of type number, but found null instead.",)

                def javaScriptConsoleMessage(self, level, message, line, source):
                    if any(m in message for m in self._MUTED):
                        return
                    super().javaScriptConsoleMessage(level, message, line, source)

            self.map_view = QWebEngineView()
            self.map_view.setPage(_QuietPage(self.map_view))
            self.map_view.setHtml(_MAP_HTML)
            layout.addWidget(self.map_view, 1)
        except ImportError:
            fallback = QLabel(
                "Map unavailable: PySide6's QtWebEngine component isn't installed.\n"
                "Travel times still work -- only the map picture is missing."
            )
            fallback.setWordWrap(True)
            fallback.setAlignment(Qt.AlignCenter)
            fallback.setStyleSheet("color: #a08030;")
            layout.addWidget(fallback, 1)
        return panel

    def _build_trips_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 0, 0, 0)

        layout.addWidget(QLabel("Trips"))
        self.trip_table = QTableWidget(0, 6)
        self.trip_table.setHorizontalHeaderLabels(["SR", "Time", "From", "To", "Travel", "Dist"])
        self.trip_table.horizontalHeader().setSectionResizeMode(TRIP_FROM, QHeaderView.Stretch)
        self.trip_table.horizontalHeader().setSectionResizeMode(TRIP_TO, QHeaderView.Stretch)
        self.trip_table.setColumnWidth(TRIP_SR, 45)
        self.trip_table.setColumnWidth(TRIP_TIME, 95)
        self.trip_table.setColumnWidth(TRIP_TRAVEL, 70)
        self.trip_table.setColumnWidth(TRIP_DIST, 60)
        self.trip_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.trip_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.trip_table.itemSelectionChanged.connect(self._on_trip_selected)
        layout.addWidget(self.trip_table, 1)

        chain_header = QLabel("Driver Day Chains")
        chain_header.setToolTip(
            "Each driver's day in order. The gap between one job ending and the next\n"
            "starting is compared against the real drive time between those places."
        )
        layout.addWidget(chain_header)
        chain_note = QLabel("Red = drive time exceeds the gap (not physically possible). Amber = under 15 min spare.")
        chain_note.setWordWrap(True)
        chain_note.setStyleSheet("color: #888888; font-size: 11px;")
        layout.addWidget(chain_note)

        self.chain_tree = QTreeWidget()
        self.chain_tree.setHeaderLabels(["Driver / Connection", "Gap", "Drive", "Verdict"])
        self.chain_tree.setColumnWidth(0, 240)
        self.chain_tree.setColumnWidth(1, 70)
        self.chain_tree.setColumnWidth(2, 70)
        self.chain_tree.itemSelectionChanged.connect(self._on_chain_selected)
        layout.addWidget(self.chain_tree, 1)

        self.totals_label = QLabel("")
        self.totals_label.setStyleSheet("font-weight: 650;")
        layout.addWidget(self.totals_label)
        return panel

    # ------------------------------------------------------ locations CRUD

    def refresh_locations(self):
        # Populating fires itemChanged for every cell; suppress so it isn't
        # mistaken for the planner editing coordinates.
        self._suppress_loc_save = True
        self.loc_table.setRowCount(0)
        for loc in db.list_locations(self.conn):
            row = self.loc_table.rowCount()
            self.loc_table.insertRow(row)
            code_item = QTableWidgetItem(loc["short_code"])
            addr_item = QTableWidgetItem(loc["full_address"])
            # Code/Address are edited through the boxes below, not in-place.
            for item in (code_item, addr_item):
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.loc_table.setItem(row, LOC_CODE, code_item)
            self.loc_table.setItem(row, LOC_ADDRESS, addr_item)
            lat = loc["latitude"]
            lon = loc["longitude"]
            lat_item = QTableWidgetItem("--" if lat is None else f"{lat:.6f}")
            lon_item = QTableWidgetItem("--" if lon is None else f"{lon:.6f}")
            lat_item.setTextAlignment(Qt.AlignCenter)
            lon_item.setTextAlignment(Qt.AlignCenter)
            if lat is None or lon is None:
                lat_item.setForeground(TIGHT_COLOR)
                lon_item.setForeground(TIGHT_COLOR)
            self.loc_table.setItem(row, LOC_LAT, lat_item)
            self.loc_table.setItem(row, LOC_LON, lon_item)
        self._suppress_loc_save = False

    def _on_location_cell_edited(self, item):
        """Saves a hand-entered coordinate. Pasting the whole
        "25.223732, 55.288312" pair into either cell fills both -- that's
        the exact string Google Maps puts on the clipboard, so the natural
        paste just works instead of erroring."""
        if getattr(self, "_suppress_loc_save", False):
            return
        if item.column() not in (LOC_LAT, LOC_LON):
            return
        row = item.row()
        code_item = self.loc_table.item(row, LOC_CODE)
        if code_item is None:
            return
        code = code_item.text()

        pair = _parse_latlon(item.text())
        if pair:
            lat, lon = pair["lat"], pair["lon"]
        else:
            def _num(col):
                cell = self.loc_table.item(row, col)
                try:
                    return float(cell.text())
                except (AttributeError, TypeError, ValueError):
                    return None
            lat, lon = _num(LOC_LAT), _num(LOC_LON)
            if lat is None or lon is None:
                return   # half-entered -- wait for the other cell
            if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                QMessageBox.warning(
                    self, "Invalid coordinates",
                    "Latitude must be between -90 and 90, longitude between -180 and 180.\n\n"
                    "Tip: right-click the spot in Google Maps and paste what it copies.",
                )
                self.refresh_locations()
                return

        db.set_location_coords(self.conn, code, lat, lon)
        self.refresh_locations()
        self.status_label.setText(f"Coordinates saved for {code}: {lat:.6f}, {lon:.6f}")

    def _on_add_location(self):
        code = self.code_input.text().strip()
        address = self.address_input.text().strip()
        if not code or not address:
            QMessageBox.information(self, "Missing info", "Both short code and address are required.")
            return
        db.add_location(self.conn, code, address)
        # Pasting a coordinate pair as the "address" is a perfectly
        # reasonable thing to do (it's what Google Maps hands you), so
        # capture it as real coordinates straight away rather than leaving
        # the row looking un-located until something geocodes it.
        point = _parse_latlon(address)
        if point:
            db.set_location_coords(self.conn, code, point["lat"], point["lon"])
        self.code_input.clear()
        self.address_input.clear()
        self.refresh_locations()

    def _on_delete_location(self):
        row = self.loc_table.currentRow()
        if row < 0:
            return
        code = self.loc_table.item(row, LOC_CODE).text()
        if QMessageBox.question(self, "Delete Location", f"Delete the mapping for '{code}'?") != QMessageBox.Yes:
            return
        db.delete_location(self.conn, code)
        self.refresh_locations()

    def _on_location_selected(self):
        row = self.loc_table.currentRow()
        if row < 0:
            return
        lat_text = self.loc_table.item(row, LOC_LAT).text()
        lon_text = self.loc_table.item(row, LOC_LON).text()
        if lat_text == "--" or lon_text == "--":
            return
        code = self.loc_table.item(row, LOC_CODE).text()
        self._draw([{"lat": float(lat_text), "lon": float(lon_text), "label": code}], [], code)

    # -------------------------------------------------------- provider

    def _active_provider(self):
        """Google when configured (traffic-aware, better answers), else the
        free OpenRouteService fallback -- exactly the preference order the
        Settings tab describes. Returns (provider, api_key) or (None, None)."""
        google_key = db.get_setting(self.conn, GOOGLE_MAPS_KEY_SETTING)
        if google_key:
            return maps_client.PROVIDER_GOOGLE, google_key
        ors_key = db.get_setting(self.conn, ORS_TEST_KEY_SETTING)
        if ors_key:
            return maps_client.PROVIDER_ORS, ors_key
        return None, None

    def _refresh_cache_stats(self):
        travel, geo = db.travel_cache_stats(self.conn)
        self.status_label.setText(
            f"Cache: {travel} travel time(s), {geo} geocoded address(es) stored -- "
            f"cached lookups are reused instead of re-charged."
        )

    def _on_clear_cache(self):
        if QMessageBox.question(
            self, "Clear Cache",
            "Drop all stored travel times AND looked-up coordinates?\n\n"
            "The next run will re-fetch them (which may cost API calls). Use this if any "
            "looked-up location turned out to be plain wrong.\n\n"
            "Coordinates you entered or confirmed yourself are NOT affected."
        ) != QMessageBox.Yes:
            return
        travel = db.clear_travel_time_cache(self.conn)
        geo = db.clear_geocode_cache(self.conn)
        self._refresh_cache_stats()
        QMessageBox.information(
            self, "Cache cleared",
            f"Removed {travel} cached travel time(s) and {geo} looked-up coordinate(s).\n"
            f"Your own location coordinates are untouched."
        )

    # ------------------------------------------------------------- run

    def _operating_focus(self):
        """The fleet's real operating centre, averaged from the predefined
        locations that already have coordinates -- so searches bias toward
        where this fleet actually works with ZERO configuration, and
        self-correct if the business ever moves. Returns None until at
        least one location has coordinates."""
        rows = [
            r for r in db.list_locations(self.conn)
            if r["latitude"] is not None and r["longitude"] is not None
        ]
        if not rows:
            return None
        return {
            "lat": sum(r["latitude"] for r in rows) / len(rows),
            "lon": sum(r["longitude"] for r in rows) / len(rows),
        }

    def _resolve_point(self, raw_text):
        """Coordinates for a raw Excel location string, WITHOUT any network
        call -- predefined codes first (planner-set coordinates always win
        over anything a geocoder guessed), then a coordinate pair typed
        directly into the address field, then the geocode cache. Returns
        None only if it genuinely still needs fetching."""
        if not raw_text:
            return None
        row = self.conn.execute(
            "SELECT latitude, longitude, full_address FROM locations WHERE short_code = ?",
            (raw_text.strip(),),
        ).fetchone()
        if row and row["latitude"] is not None and row["longitude"] is not None:
            return {"lat": row["latitude"], "lon": row["longitude"]}
        # The address itself may BE a coordinate pair (pasted from Google
        # Maps) -- use it directly rather than paying to geocode a string
        # that already says exactly where the place is.
        if row:
            point = _parse_latlon(row["full_address"])
            if point:
                return point
        resolved = db.resolve_location(self.conn, raw_text)
        point = _parse_latlon(resolved["address"])
        if point:
            return point
        return db.get_geocode(self.conn, resolved["address"])

    def _backfill_coords_from_addresses(self):
        """Fills in latitude/longitude for any location whose address field
        already contains a coordinate pair.

        Exists because the Lat/Lon columns were read-only in the first
        version of this screen, so the only place to put coordinates was
        the address box -- and that's exactly what the project owner did
        for all their locations. Rather than make them re-enter everything,
        their existing data is simply understood. Runs on tab open; a
        no-op once every row has coordinates, and it never overwrites
        coordinates that are already set."""
        filled = 0
        for loc in db.list_locations(self.conn):
            if loc["latitude"] is not None and loc["longitude"] is not None:
                continue
            point = _parse_latlon(loc["full_address"])
            if point:
                db.set_location_coords(self.conn, loc["short_code"], point["lat"], point["lon"])
                filled += 1
        return filled

    def _on_geocode_missing(self):
        provider, api_key = self._active_provider()
        if not provider:
            QMessageBox.information(
                self, "No maps key",
                "Add a Google Maps key in Settings -- or an OpenRouteService key in the same tab's "
                "\"Free/Testing Maps Provider\" section -- first."
            )
            return
        missing = [
            loc["full_address"] for loc in db.list_locations(self.conn)
            if loc["latitude"] is None or loc["longitude"] is None
        ]
        if not missing:
            QMessageBox.information(self, "Nothing to do", "Every predefined location already has coordinates.")
            return
        self._start_worker(provider, api_key, missing, [], mode="geocode_locations")

    def _on_run(self):
        jobs = list(getattr(self.plan_day_tab, "jobs", []) or [])
        if not jobs:
            QMessageBox.information(
                self, "No plan loaded",
                "Upload a file and click Run Planning on the Plan a Day tab first -- this screen "
                "maps whatever plan is currently loaded there."
            )
            return
        provider, api_key = self._active_provider()
        if not provider:
            QMessageBox.information(
                self, "No maps key",
                "Add a Google Maps key in Settings -- or an OpenRouteService key in the same tab's "
                "\"Free/Testing Maps Provider\" section -- first."
            )
            return

        needed_geocodes, needed_routes = self._plan_lookups(jobs, provider)
        if not needed_geocodes and not needed_routes:
            # Everything already cached -- no network at all. This is the
            # normal case after the first run and the whole point of the cache.
            self._rebuild_views(jobs, provider)
            self.status_label.setText("All lookups served from cache -- no API calls made.")
            return
        self._pending_jobs = jobs
        self._start_worker(provider, api_key, needed_geocodes, needed_routes, mode="plan")

    def _plan_lookups(self, jobs, provider):
        """Works out what genuinely still needs fetching -- everything else
        comes from cache. Returns (addresses_to_geocode, routes_to_fetch)."""
        needed_geocodes, seen_geo = [], set()
        needed_routes, seen_routes = [], set()

        def want_geocode(raw):
            if not raw:
                return
            address = db.resolve_location(self.conn, raw)["address"]
            if not address or address in seen_geo:
                return
            if self._resolve_point(raw) is None:
                seen_geo.add(address)
                needed_geocodes.append(address)

        def want_route(origin_raw, destination_raw, departure_dt):
            if not origin_raw or not destination_raw or departure_dt is None:
                return
            origin = db.resolve_location(self.conn, origin_raw)["address"]
            destination = db.resolve_location(self.conn, destination_raw)["address"]
            if not origin or not destination or origin == destination:
                return
            hour = departure_dt.hour
            key = (origin, destination, hour)
            if key in seen_routes:
                return
            if db.get_cached_travel_time(self.conn, origin, destination, hour) is not None:
                return
            seen_routes.add(key)
            needed_routes.append({
                "origin": origin, "destination": destination, "hour": hour,
                "departure_dt": departure_dt,
                "origin_coords": self._resolve_point(origin_raw),
                "destination_coords": self._resolve_point(destination_raw),
            })

        for job in jobs:
            want_geocode(job.pickup_location)
            want_geocode(job.order_location)
            want_route(job.pickup_location, job.order_location, job.start_dt)
        for _driver, legs in self._driver_legs(jobs):
            for prev_job, next_job in legs:
                want_geocode(prev_job.order_location or prev_job.pickup_location)
                want_geocode(next_job.pickup_location or next_job.order_location)
                want_route(
                    prev_job.order_location or prev_job.pickup_location,
                    next_job.pickup_location or next_job.order_location,
                    prev_job.end_dt,
                )
        return needed_geocodes, needed_routes

    def _driver_legs(self, jobs):
        """[(driver_label, [(prev_job, next_job), ...]), ...] -- each
        driver's day in time order, paired into consecutive connections."""
        by_driver = {}
        for job in jobs:
            if job.start_dt is None or job.end_dt is None:
                continue
            label = _job_label(job)
            if label == "(unassigned)":
                continue
            by_driver.setdefault(label, []).append(job)
        out = []
        for label, driver_jobs in sorted(by_driver.items()):
            driver_jobs.sort(key=lambda j: j.start_dt)
            out.append((label, list(zip(driver_jobs, driver_jobs[1:]))))
        return out

    def _start_worker(self, provider, api_key, geocodes, routes, mode):
        # Refuse to start a second run on top of a live one. The buttons are
        # disabled while running so this shouldn't normally be reachable,
        # but if it ever were, reassigning self._worker below would drop the
        # running worker's last Python reference mid-flight -- the same
        # double-free that crashed the app (see _reset_controls). Cheap
        # insurance against a whole class of hard crash.
        if self._lookup_running:
            return
        self._lookup_running = True
        self.run_btn.setEnabled(False)
        self.geocode_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.status_label.setText(
            f"Looking up {len(geocodes)} address(es) and {len(routes)} route(s) via "
            f"{'Google' if provider == maps_client.PROVIDER_GOOGLE else 'OpenRouteService'}..."
        )
        self._pending_state = {"provider": provider, "mode": mode}

        self._thread = QThread(self)
        self._worker = _LookupWorker(
            provider, api_key, geocodes, routes,
            focus_point=self._operating_focus(), max_km=MAX_GEOCODE_DISTANCE_KM,
            country=OPERATING_COUNTRY,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_lookups_finished)
        self._worker.failed.connect(self._on_lookups_failed)
        self._worker.progress.connect(self._on_lookup_progress)
        for signal in (self._worker.finished, self._worker.failed):
            signal.connect(self._thread.quit)
            signal.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_lookup_progress(self, done, total):
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _reset_controls(self):
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)
        self.run_btn.setEnabled(True)
        self.geocode_btn.setEnabled(True)
        self._lookup_running = False
        # DO NOT set self._worker = None here. _LookupWorker has no Qt
        # parent (moveToThread() requires a parentless object), so PYTHON
        # owns its lifetime -- and `finished` is also connected to
        # worker.deleteLater. Dropping the last Python reference here made
        # Python destroy the C++ object while Qt still had a deleteLater
        # queued for it: a double free, which hard-crashed the whole app
        # with no traceback (reported 2026-08-16, right after clicking Run
        # Locations). The references are instead kept until the next
        # _start_worker() replaces them -- exactly what plan_day_tab.py's
        # _SolverWorker/_AIReviewWorker have always done, which is why
        # those never had this problem. Use self._lookup_running above to
        # ask "is a run in flight?", never the object references.

    def _on_lookups_failed(self, detail):
        self._pending_state = None
        self._reset_controls()
        QMessageBox.warning(self, "Lookup failed", detail)

    def _on_lookups_finished(self, results):
        state = self._pending_state or {}
        self._pending_state = None
        self._reset_controls()

        # Cache writes happen HERE, on the main thread -- the worker never
        # touches self.conn (sqlite3 connections aren't thread-safe).
        errors = 0
        for item in results:
            if item.get("error"):
                errors += 1
                continue
            if item["kind"] == "geocode":
                db.save_geocode(self.conn, item["address"], item["point"]["lat"], item["point"]["lon"])
                # If this address belongs to a predefined code, store the
                # coordinates on the location row too, so the planner can
                # see and correct them.
                row = self.conn.execute(
                    "SELECT short_code FROM locations WHERE full_address = ? AND latitude IS NULL",
                    (item["address"],),
                ).fetchone()
                if row:
                    db.set_location_coords(self.conn, row["short_code"],
                                            item["point"]["lat"], item["point"]["lon"])
            else:
                route, data = item["route"], item["data"]
                db.save_travel_time(
                    self.conn, route["origin"], route["destination"], route["hour"],
                    data.get("duration_minutes"), data.get("distance_km"), data.get("polyline"),
                )

        self.refresh_locations()
        self._refresh_cache_stats()
        provider = state.get("provider")
        if state.get("mode") == "plan":
            self._rebuild_views(getattr(self, "_pending_jobs", []), provider)
        if errors:
            self.status_label.setText(
                self.status_label.text() + f"  ({errors} lookup(s) failed -- those rows show '--'.)"
            )

    # --------------------------------------------------------- rendering

    def _cached_route(self, origin_raw, destination_raw, departure_dt):
        if not origin_raw or not destination_raw or departure_dt is None:
            return None
        origin = db.resolve_location(self.conn, origin_raw)["address"]
        destination = db.resolve_location(self.conn, destination_raw)["address"]
        if not origin or not destination:
            return None
        if origin == destination:
            # Finishing and starting in the same place needs no drive. Report
            # a real zero rather than "--": "--" means "we don't know", and
            # showing that here would wrongly make a perfectly fine
            # back-to-back connection look unverified. Costs no API call
            # either -- _plan_lookups skips same-place pairs for that reason.
            return {"duration_minutes": 0.0, "distance_km": 0.0, "polyline": None}
        return db.get_cached_travel_time(self.conn, origin, destination, departure_dt.hour)

    def _rebuild_views(self, jobs, provider):
        self._current_jobs = list(jobs)
        self._render_trips(jobs)
        self._render_chains(jobs)
        name = "Google (traffic-aware)" if provider == maps_client.PROVIDER_GOOGLE else "OpenRouteService (no traffic data)"
        self.provider_label.setText(f"Source: {name}")
        self.provider_label.setToolTip(
            "Google factors in traffic at each trip's actual departure time.\n"
            "OpenRouteService uses average road speeds, so tight connections\n"
            "look more optimistic than they really are."
            if provider == maps_client.PROVIDER_ORS else
            "Travel times include predicted traffic at each trip's departure time."
        )

    def _render_trips(self, jobs):
        self.trip_table.setRowCount(0)
        self._trip_rows = []
        total_minutes = 0.0
        total_km = 0.0
        for job in jobs:
            cached = self._cached_route(job.pickup_location, job.order_location, job.start_dt)
            row = self.trip_table.rowCount()
            self.trip_table.insertRow(row)
            time_str = ""
            if job.start_dt and job.end_dt:
                time_str = f"{job.start_dt.strftime('%H:%M')}-{job.end_dt.strftime('%H:%M')}"
            values = {
                TRIP_SR: str(job.sr or ""),
                TRIP_TIME: time_str,
                TRIP_FROM: job.pickup_location or "",
                TRIP_TO: job.order_location or "",
                TRIP_TRAVEL: _fmt_minutes(cached["duration_minutes"]) if cached else "--",
                TRIP_DIST: f"{cached['distance_km']:.1f} km" if cached and cached.get("distance_km") is not None else "--",
            }
            for col, text in values.items():
                item = QTableWidgetItem(text)
                if col in (TRIP_SR, TRIP_TIME, TRIP_TRAVEL, TRIP_DIST):
                    item.setTextAlignment(Qt.AlignCenter)
                self.trip_table.setItem(row, col, item)
            if cached:
                total_minutes += cached.get("duration_minutes") or 0
                total_km += cached.get("distance_km") or 0
            self._trip_rows.append({"job": job, "cached": cached})
        self.totals_label.setText(
            f"{len(jobs)} trips  |  total travel {_fmt_minutes(total_minutes)}  |  {total_km:.0f} km"
        )

    def _render_chains(self, jobs):
        self.chain_tree.clear()
        self._chain_data = []
        for driver, legs in self._driver_legs(jobs):
            parent = QTreeWidgetItem([driver, "", "", ""])
            worst = OK_COLOR
            leg_records = []
            for prev_job, next_job in legs:
                gap_minutes = (next_job.start_dt - prev_job.end_dt).total_seconds() / 60.0
                cached = self._cached_route(
                    prev_job.order_location or prev_job.pickup_location,
                    next_job.pickup_location or next_job.order_location,
                    prev_job.end_dt,
                )
                drive = cached["duration_minutes"] if cached else None
                if drive is None:
                    verdict, color = "--", None
                elif drive > gap_minutes:
                    verdict, color = "IMPOSSIBLE", INFEASIBLE_COLOR
                    worst = INFEASIBLE_COLOR
                elif drive > gap_minutes - TIGHT_MARGIN_MINUTES:
                    verdict, color = "tight", TIGHT_COLOR
                    if worst is not INFEASIBLE_COLOR:
                        worst = TIGHT_COLOR
                else:
                    verdict, color = "ok", OK_COLOR
                child = QTreeWidgetItem([
                    f"SR{prev_job.sr} -> SR{next_job.sr}",
                    _fmt_minutes(gap_minutes),
                    _fmt_minutes(drive),
                    verdict,
                ])
                if color is not None:
                    for col in range(4):
                        child.setForeground(col, color)
                parent.addChild(child)
                leg_records.append({"prev": prev_job, "next": next_job, "cached": cached})
            for col in range(4):
                parent.setForeground(col, worst)
            parent.setText(1, f"{len(legs)} leg(s)")
            self.chain_tree.addTopLevelItem(parent)
            # `jobs` (the driver's own trips, in time order) is kept
            # alongside `legs` (the gaps BETWEEN trips) because selecting a
            # driver should draw their whole day -- the trips they actually
            # drive AND the repositioning between them. Drawing only the
            # legs left a driver whose connections were all same-place with
            # no lines at all, just disconnected dots.
            self._chain_data.append({
                "driver": driver, "legs": leg_records, "item": parent,
                "jobs": self._driver_jobs_in_order(driver),
            })
        self.chain_tree.expandAll()

    def _driver_jobs_in_order(self, driver_label):
        jobs = [
            j for j in self._current_jobs
            if _job_label(j) == driver_label and j.start_dt is not None and j.end_dt is not None
        ]
        jobs.sort(key=lambda j: j.start_dt)
        return jobs

    # ---------------------------------------------------------- map draw

    def _draw(self, points, lines, hint=""):
        if self.map_view is None:
            return
        self.map_view.page().runJavaScript(
            f"draw({json.dumps(points)}, {json.dumps(lines)}, {json.dumps(hint)});"
        )

    def _points_for(self, job, which):
        raw = job.pickup_location if which == "pickup" else job.order_location
        point = self._resolve_point(raw)
        if not point:
            return None
        return {"lat": point["lat"], "lon": point["lon"], "label": f"SR{job.sr}: {raw}"}

    def _on_trip_selected(self):
        row = self.trip_table.currentRow()
        if row < 0 or row >= len(self._trip_rows):
            return
        record = self._trip_rows[row]
        job, cached = record["job"], record["cached"]
        points = [p for p in (self._points_for(job, "pickup"), self._points_for(job, "order")) if p]
        if points:
            points[0]["color"] = "#39a96b"          # start
            points[-1]["color"] = "#c0392b"         # end
        lines = []
        if cached and cached.get("polyline"):
            coords = maps_client.decode_polyline(cached["polyline"])
            if coords:
                lines.append({"points": [[lat, lon] for lat, lon in coords], "color": "#3f7ee8"})
        hint = f"SR{job.sr}: {job.pickup_location} -> {job.order_location}"
        if cached:
            hint += f"  ({_fmt_minutes(cached['duration_minutes'])})"
        self._draw(points, lines, hint)

    def _route_line(self, origin_raw, destination_raw, departure_dt, color, dashed=False):
        """One drawable line between two places. Prefers the real
        road-following polyline; falls back to a straight line between the
        two points when no polyline is available, so the day still reads as
        a connected path instead of disconnected dots. The fallback is
        drawn dashed so it's never mistaken for a real routed path."""
        cached = self._cached_route(origin_raw, destination_raw, departure_dt)
        if cached and cached.get("polyline"):
            coords = maps_client.decode_polyline(cached["polyline"])
            if coords:
                return {"points": [[lat, lon] for lat, lon in coords], "color": color, "dashed": dashed}
        origin = self._resolve_point(origin_raw)
        destination = self._resolve_point(destination_raw)
        if origin and destination:
            if (abs(origin["lat"] - destination["lat"]) < 1e-9
                    and abs(origin["lon"] - destination["lon"]) < 1e-9):
                return None      # same spot -- nothing to draw
            return {
                "points": [[origin["lat"], origin["lon"]], [destination["lat"], destination["lon"]]],
                "color": color, "dashed": True,
            }
        return None

    def _on_chain_selected(self):
        item = self.chain_tree.currentItem()
        if item is None:
            return
        parent = item.parent()
        if parent is None:
            entry = next((c for c in self._chain_data if c["item"] is item), None)
            if entry:
                self._draw_driver_day(entry)
            return
        # A single connection was clicked -- show just that leg.
        entry = next((c for c in self._chain_data if c["item"] is parent), None)
        if not entry:
            return
        index = parent.indexOfChild(item)
        if not (0 <= index < len(entry["legs"])):
            return
        leg = entry["legs"][index]
        points = []
        for job, which, color in ((leg["prev"], "order", "#39a96b"), (leg["next"], "pickup", "#c0392b")):
            point = self._points_for(job, which)
            if point:
                point["color"] = color
                points.append(point)
        line = self._route_line(
            leg["prev"].order_location or leg["prev"].pickup_location,
            leg["next"].pickup_location or leg["next"].order_location,
            leg["prev"].end_dt, "#e8b93a",
        )
        self._draw(points, [line] if line else [], f"{entry['driver']}: {item.text(0)}")

    def _draw_driver_day(self, entry):
        """The driver's WHOLE day as a sequential itinerary -- the view the
        reference voyage screen is built around.

        Draws both kinds of movement, distinctly:
          * each TRIP they actually run (pickup -> destination), in blue;
          * each REPOSITIONING leg between trips (deadhead), in amber.
        Previously only the repositioning legs were drawn, so a driver's
        actual work was invisible -- and when their connections were all
        same-place (finish at CPK, next job starts at CPK) there was
        nothing to draw at all, which is why the map showed only dots.

        Stops are numbered in running order so the day reads start to end.
        """
        jobs = entry.get("jobs") or []
        points, lines = [], []
        seq = 0
        for index, job in enumerate(jobs):
            pickup = self._points_for(job, "pickup")
            drop = self._points_for(job, "order")
            if pickup:
                seq += 1
                pickup["color"] = "#39a96b"          # green = a job starts here
                pickup["seq"] = seq
                pickup["label"] = f"{seq}. SR{job.sr} start — {job.pickup_location}"
                points.append(pickup)
            if drop:
                seq += 1
                drop["color"] = "#c0392b"            # red = a job ends here
                drop["seq"] = seq
                drop["label"] = f"{seq}. SR{job.sr} end — {job.order_location}"
                points.append(drop)
            line = self._route_line(
                job.pickup_location, job.order_location, job.start_dt, "#3f7ee8"
            )
            if line:
                lines.append(line)
            # ...then the repositioning to the next job, if any.
            if index + 1 < len(jobs):
                nxt = jobs[index + 1]
                hop = self._route_line(
                    job.order_location or job.pickup_location,
                    nxt.pickup_location or nxt.order_location,
                    job.end_dt, "#e8b93a", dashed=True,
                )
                if hop:
                    lines.append(hop)

        hint = f"{entry['driver']}: {len(jobs)} trip(s) — blue = trips, amber = repositioning"
        if not lines and points:
            hint += "  (no route lines yet — click Run Locations)"
        self._draw(points, lines, hint)
