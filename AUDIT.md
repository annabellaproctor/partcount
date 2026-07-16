# LAB::INV — System Audit

Issues ranked from critical (data loss / silent corruption) down to cosmetic. Each item names the exact file and line range where it lives.

---

## SEVERITY 10 — Data Loss / Silent Corruption

### 1. `get_db()` commits on every request, no transaction scope control
**File**: `app/models/database.py:14-16`

```python
async with AsyncSessionLocal() as session:
    try:
        yield session
        await session.commit()   # ← commits everything the route touched
```

The session is committed unconditionally after the route finishes. This means:
- A route that does 5 DB writes, crashes partway through, and raises an exception will still commit whatever SQLAlchemy flushed before the exception if the flush happened before the raise. SQLAlchemy auto-flushes on certain queries.
- There is no explicit `begin()` call, so autobegin is used. Any `db.flush()` inside a route does not create a savepoint — if the route does partial work and raises, the rollback in the `except` block recovers, but only if the exception propagates before commit. If a route catches its own exception internally and continues, the half-state is committed.
- The real risk: `import_components_new` (`components.py:379-440`) loops over rows, calling `db.add()` for each, then calls `db.flush()` at the end. If a row causes a DB constraint violation that isn't caught inside the loop, the session goes into a broken state. Because the outer `get_db` then calls `rollback()` on the already-broken session, this surfaces as a cryptic SQLAlchemy error rather than a useful message — and any rows that were already flushed may or may not persist depending on PostgreSQL's transaction state.

**Fix**: Explicitly begin a transaction in routes that do multi-step writes, or use explicit savepoints for bulk operations.

---

### 2. Kit barcode IDs are sequential and predictable — collision on concurrent creation
**File**: `app/routers/kits.py:241-248`

```python
async def _next_kit_barcode(db: AsyncSession) -> str:
    rows = (await db.execute(select(Kit.barcode_id))).scalars().all()
    max_num = 0
    for bid in rows:
        m = re.match(r"^K(\d+)$", bid or "")
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"K{max_num + 1:03d}"
```

This reads the current max then returns `max + 1` — a classic TOCTOU race. If two kit creations land at the same time both will read the same max and produce the same barcode (e.g., `K005`). Since `barcode_id` is `UNIQUE NOT NULL`, one request will succeed and the other will get a PostgreSQL unique constraint violation with no retry logic, surfacing as a 500 error. Additionally, the zero-padding is 3 digits (`K001`–`K999`), so any lab with >999 kits will silently produce `K1000`, `K1001`, etc., and the regex stops matching these — causing future max lookups to always return 0 and collide.

**Fix**: Use a PostgreSQL sequence (`NEXTVAL`) or a `SELECT FOR UPDATE` lock. The component barcode system uses random IDs (which sidesteps this) — kits should do the same.

---

### 3. `import_components_new` skips rows silently if barcode missing, no commit isolation
**File**: `app/routers/components.py:393-440`

The import endpoint loops over CSV rows and calls `db.add()` for each valid row, then calls `db.flush()` once at the end. If any row raises an exception during the loop (e.g., malformed `type_data` JSON after the `try/except` catches it, then something else fails later), rows already added but not yet flushed may be committed by the `get_db()` auto-commit even though the function reported errors. The caller gets `{"created": N, "errors": [...]}` but the actual DB state may be inconsistent.

Separately: `db.flush()` at line 439 but no `db.commit()`. The `get_db` dependency commits. This means if the route raises after the flush (e.g., a secondary index write fails in PostgreSQL), the session rollback happens — but the caller already got the 200 response JSON if the exception happened after the return.

**Fix**: Wrap each row's `db.add()` + `db.flush()` in a savepoint so failed rows don't poison the session state.

---

### 4. Stock cannot go negative — no guard, physical count can silently drift below zero
**File**: `app/routers/components.py:1015-1017`

```python
if action == "take":
    fp.quantity = raw_before - qty
    quantity_change = int(fp.quantity or 0) - raw_before
```

There is no floor at zero. If a user takes 50 from a bin with 10, `fp.quantity` becomes `-40`. This persists in the DB, is displayed in the UI, is included in aggregate stock calculations, and is written to InfluxDB. The effective quantity (raw + sigma) can then be positive again if `sigma_adjustment` is large enough, masking the problem entirely.

In a physical inventory context, negative stock means the count is wrong. The system should at minimum warn when quantity would go negative; in most cases it should refuse.

**Fix**: Either reject quantity < 0 at the action level, or record the discrepancy as a separate event type (`shortage`). At minimum, clamp and log a warning.

---

### 5. `autocrop_image` overwrites the source file in-place with no backup
**File**: `app/services/barcode_svc.py:50-58`

```python
def autocrop_image(src_path: str, dest_path: str = None) -> str:
    dest_path = dest_path or src_path
    img = Image.open(src_path).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    img.save(dest_path)
```

When `dest_path` is not provided (the common case — `components.py:614` calls `autocrop_image(dest)` with no second arg), the function opens the file, converts it to RGBA, crops it, and saves it back to the same path. If `img.save()` fails partway (disk full, permission error, Pillow encode error on exotic formats), the original file is already closed and partially overwritten. The image is lost. There is also no handling for images where `getbbox()` returns `None` (fully transparent image) — the save still happens, producing a zero-content file.

**Fix**: Write to a temp file, verify it, then atomically rename over the source.

---

## SEVERITY 9 — Logic Broken / Wrong Results in Normal Use

### 6. `scan_component` joins `BinAssignment` without filtering for `active=True`
**File**: `app/routers/components.py:741-763`

```python
result = await db.execute(
    select(Component, BinAssignment)
    .join(BinAssignment, BinAssignment.component_id == Component.id, isouter=True)
    .where(Component.barcode_id == barcode_id)
)
row = result.first()
```

The join does not filter `BinAssignment.active == True`. When a component has been moved (soft-deleted old assignment, new assignment created), both rows exist in `bin_assignments`. The query returns `.first()` — whichever row PostgreSQL returns first, which is not guaranteed to be the active one. The scan will report the wrong box/cell location. InfluxDB then logs the wrong location.

**Fix**: Add `.where(BinAssignment.active == True)` to the join condition.

---

### 7. `export_all_components` and `export_selected_components` are nearly identical — diverge silently
**File**: `app/routers/components.py:283-376`

Both endpoints build the same `row = { ... }` dict with identical field mappings and call `_sanitize_row`. They are copy-pasted. The `export-all` uses `delimiter="semicolon"` as the default while `export-selected` uses `delimiter="tab"`. More dangerously: any future field addition or bug fix applied to one will be missed by the other. There is already a divergence: `export-all` uses `quoting=csv.QUOTE_MINIMAL` implicitly (the default), while `export-selected` explicitly sets it. If they were meant to behave the same, they already don't.

**Fix**: Extract a `_build_component_row(comp)` function and `_write_components_csv(comps, sep, stream)` and call it from both endpoints.

---

### 8. `component_detail` page builds `bins_with_box` using `type(...)` dynamically — fragile
**File**: `app/main.py:492`

```python
bins_with_box = [type("Bin", (), {"cell_id": r.BinAssignment.cell_id, "box": r.Box})() for r in bins]
```

This constructs anonymous classes at runtime. The template accesses `bin.cell_id` and `bin.box`. Any typo in the attribute name, or any template access to an attribute not in the dict passed to `type()`, returns `AttributeError` at render time — not at load time. If a new field is needed (e.g., `bin.footprint_id`), the dynamic class must be updated in sync with the template, with no static analysis or IDE help.

Same pattern appears for `component_suppliers` (line 501) and `purchase_history` (line 508).

**Fix**: Use `dataclasses.dataclass` or a simple `TypedDict`, or return the raw SQLAlchemy row tuples and access with `r.BinAssignment.x` directly in the template.

---

### 9. `patch_component` applies `clear_fields` after setting values from `payload` — order matters
**File**: `app/routers/components.py:1263-1276`

The route first applies all `payload` fields to the component, then applies `clear_fields`. This means a caller who sends `{"name": "New Name", "clear_fields": ["name"]}` will set the name then immediately clear it to `None`. This is probably not intended — the name is now `None` in the DB. There is no validation that `clear_fields` doesn't overlap with `payload` keys.

---

### 10. `kits.py` creates a `Footprint` with `manufacturer="KitImport"` every time stock > 0 is set, even on re-import
**File**: `app/routers/kits.py:156-168`

```python
fp = (await db.execute(
    select(Footprint).where(Footprint.component_id == comp.id, Footprint.manufacturer == mname).limit(1)
)).scalar_one_or_none()
if not fp:
    fp = Footprint(component_id=comp.id, manufacturer=mname, source="kit_import", quantity=qty)
    db.add(fp)
    return
fp.quantity = int(fp.quantity or 0) + qty
```

If the same kit is imported twice (re-paste the same AI text), and the component already exists, the function finds the existing `KitImport` footprint and adds to its quantity. This is actually correct — but the condition to match the footprint is only on `manufacturer == "KitImport"`. If the same component is in two different kits that were both imported, the second import accumulates on the first kit's footprint, not creating a separate record per kit. The stock goes up by the full quantity each time the kit is parsed, even if the user was just editing/re-submitting.

There is no idempotency check. Re-submitting the same kit parse adds stock again.

---

### 11. `next_barcode_id` uses `random.choices` without seeding — not cryptographically random, but also not the real issue
**File**: `app/services/barcode_svc.py:69-79`

The barcode generation is fine for uniqueness. The real problem is the `existing_ids` parameter: callers pass only barcodes with the same prefix letter (e.g., only `R%` barcodes). This means a `R`-prefix barcode can collide with nothing since only R-prefix IDs are checked. But the collision check is sound within that prefix. No issue here by itself.

The actual problem: `existing_ids` is loaded fresh on each call (no caching), so under concurrent component creation both calls might load the same set and generate the same barcode. The unique constraint in PostgreSQL will catch this and raise an error, but the route has no retry logic — so concurrent creation produces a 500.

---

## SEVERITY 8 — Reliability / Operational Risk

### 12. No `db.commit()` after `delete_component_image` — uses explicit commit but other routes don't
**File**: `app/routers/components.py:715-736`

```python
comp.image_path = None
await db.commit()   # ← explicit commit
```

This is the only route in the entire codebase that calls `db.commit()` explicitly. Every other route relies on the `get_db()` dependency to commit on exit. The explicit commit here is harmless but inconsistent — if this route later does additional work after the `await db.commit()`, that work is in a new implicit transaction that also gets committed by `get_db()`. It also means the image is deleted from disk before the DB is committed, so a crash between the file deletion and the DB commit leaves an orphaned null in the DB and the file gone.

---

### 13. Image file deleted before DB updated — crash window loses image permanently
**File**: `app/routers/components.py:722-726`

```python
if comp.image_path:
    try:
        file_path = os.path.join("/app", comp.image_path.lstrip("/"))
        if os.path.exists(file_path):
            os.remove(file_path)   # ← file gone
    except Exception as e:
        log.warning(...)

comp.image_path = None
await db.commit()              # ← DB updated after file delete
```

If the process crashes between `os.remove()` and `db.commit()`, the file is gone but `image_path` still points to it. Every subsequent page load for this component will produce a broken image. The correct order is: update DB first, commit, then delete file.

---

### 14. WebSocket scan handler does not look up the component from the DB — just echoes the barcode
**File**: `app/main.py:77-87`

```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data.startswith("SCAN:"):
                barcode_id = data[5:].strip()
                await websocket.send_text(f'{{"event":"scan_ack","data":{{"barcode_id":"{barcode_id}"}}}}')
    except WebSocketDisconnect:
        manager.disconnect(websocket)
```

The WebSocket handler acknowledges every scan with the raw barcode ID string, even if it doesn't exist in the database. The UI relies on this ACK to trigger a lookup. If someone scans a barcode that doesn't exist, the ACK is still sent — the UI then hits `POST /api/components/{barcode_id}/scan` which correctly 404s. But the ACK is JSON with the barcode directly interpolated into the string without escaping — if the barcode contains `"` or `\` characters (unlikely with the generated format, but possible if an external barcode scanner sends arbitrary data), this produces invalid JSON or a response injection.

**Fix**: Use `json.dumps()` for the ACK payload.

---

### 15. `run_migrations` calls `BEGIN`/`COMMIT`/`ROLLBACK` as raw text on an SQLAlchemy connection
**File**: `app/services/migrations.py:491-536`

SQLAlchemy's async connection object manages transactions automatically. Calling `text("BEGIN")` and `text("COMMIT")` directly bypasses SQLAlchemy's transaction state machine — the connection object thinks it's not in a transaction while PostgreSQL thinks it is. This works in practice because the pattern is consistent, but it means SQLAlchemy's connection pool cleanup (which issues `ROLLBACK` on return) may do unexpected things. The `text("ROLLBACK")` at line 492 is there to clear stale state, which itself indicates the fragility of this pattern.

---

### 16. `search_index` endpoint loads up to 1200 components without pagination
**File**: `app/main.py:134-196`

The endpoint takes a `limit` param (max 1200), loads all those components with their `search_alias`, `image_path`, and other fields, and returns them all as JSON in a single response. With 500+ components this is a 500KB+ JSON blob. It is called on every page load from the global search bar's initialization. This will progressively slow as the inventory grows.

---

### 17. Migration v14 randomizes `M0001` barcode using `MD5(RANDOM())` — not portable and runs on every deploy
**File**: `app/services/migrations.py:340-355`

Migration v14 uses `MD5(RANDOM()::text || CLOCK_TIMESTAMP()::text)` to generate a new barcode ID. This is fine — but the migration is run on every deploy because the version check says "if current_version < 14, run it." Once it runs once, `schema_versions` records v14 as applied and it never runs again. But the `M0001` update inside the migration is: "if `M0001` exists and a candidate barcode was found." If run a second time (schema_versions somehow reset), it would try to rename whatever now has `M0001` — which is a different component. This is a historical cleanup migration that is safe in practice but would be dangerous on a schema version table reset.

---

## SEVERITY 7 — Concept / Organizational Problems

### 18. The generic/variant component system is half-built and creates ambiguity
**Files**: `app/models/models.py:133-147`, `app/main.py:326-365`

The generic component feature (`is_generic`, `parent_id`) lets you define a "10kΩ Resistor" parent and attach brand-specific children. The aggregate stock calculation in the component list page sums child footprints. But:

- The component list shows generic components and their children as separate rows. A user sees "10kΩ Resistor (generic) — 500 units" and also "Yageo RC0603 10kΩ — 300 units" and "Vishay CRCW0603 10kΩ — 200 units" as three separate rows. The aggregate is double-counted if they look at totals naively.
- The BOM system (`bom_items`) links to a single `component_id`. If a project BOM requires "10kΩ resistor" and links to the generic, but stock is only on the children, there is no resolution path — the BOM shows 0 stock for the generic even if children have 500.
- There is no constraint preventing a generic having a parent (circular potential), only a validation in the PATCH endpoint (`set_generic_parent`) that checks `is_generic` on the target parent — but the `create_component` endpoint (`POST /api/components/`) applies `parent_id` without this check.
- There is no UI surface for managing these relationships beyond PATCH requests. The components list has no indicator of hierarchy depth.

**Core problem**: The generic system was designed for aggregation but the BOM and stock-take workflows were not updated to be hierarchy-aware. Right now it is mostly cosmetic aggregation on the list page.

---

### 19. Footprint concept is overloaded — simultaneously means "stock instance" and "physical source"
**File**: `app/models/models.py:182-196`

`Footprint` has: `manufacturer`, `source`, `stripe_color`, `tape_color`, `quantity`, `sigma_adjustment`, `low_stock_threshold`. This is being used to represent:

1. A physical reel of tape (stripe color, tape color — used to distinguish reels visually in a bin)
2. A purchase lot (manufacturer, source — where this batch came from)
3. A stock counter (quantity, sigma_adjustment, low_stock_threshold)
4. A bin assignment anchor (via `BinAssignment.footprint_id`)

These are conceptually four different things. In practice, a single component's stock is almost always one footprint. When a user buys a second reel of the same component from a different manufacturer, they should create a second footprint — but there is no UI guidance or enforcement of this. The result in practice: most components have exactly one footprint, and the multi-footprint feature is never used but adds complexity everywhere (every stock operation must select which footprint to target).

The sigma_adjustment calibration offset is especially confusing layered on top of a per-reel concept. When should a user calibrate a reel vs. the component overall?

---

### 20. Kit "quantity required" vs actual component stock are never compared — BOM-style check is missing
**File**: `app/routers/kits.py`

A kit has components with quantities. There is no endpoint or UI that answers: "Can I build N of this kit given current stock?" The `kit_detail` page shows the component list and the quantities needed, but stock numbers for each component are not fetched — the template receives `kit_components_context` (from `main.py:669`) which has `quantity` (kit requirement) but not the component's current stock. A user has to navigate to each component individually to check.

This is the primary use case for a kit system in an electronics lab. Without it, kits are just named lists.

---

### 21. Box grid system has no capacity limit enforcement
**File**: `app/routers/boxes.py` (assign endpoint)

When assigning a component to a cell, the API accepts any `cell_id` string. The box's `cell_count` is stored but never checked during assignment. A `BOXALL48` (48 cells) can accept assignments to `R99C99` without error. The grid UI renders cells based on `cell_count` so the assignment would simply never appear visually — the data exists in `bin_assignments` with no way to surface it.

---

### 22. Inventory events are written but `InventoryEvent` is not used to reconstruct current stock
**File**: `app/routers/components.py:966-1078`

The `inventory_events` table is an append-only ledger (`resulting_raw_quantity`, `resulting_effective_quantity` on each row). But the source of truth for current stock is `footprints.quantity` and `footprints.sigma_adjustment`, not the ledger. If `footprints.quantity` is modified directly (e.g., via the old `PATCH /{barcode_id}/stock` endpoint at line 766, which still exists and modifies `fp.quantity` without writing an event), the ledger and the footprint diverge silently.

There are now two different stock-update code paths:
- `PATCH /{barcode_id}/stock` (line 766): modifies `fp.quantity` directly, writes to InfluxDB, broadcasts WS — **does not write InventoryEvent**
- `POST /{component_id}/inventory-action` (line 967): modifies `fp.quantity`, writes `InventoryEvent`, writes to InfluxDB, broadcasts WS

Both are publicly reachable. The old endpoint is also referenced by the template's "Take/Put/Restock" form on the component detail page. If the UI was ever updated to use `inventory-action` but missed one path, or vice versa, the ledger becomes untrustworthy. Currently the component detail page likely uses one path, the bulk operations use another.

---

### 23. `search_alias` history is unbounded except by a soft cap of 20 entries
**File**: `app/routers/components.py:100-110`

```python
existing.append(alias)
comp.search_alias = " | ".join(existing[:20])
```

Every rename appends the old name as an alias, capped at 20. The cap prevents infinite growth but silently drops old aliases. If a component was bulk-renamed many times (common during AI-assisted normalization), the earliest aliases are lost. Lost aliases break search: a user searching for the component's original name from a paper label will find nothing. The alias list should be treated as a set with priority ordering (most recent first), not silently truncated.

---

## SEVERITY 6 — Missing Constraint / Validation Holes

### 24. `BinAssignment` allows the same cell to be assigned to multiple components
**File**: `app/models/models.py:230-241`, `app/services/migrations.py:91-98`

There is no `UNIQUE(box_id, cell_id)` constraint on `bin_assignments` (even filtered for `active=True`). The `assign` endpoint in `boxes.py` does soft-delete the old assignment before creating a new one, but this is logic-level only. Two concurrent assignments to the same cell will both succeed at the DB level. The grid would then show one (whichever loads first in the ORM query) and silently discard the other.

**Fix**: Add `UNIQUE(box_id, cell_id) WHERE active = TRUE` (PostgreSQL partial index) and let the DB enforce it.

---

### 25. Kit creation requires at least one component (`components` is checked as non-empty)
**File**: `app/routers/kits.py:333-335`

```python
if not kit_data.components:
    raise HTTPException(400, "A kit must include at least one component")
```

But the kits.html page's drag-drop import handler (`kits.html:135-150`) creates kits via `POST /api/kits/` with `components: []` — which will always fail at the API level. The import creates kits with empty component arrays, which is a documented design decision in the prior session, but it conflicts with the API validation. Every drag-drop import of kits silently fails with a 400 response that the frontend doesn't surface clearly.

---

### 26. Component `type_id` is required for creation but `type_path` can exist without it
**File**: `app/routers/components.py:599-601`

```python
if not ctype:
    raise HTTPException(404, "ComponentType not found")
```

The `create_component` route requires a valid `ComponentType` record and uses its first character as the barcode prefix. But `type_path` is stored independently of `type_id` — they can and do diverge. A component can have `type_path = "passives/capacitor/ceramic"` and `type_id` pointing to a "module" type record. The barcode prefix then reflects the type record, not the type path. There is no validation that `type_id.name` is consistent with `type_path`.

The kit component creation path (`kits.py:294-330`) resolves `type_id` from `type_path` using the second path segment as the type name. This works if the type names in `component_types` table exactly match the path segments — but there is no FK linking them, so they can drift.

---

### 27. `ComponentPatchRequest.clear_fields` accepts any string — can null out non-nullable DB columns
**File**: `app/routers/components.py:1267-1276`

```python
for field in clear_fields:
    if hasattr(comp, field):
        setattr(comp, field, None)
```

`clear_fields` is a list of strings with no allowlist. Any field name that exists as a Python attribute on the Component ORM object can be nulled. This includes `barcode_id` (UNIQUE NOT NULL), `name` (NOT NULL), and relationship attributes. Setting `barcode_id = None` would cause a PostgreSQL NOT NULL violation on the next flush, producing an unhandled 500. Setting a relationship attribute to None would silently detach the related object. There should be an allowlist of fields that `clear_fields` is permitted to null.

---

## SEVERITY 5 — Performance / Scalability Issues

### 28. Component list page loads ALL components in one query with no pagination
**File**: `app/main.py:322-371`

The components page query does a full table scan with 4 JOINs (ComponentType, Footprint aggregate, KitComponent), then does a second query for generic child stock. With 2000+ components this produces a page with 2000+ DOM rows. The existing JS search/filter works client-side, so all rows are present in the HTML. At ~3KB per row (with all columns), this is ~6MB of HTML per page load. The filter works instantly because it's already in memory, but the initial load time grows linearly with component count.

---

### 29. `box_grid_page` loads ALL components for the assignment dropdown
**File**: `app/main.py:396`

```python
all_components = (await db.execute(select(Component).order_by(Component.barcode_id))).scalars().all()
```

Every time a box grid is opened, every component in the database is loaded to populate the "assign to cell" dropdown. With 2000 components this dropdown has 2000 options and the template renders them all in a `<select>`. The page weight grows with component count.

---

### 30. `project_detail` loads ALL components for the BOM "add component" dropdown
**File**: `app/main.py:430`

```python
components_all = (await db.execute(select(Component).order_by(Component.barcode_id))).scalars().all()
```

Same pattern as box grid — all components loaded for a dropdown, on every project detail page load.

---

### 31. `_next_kit_barcode` loads all kit barcode IDs to find the max
**File**: `app/routers/kits.py:241-248`

On every kit creation, all kit barcodes are fetched to find the maximum. This is O(N) and involves loading string data for all kits. At 10 kits this is trivial; at 10,000 it's a table scan for one integer. A sequence would be O(1).

---

## SEVERITY 4 — Code Quality / Maintainability

### 32. `main.py` has 690 lines of page routes — should be separated from app setup
**File**: `app/main.py`

Template routes (the `@app.get("/components")`, etc.) are all inline in `main.py`. This file handles app creation, lifespan, WebSocket, static file mounting, API helpers, AND all page routing. Any change to how a page renders requires touching the same file as changes to app bootstrapping. Page routes should live in a separate module or router group (e.g., `routers/pages.py`).

---

### 33. `component_detail` builds 3 different anonymous dynamic classes inline
**File**: `app/main.py:492, 501, 508`

The pattern `type("ClassName", (), {field: value, ...})()` is used three times to create throwaway objects for template rendering. This is unreadable, unsearchable by IDE, and untestable. A named dataclass or NamedTuple would be clearer and would catch attribute errors at definition time.

---

### 34. Duplicate name generation logic in `components.py` and `kits.py`
**Files**: `app/routers/components.py:172-213`, `app/routers/kits.py:171-213`

`_auto_component_name()` in components.py and `_human_auto_name()` in kits.py have identical logic for resistor/capacitor/inductor name generation. They are not the same function but produce the same output. Any fix or enhancement to one must be manually applied to the other.

---

### 35. `import unicodedata` at module-level after function definitions in components.py
**File**: `app/routers/components.py:34`

`import unicodedata` appears at line 34, after several function definitions. While Python doesn't require imports at the top of a file, convention (and linters like ruff/flake8) flag this. More practically, if `_sanitize_field` is called during import (it isn't, but a future refactor might), this creates an ordering issue. Imports belong at the top of the file.

---

### 36. Magic strings for event types are not enumerated
**Files**: `app/routers/components.py:978`, `app/models/models.py:204`

`event_type` is a VARCHAR column with values `"take"`, `"put"`, `"restock"`, `"order"`, `"calibrate"`. These strings appear both in the validation check at line 978 and in the model docstring at line 204, but nowhere as a Python enum or constant. A typo in any caller produces a valid DB row with an unknown event type that no UI can render correctly. Same for `action` in the InventoryActionRequest.

---

### 37. `log` variable used in `delete_component_image` but never defined at module level
**File**: `app/routers/components.py:729`

```python
log.warning(f"Failed to delete image file for {barcode_id}: {e}")
```

There is no `import logging` and no `log = logging.getLogger(...)` in `components.py`. This line will raise `NameError: name 'log' is not defined` if the image file deletion fails. The exception is caught by the outer `try/except Exception as e`, which would then also fail trying to call `log.warning`. The result: a double-exception that surfaces as a 500 error with a confusing traceback, and the image path is not cleared.

---

## SEVERITY 3 — Confusing UX / Silent Failures

### 38. Kit barcode display uses kit UUID as the URL, not the barcode_id
**File**: `app/templates/kits.html:31-32`

```html
<a class="bid" href="/kits/{{ kit.id }}">{{ kit.barcode_id }}</a>
```

The URL uses `kit.id` (UUID) while displaying `kit.barcode_id` (e.g., `K001`). Scanning a kit barcode with a physical scanner would produce `K001` — but the scan handler and WebSocket echo that back, and there is no route at `/kits/K001`. The kit detail route is `/kits/{kit_id}` where `kit_id` is the UUID. If the scan system tries to navigate to `/kits/K001` it will 404. The kit barcode is essentially non-functional for scanning.

---

### 39. `import-new` and `import-modifications` endpoints exist but only `import-new` has a documented UI path
**File**: `app/routers/components.py:443-524`

`/api/components/import-modifications` is a fully implemented endpoint that can update existing components from a CSV. It is not referenced in any template, not documented in any help text, and the only way to invoke it is via direct API call. Meanwhile, the drag-drop import in the UI always hits `import-new` (which skips existing barcodes). A user who exports, edits externally, and tries to re-import their changes will be confused when nothing updates.

---

### 40. Kit creation requires non-empty components, but the kits page AI creation flow can produce zero valid components
**File**: `app/templates/kits.html:100-104`

```js
if (!components.length) {
    status.textContent = 'No kit component lines detected';
    return;
}
```

The AI parse result's `kit_components` is filtered with `.filter(c => c.name)`. If the AI returns components without names (which happens when the input is ambiguous product description text), the array becomes empty and the user sees "No kit component lines detected" — but the input was successfully parsed. The user gets no guidance on what the AI did parse or why it couldn't identify components. There is no fallback to show the raw parse result.

---

## SEVERITY 2 — Cosmetic / Nitpick

### 41. Hardcoded name "Annabella" and initials "AP" in model defaults
**File**: `app/models/models.py:24-26`

```python
name = Column(String, nullable=False, default="Annabella")
initials = Column(String, default="AP")
```

Also appears in migration v1 DDL. These are personal defaults baked into the schema. Any new install pre-populates the profile with a real person's name. Should be empty defaults or a setup wizard.

---

### 42. `orders.html` route fetches two separate lists (orders + order_summaries) that are mostly the same data
**File**: `app/main.py:590-643`

The page fetches `orders` (all PurchaseOrder objects) and `order_summaries` (the same data with supplier name and line counts aggregated). The template likely only uses `order_summaries`. The `orders` list is loaded but may not be used in the template, wasting one query per page load.

---

### 43. `test_kits.py` tests nothing relevant to the actual kits implementation
**File**: `app/tests/test_kits.py`

The test file exists but has minimal coverage. Given that the kit creation flow involves component resolution, auto-naming, footprint creation, and barcode generation across multiple services, any refactor of the kit system has zero automated coverage.

---

## Summary: Priority Repair Order

| Priority | Issue | Impact |
|---|---|---|
| 1 | `log` undefined in `delete_component_image` | 500 error on any image delete attempt |
| 2 | Scan joins without `active=True` on BinAssignment | Wrong location reported to scanner |
| 3 | Stock can go negative silently | Corrupted physical count |
| 4 | Kit barcode TOCTOU race | 500 on concurrent kit creation |
| 5 | Image deleted before DB commit | Permanent image loss on crash |
| 6 | `clear_fields` has no allowlist | Can null barcode_id / name |
| 7 | Kit drag-drop import always 400 | Import feature silently broken |
| 8 | Two stock update code paths, one skips ledger | Inventory ledger unreliable |
| 9 | No "can I build this kit?" stock check | Core kit use case missing |
| 10 | `BinAssignment` no unique constraint on active cell | Concurrent assignment corruption |
