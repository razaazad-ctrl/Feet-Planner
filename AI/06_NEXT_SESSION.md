# Instructions for Next AI

1. Read:
   - AI_CONTEXT.md
   - ARCHITECTURE.md
   - DATABASE.md

2. Do NOT redesign architecture unless asked

3. Respect:
   - fairness = hours
   - no overtime
   - supplier fallback logic

4. When modifying code:
   - list changed files
   - explain impact

5. Do NOT break deterministic engine

6. AI layer must remain optional and explainable

---

## Current Focus

Next priority:
→ AI + Maps integration

---

## Known Sensitive Areas

- allocation_engine.py
- db.py
- event grouping logic

Be careful modifying these