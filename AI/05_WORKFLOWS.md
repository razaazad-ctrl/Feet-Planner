# Workflows

## Planning a Day

1. Upload Excel
2. Add day notes
3. Run engine

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