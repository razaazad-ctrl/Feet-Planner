# PROJECT_RULES.md

# Fleet Planner - Project Constitution

This document defines the permanent design principles of Fleet Planner.

These rules are intentionally stable and should only be modified after explicit approval from the project owner.

Every AI assistant working on this repository must read this file before making architectural decisions.

---

# Rule 1 - Preserve Existing Architecture

Do not redesign the application architecture unless explicitly instructed.

Prefer extending existing modules over replacing them.

Avoid unnecessary refactoring.

If a feature can be implemented without restructuring the application, that solution is preferred.

---

# Rule 2 - Deterministic Engine First

Fleet Planner is primarily a deterministic planning system.

Business rules and operational constraints must always be enforced by deterministic logic.

Artificial Intelligence is an advisory layer.

AI must never silently override deterministic business rules.

---

# Rule 3 - Planner Has Final Authority

The human planner is always the final decision maker.

AI recommendations are suggestions only.

The planner must always be able to override any recommendation.

---

# Rule 4 - Business Logic Is More Important Than Code Elegance

Never simplify business logic merely to make the code shorter.

Operational correctness always takes priority over clever implementations.

---

# Rule 5 - Fairness Definition

Fairness is measured using occupied working hours.

Fairness is NOT determined by:

* number of jobs
* number of events
* number of assignments

All future scheduling logic must respect this principle unless explicitly changed.

---

# Rule 6 - Driver Safety

Driver hour limits are hard constraints.

Do not create solutions that knowingly exceed legal or configured working limits.

No automatic overtime allocation.

---

# Rule 7 - Supplier Usage

Suppliers exist as a fallback solution.

Internal resources should always be preferred whenever a valid assignment exists.

---

# Rule 8 - Explainable Decisions

Scheduling decisions should always be explainable.

Avoid "black box" behaviour.

When AI recommends something, the reason should be understandable by a planner.

---

# Rule 9 - AI Should Assist, Not Control

AI should improve:

* route decisions
* travel efficiency
* waiting decisions
* optimisation
* natural language interpretation

AI should not replace deterministic allocation logic.

---

# Rule 10 - Protect Existing Features

Never remove existing functionality unless explicitly requested.

When modifying code:

* preserve backwards compatibility whenever possible
* avoid breaking user workflows
* avoid unnecessary behavioural changes

---

# Rule 11 - Database Integrity

Protect existing data.

Avoid destructive schema changes.

When schema changes are required:

* explain them
* document them
* provide migration guidance where practical

---

# Rule 12 - Production Quality

Code added to this project should be:

* readable
* maintainable
* modular
* well commented where necessary

Temporary hacks should be avoided.

---

# Rule 13 - Incremental Development

Prefer many small improvements over large rewrites.

Smaller changes are easier to understand, review, test and maintain.

---

# Rule 14 - Documentation Is Part Of The Feature

A coding task is not complete until the AI documentation reflects the new implementation.

Whenever applicable, update:

* AI_CONTEXT.md
* ARCHITECTURE.md
* DATABASE.md
* BUSINESS_RULES.md
* WORKFLOWS.md
* NEXT_SESSION.md
* AI_INDEX.json

Documentation and source code must remain synchronized.

---

# Rule 15 - Be Conservative

When multiple implementation options exist:

Prefer the solution that:

* changes fewer files
* introduces fewer risks
* preserves existing behaviour
* is easier to maintain
* is easier for future AI assistants to understand

Avoid unnecessary complexity.

---

# Rule 16 - Ask Before Assuming

If project requirements are unclear:

Stop.

Explain the uncertainty.

Ask the project owner before making architectural assumptions.

Never invent business rules.

---

# Rule 17 - Protect Project Knowledge

This repository is the permanent memory of the project.

Every significant architectural or behavioural change should be reflected in the AI documentation.

Future AI assistants should be able to understand the project by reading the AI folder without relying on previous conversations.

---

# Rule 18 - Definition Of Done

A task is only considered complete when:

✓ Code is updated.

✓ Relevant AI documentation is updated.

✓ Breaking changes are documented.

✓ Manual testing recommendations are provided.

✓ The final summary includes:

* Files modified
* Files created
* Files deleted
* Architectural impact
* Database impact
* AI documentation updated
* Suggested Git commit message

Only then is the task considered finished.

---

# Mission Statement

Fleet Planner is intended to be a reliable, explainable, maintainable planning system that combines deterministic scheduling with AI-assisted decision support.

The software should remain understandable to future developers and future AI assistants while prioritizing operational correctness over unnecessary complexity.
