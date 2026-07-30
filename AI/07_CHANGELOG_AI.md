# CHANGELOG_AI.md

# AI Architectural Change Log

This document records significant architectural and behavioural changes to Fleet Planner.

It is **not** a replacement for Git history.

Git records **what changed**.

This document records **why it changed**, **what impact it has**, and **what future AI assistants should know**.

Only significant changes should be recorded.

Do **not** record every bug fix or minor code cleanup.

---

# Entry Template

## YYYY-MM-DD

### Summary

Brief description of the change.

### Reason

Why was this change necessary?

### Files Affected

List only the major files.

### Architectural Impact

Describe whether this changes:

* module relationships
* workflows
* allocation logic
* database
* AI layer
* UI

### Business Logic Impact

Explain whether operational behaviour changed.

### Database Impact

Explain any schema or data changes.

### Backwards Compatibility

State whether older behaviour is preserved.

### Future Notes

Anything a future AI assistant should know before modifying this area again.

---

# Example Entry

## Example

### Summary

Introduced Google Maps travel-time integration.

### Reason

Travel estimation based on static assumptions produced poor planning decisions.

### Files Affected

* maps_client.py
* allocation_engine.py

### Architectural Impact

Added Maps Service between Allocation Engine and AI Review.

### Business Logic Impact

Travel duration now comes from Google Maps rather than fixed estimates.

### Database Impact

None.

### Backwards Compatibility

Existing planning behaviour remains unchanged when Maps is unavailable.

### Future Notes

All future travel optimisation should use Maps Service instead of hardcoded values.
