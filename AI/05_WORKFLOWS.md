# Workflows

## Planning a Day

1. Upload Excel
2. Add day notes
3. Run engine
4. Review the result table, all 14 columns available (6 hidden by default,
   toggle via **Columns**), filterable by driver/supplier AND by event
5. Manually reassign any row's Driver/Supplier or Vehicle/Unit directly in
   the table if needed (see "Manual Reassignment + ReCheck" below)
6. Click **ReCheck** any time after manual edits to catch clashes/rule
   breaks before finalizing
7. Click **Summary** beside **Export Filled Excel** when a workload snapshot is needed
8. Close the summary and continue reviewing/editing the plan
9. **Finalize Day** to save to history, then **Export Filled Excel**

---

## Manual Reassignment + ReCheck

Request #2 (Phase 29, 2026-08-15): the planner can override the engine's
own driver/vehicle assignment directly in the results table, then use a
dedicated check to catch mistakes before finalizing -- planner override
stays completely unrestricted (Rule 3, planner is final authority), the
check is purely advisory.

**Driver/Supplier and Vehicle/Unit columns are editable combo boxes:**
- List EVERY driver/supplier/vehicle, including ones currently marked
  "excluded from planning" (off-duty) -- deliberate: the planner must be
  able to pull an off-day driver onto a busy day. Only each table's own
  `active` (still-on-roster) flag is respected.
- Editable + type-ahead filtered (type the first letters to narrow a long
  list), and free-text entry is allowed (e.g. typing a specific supplier
  hired-unit label like "ABC Rentals 2").
- Picking a supplier in Driver/Supplier auto-fills Vehicle/Unit with the
  same label (matches the existing on-screen convention); still editable
  afterward.
- Changing either combo updates the underlying `Job`'s clean assignment
  fields (`assigned_driver_id`/`assigned_driver_name`/`assigned_vehicle_id`/
  `assigned_vehicle_plate`/`assigned_supplier_id`/`assigned_supplier_unit`/
  `unresolved`) **in memory only** -- nothing is written to the database
  until **Finalize Day** is clicked (unchanged from every other edit to the
  in-memory plan). `assignment_note` is overwritten to "Manually reassigned
  by planner" so the Note column never shows a now-inaccurate engine
  explanation next to a different driver.

**ReCheck button** (next to AI Review): re-scans the CURRENT in-memory
sheet -- including manual edits -- and flags, in red in the Note column,
without changing any assignment:
- Driver double-booked (two overlapping trips, same driver)
- Vehicle double-booked (two overlapping trips, same in-house vehicle or
  same supplier hired-unit label)
- A driver's hours over the hard daily ceiling or monthly overtime budget
  (same duty-SPAN math the engine itself enforces)
- An assigned in-house vehicle whose type doesn't match the job's required
  vehicle type

Rows sharing a non-blank "Same Driver" value are exempt from the
driver/vehicle double-booking checks (legitimate back-and-forth), matching
the engine's own overlap exemption. Re-runnable any number of times after
further edits. See `app/ui/plan_day_tab.py`'s `_compute_recheck_issues()`.

**Deliberately NOT checked: a "Same Driver" group split across more than
one driver.** "Same Driver" is a documented SOFT preference (see
`04_BUSINESS_RULES.md`'s "Same Driver Column" section: "if one driver
truly cannot cover the whole flagged group... the system brings in as few
additional drivers as possible") -- a split is often the engine's correct,
optimal decision, not a mistake. An earlier version of ReCheck flagged
every split as a rule break and produced 47 false-positive warnings on a
real, untouched, 0-unresolved optimal plan -- removed 2026-08-15 (Phase
29b) rather than kept as noise, since there's no reliable way to tell a
deliberate/necessary split from a planner mistake from the data alone.

---

## Allocation Decision

FOR each job:

→ Match vehicle type
→ Check driver availability
  (if the job is flagged in the "Same Driver" column, overlap with
   another row sharing that same flag is not treated as a conflict --
   see Business Rules)
→ Check hours
→ Rank by fairness
  (prefer a driver already used for this same "Same Driver" group, if
   they still qualify, before falling back to normal hours-fairness)
→ Assign

---

## Event Chain Logic (Future AI)

Event stages:
- Delivery
- Setup
- Service
- Teardown

Decision:
- Stay on-site OR return

Based on:
- travel time
- gap duration
- driver hours

---

## Failure Handling

If no assignment:
→ Mark unresolved
→ Show reason
---

## Result Summary

When the planner clicks **Summary**:

→ Read the current in-memory job results, plus the last Run Planning's
  driver profiles (for each driver's `working_hours_per_day`, needed by
  the Overtime column -- Phase 25)
→ Count total trips, assigned in-house drivers, in-house-assigned trips, suppliers used, supplier trips, and unresolved jobs
→ Group assigned jobs by driver and supplier from the result objects only
→ Calculate each driver's duty span, trip count, merged worked hours, and
  overtime (`max(0, span - working_hours_per_day)`, "--" if that driver's
  working hours aren't known here or for supplier rows)
→ Display modern metric cards for the primary totals (including `In-house trips`)
→ Display a structured table with an explicit `IN-HOUSE DRIVERS` group first
→ Display a `SUPPLIERS` group second when supplier records exist
→ Show both groups directly; no popup filter checkbox is required
→ Use modern metric-card icons: driver/supplier line-art icons plus the supplied
  trip clipart for both trip cards
→ Display Total trips and Unresolved trips in the footer without repeating the
  other header-card totals

No new database read, API call, allocation rerun, or workbook modification
occurs -- the driver profiles used for the Overtime column were already
fetched from the database once, at Run Planning time, not re-queried here.
