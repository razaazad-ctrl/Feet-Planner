# Architecture

## Layers

UI → Engine → DB → External APIs

---

## File Flow

main.py
  → main_window.py
    → plan_day_tab.py
      → excel_import.py
      → allocation_engine.py
        → rules_parser.py
        → db.py

---

## Data Flow

Excel → Parsed Jobs → Grouped by Event → Allocation Engine → Results Table

---

## Allocation Logic

1. Filter eligible drivers
2. Check:
   - availability
   - hours
   - rules
3. Rank by:
   → lowest occupied hours
4. Assign best match
5. If none → supplier
6. If still none → unresolved

---

## Future Layer

AI Layer sits AFTER engine:

Engine → AI Review → Planner Approval