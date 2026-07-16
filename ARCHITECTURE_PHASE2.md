# LAB::INV — Phase 2 Architectural Plan

Status: PLANNED — not yet implemented.
This document describes the data model and subsystem changes needed before the system
is reliable at scale (>500 components, >10 active projects, multi-box physical layout).

Each section names: what is wrong now, what the target state is, what schema/code changes
are required, and what the migration path looks like.

---

## ISSUE-A: Footprint is semantically overloaded

### Current state
`Footprint` is used simultaneously to represent:
1. A tape reel or physical lot (stripe_color, tape_color, manufacturer, source)
2. A stock counter (quantity, sigma_adjustment, low_stock_threshold)
3. A bin anchor — `BinAssignment.footprint_id` points here
4. An acquisition batch (there is no purchase date, unit cost, or lot code)

### Target state
Split into two tables:

```
StockLot (replaces Footprint)
  id
  component_id          FK → components
  quantity              current count
  sigma_adjustment      calibration offset
  low_stock_threshold
  source                "DigiKey order #12345", "mouser", "AliExpress", etc.
  manufacturer          brand name
  lot_code              date code / batch code from reel label
  unit_cost             cost per unit at acquisition
  acquired_at           timestamp of receipt
  notes

AcquisitionRecord (new, optional link to PurchaseOrderItem)
  id
  stock_lot_id          FK → stock_lots
  purchase_order_item_id  FK → purchase_order_items (nullable)
  quantity_received
  received_at
  unit_cost_at_receipt
```

`BinAssignment.footprint_id` becomes `BinAssignment.stock_lot_id`.

### Migration path
1. Add migration: rename `footprints` → `stock_lots` (ALTER TABLE)
2. Add new columns: `lot_code`, `unit_cost`, `acquired_at`
3. Update all FK references: `bin_assignments.footprint_id` → `stock_lots_id`
4. Update all Python: `Footprint` model → `StockLot`, all router/service references
5. No data loss — all existing rows carry forward

### Files affected
- `app/models/models.py` — rename class, update FKs
- `app/routers/components.py` — all Footprint references
- `app/routers/boxes.py` — BinAssignment.footprint_id
- `app/routers/kits.py` — _apply_kit_stock_influence
- `app/routers/suppliers.py` — any footprint-linked receive logic
- `app/services/migrations.py` — migration entry

---

## ISSUE-B: Generic/Variant system has undefined semantics

### Current state
`Component.is_generic` and `Component.parent_id` exist. Intended to let you define
"10kΩ 0603 Resistor (generic)" with Yageo and KOA variants as children. Problems:

1. The components list page sums stock for generic + all children, but also shows
   each child as its own row — double-counts visually.
2. `BOMItem.component_id` points to a single component. If BOM references the generic,
   stock lookup finds zero (generic has no footprints; children do).
3. `create_component` does not validate that `parent_id` is a generic — only
   `set_generic_parent` PATCH does. Create can produce orphaned hierarchy.
4. No UI for managing the hierarchy except PATCH API calls.

### Decision required (pick one)

**Option A — Make generic/parent a data cleanup tool only**
- `is_generic` means "this is a canonical definition, show it prominently in search"
- `parent_id` means "this is a duplicate of another component, merge it later"
- BOM never references generics — always references a specific component
- Aggregate stock display on list page is for UI convenience only, clearly labeled
- This is a low-cost fix: add the label, make the double-count explicit

**Option B — Make generic/parent a true parametric resolver**
- `BOMItem` gains `spec_requirements: JSONB` alongside `component_id`
- A `BOMItem` with `spec_requirements` and no `component_id` is parametric:
  `{"type_path": "passives/resistor", "value_min": 9900, "value_max": 10100, "package": "0603"}`
- At build-check time, system queries all components matching the spec and sums stock
- Requires: new `bom_line_spec` table or JSONB column, resolver function, UI for spec entry
- High implementation cost; correct long-term solution

**Recommendation**: Implement Option A now (low risk), plan Option B for Phase 3.

### Option A implementation
1. Add `app/main.py` component list: when showing a generic, suffix the stock display
   with "(incl. variants)" to make the aggregation explicit.
2. Add constraint in `create_component`: if `parent_id` provided, verify target exists
   and is `is_generic=True` — same check as `set_generic_parent`.
3. Add simple hierarchy indicator in the component list template (indent child rows or
   show a "⤷ variant of X" badge).
4. No schema changes needed.

---

## ISSUE-C: Location model is flat — no zone/fixture hierarchy

### Current state
```
Box (label, model, cell_count, location: VARCHAR, slot_index: INT)
BinAssignment (box_id, cell_id, component_id, active)
```

`location` is a free-text string. `slot_index` is a display order hint.
There is no way to express:
- "These 12 boxes are on Bench A, those 8 boxes are in the wall cabinet"
- "Bench A is next to the soldering station; wall cabinet is 3m away"
- "Pick these parts in this order to minimize walking"

### Target state

```
-- Location types (static reference data)
location_type
  id, name ("bench", "shelf-unit", "cabinet", "drawer-unit", "box-144", "box-96")
  is_container BOOL     -- can hold components directly (vs. holding other locations)
  grid_rows INT         -- if is_container
  grid_cols INT         -- if is_container

-- Physical location tree
location
  id
  parent_id             FK → location (self-referential)
  location_type_id      FK → location_type
  label                 "Bench A", "Wall Shelf Row 2", "Box 007"
  address               computed path: "BENCH-A.SHELF-2.BOX-007"
  slot_index            display order within parent

-- Cell assignment (unchanged except FK target)
bin_assignment
  location_id           FK → location (replaces box_id)
  cell_id               "R3C7" (still row/col notation)
  component_id          FK → components
  stock_lot_id          FK → stock_lots (replaces footprint_id)
  active BOOL
```

### Migration path
1. New tables `location_type` and `location` alongside existing `boxes`.
2. Migration: seed one `location_type` for each existing box model (BOXALL144, etc.).
3. Migration: for each existing `Box`, create a `location` row with the same label/notes,
   parent = NULL (flat root), copying `slot_index`.
4. Migration: update `bin_assignments.box_id` → `location_id` using the new location IDs.
5. Keep `boxes` table readable for backward compatibility until all routes are updated.
6. Update `boxes.py` router to operate on `location` table.
7. Update box grid page to show location tree in sidebar.

### Benefit at current scale
Even at 20 boxes, the location tree gives you:
- Group boxes by physical area (bench vs. shelf vs. storage)
- Reorder boxes within a group without affecting other groups
- Show "which zone" in scan results ("Bench A → Box 007 → R3C7")

---

## ISSUE-D: No parametric BOM matching (Phase 3 item)

### Current state
`BOMItem.component_id` is a hard FK. BOM says "I need component UUID abc123, qty 4."
Kit availability check (`GET /api/kits/{id}/availability`) sums stock for exact FK matches.
No way to express "I need any 10kΩ 0603 resistor, ±5%, ≥100mW."

### Target state (Phase 3, not Phase 2)
```
bom_line_spec (new table or JSONB column on bom_items)
  bom_item_id           FK → bom_items
  type_path             "passives/resistor"
  value_min FLOAT       9900 (Ohm)
  value_max FLOAT       10100
  package               "0603"
  tolerance_max FLOAT   5.0 (%)
  voltage_min FLOAT     (optional)
  current_min FLOAT     (optional)
  power_min FLOAT       (optional)
  preferred_mpn         (optional, hints resolver toward specific part)
```

Resolver query (pseudocode):
```sql
SELECT c.id, SUM(sl.quantity + sl.sigma_adjustment) AS stock
FROM components c
JOIN stock_lots sl ON sl.component_id = c.id
WHERE c.type_path LIKE 'passives/resistor%'
  AND c.value::FLOAT BETWEEN :value_min AND :value_max
  AND c.package = :package
  AND (c.tolerance IS NULL OR CAST(REPLACE(c.tolerance,'%','') AS FLOAT) <= :tolerance_max)
GROUP BY c.id
ORDER BY stock DESC
```

This is deferred because it requires:
- Consistent numeric storage for `value` (currently VARCHAR — needs migration to FLOAT)
- Consistent `unit` standardization (some values are "10k", some "10000", some "10kΩ")
- Resolver UI on the BOM line edit form

---

## ISSUE-E: No audit/maintenance subsystem (Phase 3 item)

### What is missing
No mechanism to periodically verify that physical inventory matches the database.
The InfluxDB scan events are write-only; there is no "audit session" workflow.

### Target state (Phase 3)
```
audit_task
  id
  location_id           FK → location
  scheduled_date DATE
  completed_date DATE
  notes TEXT
  discrepancies JSONB   [{component_id, barcode_id, expected_qty, actual_qty}]

access_log
  location_id           FK → location
  timestamp TIMESTAMPTZ
  action                "scan", "take", "put", "audit"
  component_id          FK → components (nullable)
```

Workflow:
1. `POST /api/audits/schedule` — schedule an audit for a box/location
2. `GET /api/audits/pending` — list boxes due for audit
3. `POST /api/audits/{task_id}/start` — returns expected inventory from DB
4. `POST /api/audits/{task_id}/scan` — user scans each component; system records actual
5. `POST /api/audits/{task_id}/complete` — compares actual vs. expected, writes discrepancies
6. Discrepancies generate `calibrate` inventory events (or flag for manual review)

---

## Implementation Order

| Phase | Items | Prerequisite |
|---|---|---|
| **Done** | FIX-01 through FIX-13 (emergency patches) | — |
| **Phase 2A** | ISSUE-A: Footprint → StockLot rename | None |
| **Phase 2B** | ISSUE-B Option A: generic/variant display fixes | None |
| **Phase 2C** | ISSUE-C: Location hierarchy (new tables alongside existing) | ISSUE-A done |
| **Phase 3A** | ISSUE-B Option B: parametric BOM matching | ISSUE-C done, value normalization done |
| **Phase 3B** | ISSUE-E: Audit subsystem | ISSUE-C done |
| **Phase 3C** | Value normalization (VARCHAR → FLOAT for resistances/caps) | None, but blocks Phase 3A |

---

## What NOT to change

- `get_db()` auto-commit pattern — correct as-is; per-row savepoints added to loops
- Migration system (raw SQL, idempotent) — working, keep as-is
- Barcode ID format — working, keep as-is
- AI provider abstraction — working, keep as-is
- WebSocket broadcast pattern — working, keep as-is
- `component_lookups` cache table — working, keep as-is
