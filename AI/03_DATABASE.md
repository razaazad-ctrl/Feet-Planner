# Database

## Type
SQLite

File: fleetplanner.db

---

## Entities

### Drivers
- name (unique, case-insensitive)
- rules

### Vehicles
- plate (unique)
- type
- capacity
- notes

### Suppliers
- name
- vehicle type

---

## Important Behavior

- Case-insensitive uniqueness enforced
- Trailing spaces removed
- DB must be recreated after schema changes

---

## Relationships

Drivers ↔ Vehicles (implicit via assignment)

Events:
- Not stored in DB
- Loaded from Excel dynamically

---

## Future Tables

- vehicle_issues (planned)
- planning_history