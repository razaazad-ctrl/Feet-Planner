# Fleet Planner — Claude Code Project Instructions

## 1. PURPOSE

Fleet Planner is a fleet planning and scheduling application used to plan:

* Drivers
* Vehicles
* Trips / jobs
* Working hours
* Overtime
* Driver workload
* Vehicle allocation
* Supplier usage
* Same-driver assignments
* Planner overrides and manual scheduling decisions

The objective is to produce a reliable, maintainable, deterministic planning system while preserving the planner's final authority.

Claude Code is an implementation assistant. It must not independently redefine Fleet Planner's business rules.

---

# 2. SOURCE OF TRUTH HIERARCHY

When determining how the system should behave, use this priority:

1. Explicit instruction from the user in the current task
2. Relevant authoritative documentation in `/AI`
3. Existing implemented behavior
4. General assumptions

Never override an explicit current user instruction with an older project rule.

Do not invent business rules when the required behavior is not documented.

If the required behavior cannot safely be determined from the user instruction, `/AI`, or existing implementation, stop and ask for clarification rather than guessing.

---

# 3. CONFLICT / RULE VIOLATION ALERT — HIGH PRIORITY

The user may sometimes propose an idea, implementation, workaround, or
temporary solution that conflicts with a rule, principle, constraint, or
development practice established in this `CLAUDE.md` or the authoritative
`/AI` documentation.

When this happens, DO NOT silently follow the conflicting instruction.

Instead:

1. Clearly alert the user that the requested approach conflicts with an
   established project rule.

2. Identify the specific rule or principle that would be violated.

3. Explain briefly why the requested approach conflicts with that rule.

4. Explain the likely consequence or risk of proceeding.

5. Do NOT make the conflicting change automatically.

6. Present the user with the choice to:

   * follow the existing rule, or
   * explicitly override/change the rule.

7. If the user explicitly confirms that the rule should be changed or
   overridden, proceed with the requested change and update the relevant
   project documentation if necessary.

Example:

> ⚠️ CONFLICT WITH PROJECT RULE
>
> You asked me to replace the deterministic scheduling logic with an AI-based
> decision for this step.
>
> This conflicts with the project rule that the core scheduling engine must
> remain deterministic and AI/API functionality must remain optional.
>
> Risk: this could make scheduling less predictable and make normal testing
> dependent on an external API.
>
> I have not made the change.
>
> Options:
>
> 1. Keep the current deterministic approach.
> 2. Explicitly change the project rule and implement the AI-based approach.

### Important distinction

Do not treat every difference of opinion as a rule violation.

Only issue a conflict alert when the requested action actually contradicts:

* A rule in this `CLAUDE.md`
* An authoritative rule in `/AI`
* An explicitly established project constraint
* A safety/integrity requirement of the project
* A previously established architectural decision that is still documented

If the user is merely exploring an idea, discussing a possibility, or asking
"What if we did X?", do not block the discussion.

The alert applies when the user is asking Claude Code to actually implement,
modify, remove, or otherwise act on the conflicting instruction.

### Explicit override

The user has final authority over project decisions.

If the user explicitly says that an existing rule should be changed, overridden,
or removed, acknowledge that the previous rule is being changed.

Then:

1. Implement the requested change.
2. Update the relevant `/AI` documentation.
3. Update `CLAUDE.md` if the rule itself is a general development rule.
4. Update `AI_INDEX.json` when required.
5. Add a significant change to `/AI/07_CHANGELOG_AI.md`.
6. Clearly report that an established rule was changed.

Never silently override a documented project rule.


# 4. THE `/AI` DIRECTORY

The `/AI` directory is the authoritative project documentation.

Before making a significant change:

1. Read the relevant `/AI` documentation.
2. If the area of the system is unfamiliar, read the `/AI` files in numerical order.
3. Identify the business rules, architecture, workflow, database dependencies, and implementation requirements relevant to the requested change.
4. Follow the documented rules unless the user explicitly requests a change.

Do not treat `CLAUDE.md` as a replacement for the `/AI` documentation.

`CLAUDE.md` defines how Claude Code should work.

`/AI` defines what Fleet Planner is supposed to do.

---

# 5. TASK-FIRST DEVELOPMENT

Always understand the requested change before editing.

For significant changes:

1. Inspect the existing implementation.
2. Locate the relevant modules/functions/classes.
3. Read the relevant `/AI` documentation.
4. Determine dependencies and possible side effects.
5. Identify the minimum files that need modification.
6. Explain the intended approach when appropriate.
7. Implement the smallest safe change.
8. Test the change.
9. Inspect the resulting diff.
10. Update affected `/AI` documentation.

Do not begin by rewriting large portions of the application.

---

# 6. CHANGE SCOPE — CRITICAL RULE

Make the smallest change necessary to satisfy the user's request.

Do NOT:

* Refactor unrelated code.
* Redesign architecture without explicit approval.
* Rename unrelated functions or variables.
* Remove existing functionality because it appears unnecessary.
* Change business rules during a UI task.
* Change scheduling behavior during a cosmetic/UI task.
* Change database behavior unless required.
* Replace working code with a different implementation merely because it is preferred.
* Modify unrelated files "while you are there."

If a requested change can be implemented in one file, do not modify five files unnecessarily.

If a broader change is genuinely required, explain why before proceeding.

---

# 7. PRESERVE EXISTING FUNCTIONALITY

Existing functionality must be preserved unless the user explicitly asks for it to be changed or removed.

This is especially important for:

* Existing UI controls
* Totals and counters
* Trip calculations
* Unresolved-trip calculations
* Driver allocation
* Vehicle allocation
* Scheduling rules
* Database operations
* Planner overrides
* Supplier logic
* Same Driver functionality
* Existing filters
* Existing status indicators

Never assume that something can be removed simply because it appears redundant.

A visual/UI redesign must not silently remove functional elements.

---

# 8. UI CHANGE RULE

When the user requests a UI change:

1. Modify the UI only as requested.
2. Preserve the underlying functionality.
3. Preserve existing calculations and data.
4. Preserve existing buttons, counters, labels, and status information unless explicitly told to remove them.
5. Do not change scheduling logic unless the UI change genuinely requires it.
6. Do not change database behavior unless genuinely required.
7. Compare the requested design against the existing implementation before editing.

A UI request is NOT permission to redesign the application logic.

---

# 9. SCHEDULING ENGINE PRINCIPLES

Fleet Planner's scheduling system must follow the documented scheduling rules.

The following principles are critical:

### 9.1 Working Hours

Driver working hours are a primary planning constraint.

The configured/planner-defined working hours must be respected according to the current documented rules.

Do not hard-code a fixed working-hour limit unless the `/AI` documentation explicitly requires it.

The planner may need to adjust working hours for unusually busy days according to the application's documented rules.

### 9.2 Fairness

Driver workload should be distributed fairly.

Fairness must consider occupied/working hours, not merely the number of jobs.

The goal is to avoid:

* One driver receiving excessive hours while another remains idle.
* One driver receiving many short jobs while another receives fewer but much longer jobs.
* Leaving an available driver idle when suitable work exists.

Working-hour balance takes priority over simply counting jobs.

### 9.3 Same Driver

The `Same Driver` information provided by the planner is an important scheduling constraint.

If multiple requests contain the same driver/event assignment information, the scheduler must respect the documented Same Driver logic.

Do not ignore or overwrite planner-provided Same Driver information.

### 9.4 Overlapping Jobs

Overlapping trips must be evaluated according to the documented scheduling rules.

Do not automatically assume that every overlap is impossible.

The system must distinguish between:

* Genuine impossible conflicts.
* Jobs that can be accommodated according to documented rules.
* Planner-approved/manual scheduling decisions.
* Jobs requiring further review.

Do not invent overlap behavior.

### 9.5 Idle Time

Idle time can be relevant when improving driver utilization.

Where the documented scheduling algorithm permits squeezing an additional job into available time, the existing hard rules must be evaluated first.

A manually or specially accommodated job must remain identifiable as a planner decision where required by the specification.

### 9.6 Overtime

Overtime behavior must follow the current `/AI` documentation.

Do not reintroduce an obsolete hard-coded overtime cap.

If the project documentation permits planner-controlled working hours or extended hours for busy days, preserve that behavior.

### 9.7 Supplier Drivers

In-house drivers should be utilized before suppliers.

Supplier drivers are a fallback resource when the available in-house capacity cannot satisfy the documented scheduling requirements.

Do not call suppliers unnecessarily.

Do not change supplier allocation rules without explicit instruction.

### 9.8 Vehicles

Vehicle allocation must follow the documented vehicle rules and compatibility requirements.

Do not assign incompatible vehicles simply to eliminate an unresolved trip.

---

# 10. DETERMINISTIC-FIRST PRINCIPLE

The core scheduling engine should remain deterministic and rule-based unless the user explicitly approves a change.

AI and external APIs are advisory/optional components, not a replacement for the core hard-rule scheduling engine.

The basic scheduler must remain functional without requiring paid AI or external API calls for normal rule-based planning and testing, where the current architecture supports this.

Do not introduce AI/API dependency into core scheduling logic without explicit approval.

---

# 11. PLANNER IS FINAL AUTHORITY

Fleet Planner is intended to assist the planner, not replace the planner.

The planner must remain able to review and make final decisions where the documented workflow permits manual intervention.

Do not remove planner control in favor of fully automatic behavior without explicit approval.

Manual planner decisions should remain distinguishable from automatically generated scheduling decisions when required by the UI/specification.

---

# 12. DATABASE SAFETY

Before changing database-related code:

1. Inspect the existing schema/model.
2. Identify all affected queries and relationships.
3. Check how the application currently reads and writes the data.
4. Determine whether migrations or compatibility changes are required.
5. Avoid destructive schema changes.

Do not modify the database structure simply to make a code change easier.

Do not delete existing data.

Do not run destructive database operations without explicit user approval.

---

# 13. ARCHITECTURE

Do not redesign the Fleet Planner architecture without explicit approval.

Prefer:

* Existing project patterns
* Existing modules
* Existing database structures
* Existing UI patterns
* Small targeted changes
* Maintainable code
* Clear separation of concerns

Do not introduce a new framework, dependency, architecture, or major design pattern merely because it is technically preferable.

---

# 14. DEPENDENCIES

Before adding a dependency:

1. Check whether the functionality already exists in the project.
2. Check whether Python/standard-library functionality is sufficient.
3. Check the existing dependency structure.
4. Consider deployment and maintenance implications.
5. Explain the reason for the new dependency.

Do not add unnecessary packages.

---

# 15. CODE QUALITY

Code should be:

* Readable
* Maintainable
* Explicit
* Consistent with the existing project
* Appropriately commented
* Focused on the actual requirement

Avoid clever solutions when a straightforward implementation is available.

Do not hide important business logic inside unexplained helper functions.

Do not silently swallow errors.

Preserve existing error handling unless it is part of the requested change.

---

# 16. TESTING REQUIREMENTS

After making changes:

1. Check for syntax errors.
2. Run relevant automated tests if available.
3. Run relevant application/module checks.
4. Verify the affected functionality.
5. Check for obvious regressions.
6. Inspect `git diff`.

For UI changes, verify that the affected UI still contains all existing required functional elements.

For scheduling changes, test representative scheduling scenarios, including edge cases relevant to the modified rule.

Never claim that a test was performed if it was not actually run.

Clearly distinguish:

* Tests actually executed
* Tests unavailable
* Tests that require manual verification

---

# 17. MANUAL TESTING

The developer/planner may manually test the application after Claude Code changes.

Do not assume manual testing has happened unless the user confirms it.

When handing a change back to the user, provide concise manual-test instructions when appropriate.

For example:

* Open the affected screen.
* Load a planning day.
* Verify Total Trips.
* Verify Unresolved Trips.
* Verify driver allocation.
* Verify supplier behavior.
* Verify the affected UI behavior.

---

# 18. GIT SAFETY

Before significant modifications:

```
git status
```

After modifications:

```
git diff
```

Never:

* `git reset --hard`
* Delete user changes
* Checkout over uncommitted work
* Force-reset the repository
* Delete branches
* Rewrite history

unless the user explicitly requests the specific destructive operation.

Never assume existing uncommitted changes were created by Claude.

Preserve user work.

---

# 19. AI DOCUMENTATION UPDATE REQUIREMENT

A task is NOT considered completely finished when implementation changes require corresponding `/AI` documentation changes.

Update affected AI documentation when the change affects:

* Business rules
* Scheduling rules
* Architecture
* Modules
* Workflows
* Dependencies
* Database structure
* Database behavior
* User workflows
* Important system behavior

Regenerate `AI_INDEX.json` when required by the project's documentation rules.

Add significant changes to:

```
/AI/07_CHANGELOG_AI.md
```

Do not update unrelated AI files.

Documentation changes must accurately describe the implemented behavior.

Do not document behavior that does not actually exist.

---

# 20. DOCUMENTATION TRANSPARENCY

AI-authored documentation changes must be explicit and reviewable.

Do not hide important architectural or business-rule changes inside code comments or undocumented behavior.

When documentation is updated, report:

* Which AI files changed
* Why they changed
* What behavior is now documented

---

# 21. FILE MODIFICATION DISCIPLINE

Before modifying a file:

* Read the relevant existing code.
* Understand its role.
* Identify dependencies.
* Preserve unrelated sections.

Prefer targeted edits.

Do not rewrite an entire file when a smaller modification is sufficient.

If a file must be substantially rewritten, explain why.

---

# 22. OUTPUT / COMPLETION REPORT

After completing a significant task, report:

### Files changed

List every modified, created, or deleted file.

### Implementation

Briefly explain what was changed.

### AI documentation

State which `/AI` files were updated, or explicitly state that no AI documentation change was required.

### Architecture impact

State whether architecture changed.

### Database impact

State whether database structure or behavior changed.

### Breaking changes

State whether any breaking changes were introduced.

### Testing

State exactly what was tested.

### Remaining issues

State any unresolved issues, limitations, or manual testing still required.

Do not claim success without evidence.

---

# 23. WHEN TO ASK THE USER

Ask for clarification when:

* Two documented rules conflict.
* The requested behavior is ambiguous and cannot safely be inferred.
* A change would require architectural redesign.
* A database-destructive operation appears necessary.
* Existing behavior conflicts with the requested change and the intended outcome is unclear.
* A significant business rule would need to be invented.

Do NOT ask unnecessary questions when the request is clear and can be safely implemented from the existing project rules.

---

# 24. PLAN MODE FOR SIGNIFICANT CHANGES

For complex or potentially risky tasks, inspect and plan before editing.

The plan should identify:

1. Root cause/problem.
2. Relevant files.
3. Relevant `/AI` documentation.
4. Proposed changes.
5. Potential side effects.
6. Testing approach.
7. Documentation updates required.

Do not make broad changes simply because they might solve the problem.

---

# 25. UI AND BUSINESS LOGIC SEPARATION

Treat UI and scheduling/business logic as separate concerns.

If the user asks for:

"Change the popup design"

do not automatically modify:

* Scheduling algorithms
* Driver allocation
* Vehicle allocation
* Database schema
* Trip calculations

If the user asks:

"Fix the scheduling algorithm"

do not automatically redesign the UI.

Only cross these boundaries when technically necessary or explicitly requested.

---

# 26. NEVER REMOVE SOMETHING WITHOUT PERMISSION

This is a high-priority project rule.

If the user asks to add/change something, do not interpret that as permission to remove other existing functionality.

Examples:

If the user asks to change a popup:

* Do not remove counters.
* Do not remove totals.
* Do not remove unresolved-trip information.
* Do not remove existing data.
* Do not remove controls unless explicitly requested.

If the user asks to change scheduling:

* Do not remove existing scheduling rules unless explicitly requested.
* Do not replace deterministic logic with AI.
* Do not remove planner controls.

---

# 27. PROJECT-SPECIFIC DEVELOPMENT PHILOSOPHY

Fleet Planner should evolve incrementally.

Priorities are:

1. Correctness
2. Preservation of existing functionality
3. Deterministic and explainable scheduling
4. Fair driver workload
5. Planner control
6. Maintainability
7. Clear documentation
8. UI improvements
9. Optional AI/API enhancements

Do not sacrifice correctness or maintainability for a quick implementation.

---

# 28. FINAL RULE

Before considering a task complete, ask internally:

* Did I understand the user's exact request?
* Did I inspect the existing implementation?
* Did I read the relevant `/AI` documentation?
* Did I modify only what was necessary?
* Did I accidentally remove existing functionality?
* Did I change scheduling/business logic unintentionally?
* Did I change database behavior unintentionally?
* Did I preserve planner control?
* Did I test the change?
* Did I inspect the final diff?
* Did I update the affected AI documentation?
* Did I accurately report what changed?

If the answer to any applicable question is "no", the task is not complete.
