# NEXT_SESSION.md — Instructions for the next AI assistant

You are picking up an existing, working project. The person you're
talking to is **not a developer** — they're a fleet planner who has been
directing feature/bug decisions in plain language and relying on the
assistant to handle all implementation, testing, and technical tradeoffs.
Keep explanations in that same plain, concrete style; don't assume they
know programming terms.

## 1. Read these files, in this order, before touching anything

1. `AI_CONTEXT.md` — the full picture: what this is, why it's built the
   way it is, the business rules, the design decisions and their
   reasoning.
2. `DATABASE.md` — exact schema, what each column means, which tables
   are live vs. unused/reserved.
3. `ARCHITECTURE.md` — file-by-file responsibilities, data flow, the
   `allocate()` algorithm step by step.
4. `CHANGELOG_AI.md` — history, in case the person references "the
   supplier thing we fixed before" or similar — you'll need the context
   of *why* something is built the way it is, not just that it is.
5. This file, last, for how to actually work going forward.

**Do not start writing code before reading `AI_CONTEXT.md` in full.**
Several past mistakes in this project happened specifically because a
change was made without full context of an adjacent decision (see
Section 3 below).

## 2. How to understand this project quickly (if you only have 5 minutes)

- It's a **Windows desktop app** (Python + PySide6 + SQLite), used by
  **one planner**, for **one purpose**: fill in the Driver and Vehicle
  columns of a daily transport-request Excel file.
- There's a **strict three-stage pipeline**: deterministic rules engine
  (always runs first, no AI) → optional AI review (separate button,
  costs money, suggestions only) → export (writes back into the
  *original* file, untouched except two columns).
- **Hard rules live in database columns, never in free text.** If you
  see a free-text rule-line list anywhere in the UI, it is AI-context
  only — it is never enforced as a constraint. This was learned the hard
  way (twice) and is now a firm project convention.
- The single most valuable thing you can do when testing a change: get
  the person's actual data (they've previously pushed a real database +
  real Excel files to a public GitHub repo,
  `razaazad-ctrl/Feet-Planner`, specifically for this purpose) and run
  the real engine against it, rather than reasoning about it abstractly
  or only using synthetic test data. Ask if a similarly public repo/data
  dump exists for the current session if you need to validate something
  data-dependent.

## 3. Common mistakes to avoid (each of these actually happened)

- **Adding a new structured field to the database and the UI form, but
  forgetting to add it to the corresponding dataclass and actually check
  it inside `allocate()`.** This happened with `shift_start` — it was in
  the schema, the UI read/wrote it correctly, but `DriverProfile` didn't
  have the field and `build_driver_profiles` never read it, so the
  enforcement function existed but had nothing to check. **Whenever you
  add or modify a hard rule, verify all three of: (1) database column,
  (2) dataclass field + populated in `build_*_profiles`, (3) actually
  read inside the `allocate()` candidate-filtering loop.** Write a
  direct test that would fail if step 3 were missing, not just a test
  that the field round-trips through the database.
- **Assuming free-text rule-line matching (`rules_parser.py`) can be
  used for anything that needs to be reliably enforced.** It can't — see
  `AI_CONTEXT.md` Section 6. If a new hard rule is needed, it needs a new
  structured database column and form field, following the pattern of
  `working_hours_per_day`/`shift_start`/etc. in `drivers_tab.py`.
- **Changing the database schema (renaming/removing/retyping a column)
  without considering that this breaks the additive migration system.**
  The migration system (`db._MIGRATIONS`) only supports *adding* new
  columns safely. Anything else requires either a fresh database (data
  loss, needs explicit warning to the user) or a real migration script
  you write yourself — don't assume `ALTER TABLE ADD COLUMN` covers it.
- **Introducing fuzzy/approximate matching for vehicle types "to fix"
  text-mismatch issues.** This was deliberately rejected — fuzzy
  matching risks silently conflating genuinely different vehicle
  configurations, which is worse than a visible zero-match. The correct
  fix for a text mismatch is telling the user to make the text
  consistent at the source (copy-paste from the Excel column), not
  loosening the matching logic. If this tradeoff is ever revisited, it
  needs an explicit conversation with the user first, not a unilateral
  code change.
- **Modifying `main_window.py`'s PIN-lock mechanism to switch-then-check
  instead of disable-then-unlock.** The switch-then-revert pattern was
  already tried and rejected because it could flash the Settings tab's
  contents before the check completed. The current pattern
  (`setTabEnabled(False)` while locked, corner "Unlock Settings" button)
  is the fix — don't regress to the earlier pattern.
- **Bundling "Run Planning" and "AI Review" into one automatic step**
  without an explicit go-ahead from the user. This was deliberately kept
  as two separate steps while the deterministic engine is still being
  hardened, specifically so bugs are traceable to one layer or the
  other. The user has agreed a combined shortcut can be added *later*,
  once they're confident in the engine — check whether that conversation
  has happened since this document was written before changing this.
- **Assuming `entity_rules_widget.py` is in active use.** It is legacy,
  superseded by `drivers_tab.py`/`suppliers_tab.py`. Check
  `main_window.py`'s actual imports before touching it or assuming
  changes to it will have any effect.
- **Assuming `off_day_log`/`comp_days` are working features because the
  tables exist.** They are reserved schema with zero CRUD or UI wired to
  them. If asked to build off-day/comp-day tracking, you're building it
  from scratch, not extending something that partially works.

## 4. Areas of the code that require extra care

- **`allocation_engine.py`'s `allocate()` function.** This is the core
  of the whole product's value. It is deterministic and must stay that
  way — no AI, no network calls, fully repeatable given the same inputs.
  Any change here should be validated with a direct test (synthetic
  data first, then against real data if available) before considering
  it done, following the pattern established throughout this project
  (see `CHANGELOG_AI.md` for many examples of exactly this testing
  discipline).
- **Exact-string vehicle-type matching (`_type_matches`,
  `_driver_qualifies_for_type`).** This is intentionally strict. Any
  real-world "why isn't this working" report should be diagnosed first
  by checking for text mismatches (case, whitespace, wording variants
  like "Seated"/"Seater") before assuming there's a code bug.
- **`excel_import.py`'s header/column detection (`_HEADER_MAP`).** This
  is the single point of fragility for reading any new Excel export
  variant. An unrecognized header column silently contributes nothing
  (no error raised) — if a future file format changes column names,
  this will fail silently, not loudly. Consider whether this deserves a
  validation warning if picking this up.
- **`export.py`'s original-file preservation.** The whole point of this
  module is that it changes *nothing* except two specific cells. Any
  future change here needs to be re-verified against a real file with
  the same test discipline used originally (confirm an untouched column
  is byte-identical before/after, confirm formatting/styles survive).
- **Monthly overtime calculation (`db.get_driver_month_overtime_hours`).**
  This groups by day and sums *excess over baseline per day*, not a
  simple total — a subtle distinction that's easy to accidentally
  simplify incorrectly if refactored. Re-read the docstring and the
  `DATABASE.md` explanation before touching this.
- **Supplier hiring/naming logic in `allocate()`'s supplier pass.** The
  exact naming convention (`SUPPLIER`, `SUPPLIER 1`, `SAME <label>`) was
  explicitly confirmed by the user, not inferred — don't "improve" it
  based on what a historical reference file shows, since that file is
  known to be an inconsistent manual artifact (see `CHANGELOG_AI.md`
  Phase 6).

## 5. Current known issues (honest, as of end of last session)

- ~~`shift_start` documentation/code mismatch~~ **RESOLVED this session.**
  The project owner confirmed directly that `shift_start` was NOT being
  enforced in their live local copy either (not just this GitHub
  snapshot) — so this was a real, live bug, not a stale-snapshot
  artifact. Fixed by adding the missing `DriverProfile.shift_start`
  field, populating it in `build_driver_profiles`, and enforcing it via
  `_job_is_before_shift_start()` in the candidate loop. While fixing
  this, the project owner also asked for `license_types` and
  `working_hours_per_day` to be audited for the same failure pattern:
  `license_types` was confirmed correct (tested, no bug); a real bug WAS
  found in `working_hours_per_day` — see the item below.
- ~~`working_hours_per_day` not enforced when overtime cap is blank~~
  **RESOLVED this session.** `max_overtime_hours_per_month = None` was
  silently skipping the entire hours-check block, rather than being
  treated as "no overtime allowed" — a driver with a daily limit
  configured but no monthly overtime cap could get unlimited hours in
  one day. Confirmed with the project owner and fixed: blank overtime
  cap is now treated the same as an explicit `0`. See `AI_CONTEXT.md`
  Section 9 (bug #9) for the full writeup and the test that proved it.

- **`total_hours_per_month_target` is stored and shown in the UI but
  never enforced anywhere.** It's informational only right now — no
  logic reads it. If asked to make it a real constraint, this is new
  work, not a bug fix.
- **AI suggestion Accept does not automatically update the results
  table.** Clicking Accept on an AI suggestion logs the decision
  (`decision_log`) but the planner still has to manually go find and
  edit the corresponding row if they want the table to reflect it. This
  was an explicitly acknowledged interim state, not a hidden bug — the
  natural next step (discussed but not built) is wiring Accept to
  actually mutate the relevant `Job` and re-render the table.
- **The "restrict planning to a shortlist of drivers/suppliers for
  today" feature has a supported function signature
  (`allocate(..., allowed_driver_ids=..., allowed_supplier_ids=...)`)
  but no UI control to set those parameters.** Backend-ready, no
  frontend.
- **No PDF export.** Only Excel export exists. PDF was discussed as
  likely needing Excel COM automation (`pywin32`) since the target
  machine is Windows and almost certainly has Excel installed — this
  was a plan, not an implementation.
- **Driver shift rotation (e.g. "15 days morning, then 15 days
  afternoon") is not implemented.** The user explicitly wants this
  derived from `finalized_jobs` history rather than a fixed calendar
  anchor, but there isn't yet a design for *how* to derive it, and
  there likely isn't enough real finalized-day history accumulated yet
  to do so meaningfully.
- **No packaging into a `.exe`.** The app has only ever been run via
  `python -m app.main`. PyInstaller packaging was discussed as a later
  step, deliberately deferred while the app is still under heavy
  iteration (rebuilding a `.exe` on every change is unnecessary friction
  during active development).
- **Data-quality issues found in the user's real database** (not code
  bugs, but worth knowing so you don't rediscover them from scratch):
  license types were uniformly under-specified across all drivers in
  the snapshot pushed to GitHub, at least one driver (`VENUGOPAL`) was
  missing from the roster entirely, and at least one vehicle-type wording
  mismatch existed ("23 Seater Bus" vs "23 Seated Bus"). If working
  against that same repo snapshot, expect these same issues unless the
  user has since corrected them.

## 6. Recommended next improvements, roughly in priority order

Based on what's explicitly still open and what would unblock the most
value:

1. **Wire AI suggestion Accept to actually mutate the results table.**
   High value, well-scoped, the decision-logging plumbing already
   exists — this is "finish what's started," not new design work.
2. **Help the user fix their real data** (license types, missing
   drivers, vehicle-type text consistency) using the same
   extract-from-PLANNED-file technique already built and used once in
   this project (see `CHANGELOG_AI.md` Phase 6) — this will likely
   improve real-world accuracy more than further engine changes at this
   point, based on the diagnostic work already done.
3. **Build the daily driver/supplier shortlist UI**, since the backend
   already supports it — comparatively low effort for real planner
   value (handling "these specific drivers are off tomorrow" days more
   conveniently than the per-entity exclusion toggle alone).
4. **PDF export**, once the user is ready to prioritize it — needs a
   design conversation about the `pywin32`/Excel-COM approach first,
   since it's a different technical approach than everything else built
   so far (everything else is pure Python; this would shell out to a
   real Excel install).
5. **Only after the above, and only with explicit confirmation from the
   user:** consider the combined "Plan My Day" one-click shortcut
   (merging Run Planning + AI Review), driver shift-rotation logic, and
   PyInstaller packaging into a distributable `.exe`.

Do not reorder this list based on assumptions about what seems
technically interesting — confirm priority with the user, since they've
consistently driven prioritization decisions throughout this project
rather than deferring to default technical judgment.

## 7. How to work with this specific person

- They think in **real operational scenarios**, not abstractions — when
  proposing a design, it lands better paired with a concrete example
  ("if Deepak's shift starts at 6 PM and gets offered a 10 AM job...")
  than a general description of the rule.
- They have caught **real, subtle bugs by testing directly**, not by
  reading code — they will upload real files and describe unexpected
  output rather than pointing at a line of code. Take these reports
  seriously and re-derive the root cause from the actual data rather
  than guessing; this project's bug list (`CHANGELOG_AI.md`) shows this
  approach reliably finds real issues.
- They are cost-conscious about API usage and explicitly asked for the
  system to be designed so cost doesn't grow unboundedly over time (the
  whole `decision_log`/`preference_digest` two-tier design exists
  because of this one conversation) — keep this in mind before adding
  any new AI-call pattern.
- They prefer **asking a clarifying question over guessing** when a
  requirement is ambiguous, and have been given exactly that kind of
  question multiple times in this project (e.g. the supplier-numbering
  toggle question) — this worked well and should continue as the
  default approach for genuinely ambiguous new requirements, rather than
  picking a default and hoping it's right.
