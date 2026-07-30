# Fleet Planner – AI Context

## Purpose
Fleet Planner is a desktop application built using PySide6 to manage and optimize vehicle, driver, and supplier allocation for event-based logistics operations.

The system processes daily Excel input and assigns drivers and vehicles while respecting operational constraints and fairness.

---

## Core Architecture

The application is divided into:

- UI Layer (`app/ui`)
- Business Logic Layer (`app/`)
- Database Layer (`db.py`)
- External Services (Maps + AI)

---

## Main Modules

### 1. Excel Import (`excel_import.py`)
- Reads structured Excel input
- Extracts:
  - Event ID
  - Time windows
  - Locations
  - Vehicle type
- Groups rows by Event ID

---

### 2. Allocation Engine (`allocation_engine.py`)
Deterministic core engine.

Responsibilities:
- Assign drivers and vehicles
- Enforce constraints
- Ensure fairness

#### Rules:
- Fairness = **occupied hours**, not job count
- Driver hour limits are **hard limits**
- No overtime allowed
- Supplier used only if no internal option exists

---

### 3. Rules Parser (`rules_parser.py`)
- Parses rule lines for drivers, suppliers, vehicles
- Applies constraints dynamically

---

### 4. Database (`db.py`)
SQLite-based.

Stores:
- Drivers
- Vehicles
- Suppliers
- Rules

Important:
- Case-insensitive uniqueness enforced
- DB must be deleted when schema changes

---

### 5. AI Layer (Partial)

#### `ai_review.py`
- Placeholder for AI decision layer

#### `maps_client.py`
- Will integrate Google Maps API
- Used for:
  - Distance
  - Travel time
  - Traffic

---

### 6. UI (PySide6)

Main Tabs:
- Drivers
- Vehicles
- Suppliers
- Locations
- Plan a Day (core feature)

---

## Planning Workflow

1. Upload Excel file
2. Add Day Notes (free text context)
3. Run planning
4. Engine assigns:
   - Driver
   - Vehicle
   - Supplier (fallback)
5. Output shows:
   - Assignments
   - Unresolved jobs (critical)

---

## Key Concepts

### Event Grouping
Multiple rows can belong to the same event.

Example:
- Equipment delivery
- Staff transport
- Food delivery
- Teardown

All linked via Event ID.

---

### AI Role (Planned)

AI will decide:

- Stay on-site vs return
- Travel efficiency
- Special day notes interpretation

---

### Day Notes
Planner can override logic using natural language.

Example:
- VIP event
- Long waiting times
- Special driver conditions

---

## Current State

✅ Excel parsing working  
✅ Event grouping working  
✅ Allocation engine working  
✅ UI working  
❌ AI decision layer not fully implemented  
❌ Maps API not connected  

---

## Known Limitations

- No real-time travel data yet
- No AI reasoning layer active
- DB resets required after schema changes
- No audit trail of decisions

---

## Philosophy

- Deterministic logic = always correct
- AI = suggestion layer only
- Planner = final authority