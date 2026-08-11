# Workflows

## Planning a Day

1. Upload Excel
2. Add day notes
3. Run engine
4. Review the result table and optional driver/supplier filter
5. Click **Summary** beside **Export Filled Excel** when a workload snapshot is needed
6. Close the summary and continue reviewing/editing the plan

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

→ Read the current in-memory job results
→ Count total trips, assigned in-house drivers, in-house-assigned trips, suppliers used, supplier trips, and unresolved jobs
→ Group assigned jobs by driver and supplier from the result objects only
→ Calculate each driver's duty span, trip count, and merged worked hours
→ Display modern metric cards for the primary totals (including `In-house trips`)
→ Display a structured table with an explicit `IN-HOUSE DRIVERS` group first
→ Display a `SUPPLIERS` group second when supplier records exist
→ Show both groups directly; no popup filter checkbox is required
→ Use modern metric-card icons: driver/supplier line-art icons plus the supplied
  trip clipart for both trip cards
→ Display Total trips and Unresolved trips in the footer without repeating the
  other header-card totals

No database read, API call, allocation rerun, or workbook modification occurs.
