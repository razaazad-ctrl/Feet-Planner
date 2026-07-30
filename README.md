# Fleet Planner — Phase 1 (Master Data)

This is the first building block of the fleet planning app: a standalone
desktop tool (Windows) for managing the master data everything else will
depend on — Drivers, Suppliers, and Vehicles.

Nothing here touches the daily Excel upload or the AI allocation yet —
that's Phase 2. This phase is just the "set it up once, edit only when it
changes" data layer and its screens.

## What's included

- **Drivers tab** — add/delete drivers; each driver has free-form rule
  lines (one rule per line). Lines like `Shift start: 07:00 AM`,
  `Max duty hours: 8`, `Qualified for: 5 Ton Chiller Truck`, `Off day: Friday`
  are automatically recognized and will be enforced as hard rules by the
  allocation engine later. Anything else you type is still saved and shown
  — it just gets passed to the AI as context instead of being enforced.
- **Suppliers tab** — same pattern: add/delete suppliers, then rule lines
  like `Rate: 350 AED per truck per day`, `Unit: PINK PEPPER CHILLER TRUCK #1
  (5 Ton Chiller Truck)`, `Max hours per unit: 12`.
- **Vehicles tab** — in-house fleet roster: plate, type, notes, and a
  workshop in/out toggle.
- All data is stored locally in a single SQLite file, `fleetplanner.db`,
  created next to the app on first run. Nothing is sent anywhere except
  when the AI allocation step (Phase 2) explicitly calls Claude.

The off-day/comp-day tracking tables also already exist in the database
(`off_day_log`, `comp_days`) so nothing needs to be redesigned when we
wire up the daily planning screen — there's just no UI for them yet.

## Running it (on Windows)

1. Install Python 3.11+ from python.org (check "Add to PATH" during install).
2. Open Command Prompt in this folder and run:
   ```
   pip install -r requirements.txt
   python -m app.main
   ```
3. The app window should open with three tabs: Drivers, Suppliers, Vehicles.

## Packaging into a standalone .exe (later, once you're happy with it)

From this same folder, on a Windows machine:
```
pip install pyinstaller
pyinstaller --noconsole --onefile --name FleetPlanner app/main.py
```
The finished `FleetPlanner.exe` will be in the `dist` folder. It can be
copied anywhere and run without Python installed. The `fleetplanner.db`
file will be created next to wherever the .exe is run from — so keep the
.exe in a fixed folder (not somewhere that changes) so your data stays
put between runs.

## Project structure

```
app/
  main.py                    entry point
  db.py                      SQLite schema + all CRUD functions
  rules_parser.py            recognizes common rule-line patterns
  ui/
    main_window.py           tab container
    entity_rules_widget.py   shared Drivers/Suppliers screen
    vehicles_tab.py          Vehicles screen
```

## What's next (Phase 2)

- Daily Excel upload screen — DONE
- The allocation engine: in-house drivers/vehicles first (fair
  distribution, hour limits, vehicle-type matching), then supplier
  overflow — DONE
- Claude integration for event-chain reasoning and day-notes — DONE
  (Settings tab + AI Review button on the Plan a Day tab)
- Google Maps integration for real, traffic-aware travel times — DONE
- Accept/reject UI for individual AI suggestions — DONE
- Decision-history memory (preferences digest) — DONE
- Export to Excel and print-ready PDF in your existing layout — TODO
- Off-day / comp-day screen (using the tables that already exist) — TODO
- The optional "restrict to this shortlist of drivers/suppliers" toggle
  for a given day — TODO
- Vehicle maintenance/inspection log (planner's idea from earlier) — TODO,
  intentionally deferred until the core planning flow is done
- Bug fix: real Excel export has separate START DATE + TIME columns
  (not one combined cell) — import now handles this correctly, verified
  against a real file
- Vehicle plate is now shown in the results table (a driver can and does
  get different vehicles for different jobs across the day -- this was
  always supported by the engine, just wasn't displayed)
- Predefined Locations tab — map short codes (CPK, BQT STORE, etc.) to
  real addresses for precise Maps lookups; anything else is used as-is
  but flagged as an approximate/area-level estimate for the AI to weigh
  accordingly
- "Don't use tomorrow" exclusion toggle for Drivers, Suppliers, and
  Vehicles — excluded entries turn orange and sort to the bottom as a
  visual reminder; the allocation engine skips them entirely
- Settings PIN (optional, not real security -- just friction against
  accidental changes)
- Schema migrations are now additive where possible, so small future
  changes won't require deleting the database file

## Decision-history memory (new)

Every AI suggestion you Accept or Reject on the Plan a Day screen gets
logged permanently in the `decision_log` table -- full detail, forever.
That table is NEVER sent to Claude directly, on purpose: sending a
growing log to the API every day would make cost and speed creep up
over months/years.

Instead, go to Settings and click "Refresh Preferences Digest Now"
periodically (monthly is reasonable). This reads only the decisions
logged since the last refresh, asks Claude to fold them into the
existing digest, and saves a short, roughly fixed-size summary (capped
around 400 words) of your demonstrated real-world patterns. Every daily
AI Review call only ever reads that one small digest -- never the raw
log -- so cost and speed stay flat no matter how much history has
accumulated.

## Settings tab (new)

Paste in your Anthropic API key and Google Maps API key here. Both are
stored locally in `fleetplanner.db` only. Each has a "Test" button that
makes one real, tiny request to confirm the key actually works, so you
find out immediately rather than in the middle of a full day's plan.

## AI Review (new)

On the Plan a Day tab, after "Run Planning" you can click
"AI Review (event chains + day notes)". This:
1. Finds every event with more than one stage (same event ID across rows)
2. Looks up real, traffic-aware travel time between consecutive stages
   using Google Maps, departing at the actual job end time (so a 6pm
   gap and a 2am gap get realistically different travel estimates)
3. Sends all of that, plus each driver's occupied hours so far and your
   day notes, to Claude
4. Shows back plain-language suggestions (e.g. "keep this driver on-site
   between these two jobs instead of cycling him back")

It never changes the plan by itself -- suggestions are for you to read
and act on manually for now. Turning each suggestion into a one-click
accept/reject is the natural next step once you've seen how the
suggestions read in practice.

## Major rework this session (bug fixes + big feature rebuild)

**Bug fixes (confirmed against a real uploaded file):**
- Date/time parsing now handles separate START DATE + TIME columns
  correctly (was silently failing on every row before)
- Vehicle plate now shown in the results table
- A driver's hour cap is now read from a STRUCTURED field, not a
  free-text line that could silently fail to match -- this is what fixed
  the bug where a driver was scheduled well past their stated hours

**Drivers now have structured hard-rule fields** (Drivers tab): working
hours/day, shift start, off day(s), max overtime hours/month (blank =
unlimited overtime), total hours/month target, license types. Free-text
notes are still there below, for AI context only.

**Suppliers now have structured offerings** (Suppliers tab): vehicle
type + rate/hour + max available/day, one row per type a supplier
offers. The app dynamically names/numbers hires at planning time:
- 1st hire of the day: "SUPPLIER NAME"
- 2nd hire of the day: "SUPPLIER NAME 1"
- reusing an existing hire later in the day: "SAME <label>"
Priority is always to reuse an already-hired unit before hiring a new
one (minimize headcount), only hiring a new unit when timing conflicts
or daily availability limits force it.

**Finalized-day history** (`finalized_jobs` table): click "Finalize Day"
on the Plan a Day tab to save that day's result permanently. This
history is what monthly overtime enforcement and cross-day supplier
fairness are computed from.

**Export**: "Export Filled Excel" produces the exact file you uploaded,
unchanged, with only Vehicle and Driver columns filled in -- verified
against a real file that formatting and other columns are untouched.

**Results table**: sortable by clicking any column header; filterable
by driver/supplier via a dropdown (groups "SAME X" with "X").

**Settings PIN**: now genuinely disables the tab itself while locked
(with a corner "Unlock Settings" button), rather than switching-then-
reverting, which could flash contents briefly.

**Still to do**: PDF export (likely via Excel COM automation/pywin32
since you're on Windows), driver shift rotation derived from history,
vehicle maintenance log, and the daily driver/supplier shortlist toggle.
