# LAB::INV — Applied Fixes (2026-05-08)

This document records every code change made in the post-audit repair session.
Format is designed for LLM context ingestion: each entry names the exact file,
the line range affected (pre-change), the root cause, and the new behavior.

---

## FIX-01 — Undefined logger in components.py (was: NameError on image delete)

**Severity**: 9 (500 error on any failed image deletion)
**File**: `app/routers/components.py`
**Root cause**: `delete_component_image` called `log.warning(...)` but no `log` variable
was defined at module level. Any exception during `os.remove()` triggered a secondary
`NameError: name 'log' is not defined`, masking the original error and producing a
confusing 500.

**Change**:
- Added `import logging` and `import unicodedata` to top-of-file imports (consolidating
  the previously mid-file `import unicodedata` at old line 34).
- Added `log = logging.getLogger(__name__)` after imports.
- Removed the duplicate `import unicodedata` that was previously inline before
  `_sanitize_field`.

**New behavior**: Logger is defined at module load. File deletion failures produce a
`WARNING` log entry and do not abort the response.

---

## FIX-02 — scan_component joined BinAssignment without active=True (wrong location)

**Severity**: 9 (scanner reports wrong box/cell after any component move)
**File**: `app/routers/components.py`, `scan_component` endpoint
**Root cause**: The outer join on `BinAssignment` had no filter on `active`. After a
component is moved to a new cell, the old (soft-deleted, `active=False`) row stays in
`bin_assignments`. PostgreSQL could return either row from `result.first()`. In
practice the stale row was often returned, reporting the wrong location to the user
and writing incorrect box/cell data to InfluxDB.

**Change**:
```python
# Before
.join(BinAssignment, BinAssignment.component_id == Component.id, isouter=True)

# After
.join(
    BinAssignment,
    (BinAssignment.component_id == Component.id) & (BinAssignment.active == True),
    isouter=True,
)
```

**New behavior**: Only the current active assignment is joined. A component with no
active assignment returns `bin_assign = None` (box/cell show as "unknown").

---

## FIX-03 — WebSocket ACK used raw f-string (JSON injection risk)

**Severity**: 8 (malformed JSON if barcode contains quote or backslash)
**File**: `app/main.py`, `websocket_endpoint`
**Root cause**: The scan acknowledgment was built as:
```python
f'{{"event":"scan_ack","data":{{"barcode_id":"{barcode_id}"}}}}'
```
If `barcode_id` contained `"` or `\`, the output was invalid JSON. Generated barcodes
use a safe charset, but external HID scanners can send arbitrary strings.

**Change**: Replaced f-string with `json.dumps()`:
```python
await websocket.send_text(json.dumps({
    "event": "scan_ack",
    "data": {"barcode_id": barcode_id},
}))
```

**New behavior**: ACK payload is always valid JSON regardless of barcode content.

---

## FIX-04 — Stock could go negative (silent inventory corruption)

**Severity**: 10 (physical count silently diverges from DB)
**File**: `app/routers/components.py`, `inventory_action` endpoint, `take` branch
**Root cause**: No floor at zero. `fp.quantity = raw_before - qty` with no guard.
Taking 50 from a bin with 10 stored `-40`. The effective quantity (raw + sigma_adjustment)
could still be positive if sigma was large, completely hiding the problem.

**Change**: Added pre-check before mutating quantity:
```python
if action == "take":
    if raw_before - qty < 0:
        raise HTTPException(
            400,
            f"Insufficient stock: have {raw_before}, cannot take {qty}. "
            "Use 'calibrate' to correct the count if the physical quantity differs.",
        )
    fp.quantity = raw_before - qty
```

**New behavior**: `take` returns HTTP 400 if quantity would go negative. The error
message instructs the user to use `calibrate` if the physical count differs from the
DB (which is the correct recovery path). `calibrate` still allows any value including
negative effective quantities, since calibration is an audit operation.

---

## FIX-05 — Image deleted from disk before DB commit (permanent loss on crash)

**Severity**: 8 (image file gone, DB still points to it)
**File**: `app/routers/components.py`, `delete_component_image` endpoint
**Root cause**: `os.remove()` was called before `db.commit()`. A crash between those
two operations left the file deleted but `comp.image_path` still set in the DB.
Every subsequent component detail page load produced a broken image tag.

Additionally, the endpoint had an explicit `await db.commit()` — the only route in the
codebase that did so — which was inconsistent with `get_db()`'s auto-commit pattern.

**Change**:
1. Clear `comp.image_path = None` and call `db.flush()` first.
2. Remove the explicit `await db.commit()` — let `get_db()` commit on exit as every
   other route does.
3. Delete the physical file after the flush (post-commit if get_db exits cleanly).
4. Broadcast the WS event before the file deletion (it's a DB-level fact at that point).

**New behavior**: If the process crashes between flush and file deletion, the DB has
`image_path = None` (correct) and the old file still exists on disk (stale orphan,
recoverable by manual cleanup — not a broken pointer). This is the safer failure mode.

---

## FIX-06 — autocrop_image overwrote source in-place (permanent loss on partial write)

**Severity**: 10 (image file corrupted/lost on disk-full or Pillow error)
**File**: `app/services/barcode_svc.py`, `autocrop_image`
**Root cause**: Function opened `src_path`, cropped, and saved directly back to the
same path. If `img.save()` failed midway (disk full, encode error), the original was
partially overwritten with no recovery path. Also: if `getbbox()` returned `None`
(fully transparent image), the save still ran, producing valid but uncropped output —
that's fine — but a zero-byte save would be silently accepted.

**Change**: Write to `dest_path + ".tmp"` first, verify size > 0, then `os.replace()`
atomically over the destination:
```python
tmp_path = dest_path + ".tmp"
try:
    img = Image.open(src_path).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    img.save(tmp_path)
    if os.path.getsize(tmp_path) == 0:
        raise RuntimeError("autocrop produced a zero-byte file")
    os.replace(tmp_path, dest_path)
except Exception:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
    raise
```

**New behavior**: `os.replace()` is atomic on POSIX. Either the full new file lands or
the original is untouched. A failure cleans up the temp file and re-raises, allowing
callers to handle the error.

---

## FIX-07 — clear_fields had no allowlist (could null barcode_id/name)

**Severity**: 6 (500 on DB NOT NULL violation, or silent relationship detach)
**File**: `app/routers/components.py`, `patch_component` endpoint
**Root cause**: `clear_fields` accepted any string and did `setattr(comp, field, None)`
for any attribute name that existed on the Component ORM object. This included:
- `barcode_id` — UNIQUE NOT NULL → DB constraint violation 500
- `name` — NOT NULL → DB constraint violation 500
- `component_type`, `footprints`, etc. — SQLAlchemy relationship attributes →
  silent detach of related objects

**Change**: Added explicit allowlist `_CLEARABLE_FIELDS` containing only fields that
are nullable in the schema:
```python
_CLEARABLE_FIELDS = {
    "value", "unit", "package", "tolerance", "voltage_rating", "current_rating",
    "power_rating", "notes", "datasheet_url", "mpn", "digikey_pn", "lcsc_pn",
    "description", "short_title", "search_alias", "image_path", "image_query",
    "sticker_tag_no", "type_path", "type_data", "manufacturer_id", "parent_id",
}
```
`type_data.{key}` dot-notation paths bypass the allowlist check (intentional —
they only touch a JSON sub-key, not a column).

**New behavior**: Requests with `clear_fields: ["barcode_id"]` or `["name"]` are
silently ignored for those fields (not an error — the allowed fields are still
processed). The `barcode_id` and `name` columns are never nulled via this path.

---

## FIX-08 — Kit drag-drop import always returned 400 (empty components rejected)

**Severity**: 7 (import feature silently broken for kits)
**File**: `app/routers/kits.py`, `create_kit` endpoint and `KitCreate` model
**Root cause**: The kits.html `inv:import` event handler creates kits with
`components: []` (the import only has name/description columns). The API required
`len(components) >= 1` and returned HTTP 400 "A kit must include at least one
component". Every drag-drop import of kits silently failed.

A kit skeleton with no components is a valid state — the user adds components later
via the kit detail page or AI parse.

**Change**:
1. Removed the `if not kit_data.components: raise HTTPException(400, ...)` check.
2. Changed `KitCreate.components` default from required to `= []`.

**New behavior**: `POST /api/kits/` accepts `{"name": "...", "components": []}` and
creates a kit with zero components. Component addition is handled by the PATCH endpoint.

---

## FIX-09 — Kit barcode had TOCTOU race + 999-kit limit

**Severity**: 10 (concurrent creates produce duplicate barcode → 500)
**File**: `app/routers/kits.py`, `_next_kit_barcode`
**Root cause**: Previous implementation:
```python
rows = (await db.execute(select(Kit.barcode_id))).scalars().all()
max_num = 0
for bid in rows:
    m = re.match(r"^K(\d+)$", bid or "")
    if m:
        max_num = max(max_num, int(m.group(1)))
return f"K{max_num + 1:03d}"
```
Two concurrent create requests both read the same max and both return `K005` (or
whatever). One succeeds; the other gets a PostgreSQL unique constraint violation (500,
no retry). Also: zero-padding is 3 digits, so kit #1000 produces `K1000` which the
regex no longer matches, causing future max lookups to return 0 and collide with `K001`.

**Change**: Replaced with the same `next_barcode_id("K", existing_barcodes)` used by
components — random 4-character alphanumeric suffix, 1000-attempt collision check:
```python
async def _next_kit_barcode(db: AsyncSession) -> str:
    existing = (await db.execute(select(Kit.barcode_id))).scalars().all()
    return next_barcode_id("K", list(existing))
```

**New behavior**: Kit barcodes are now `K` + 4 random chars (e.g., `K7X2M`). Existing
`K001`-style barcodes are unaffected (no migration needed). Concurrent creates are still
not guaranteed collision-free at the read level, but the random space (31^4 ≈ 923,521
possibilities) makes collision astronomically unlikely, and the DB unique constraint
catches any that do collide.

**Note**: The `re` import in kits.py is still used by `_MPN_STYLE_RE` — not removed.

---

## FIX-10 — Old stock PATCH endpoint skipped InventoryEvent ledger

**Severity**: 7 (ledger and footprint quantity diverge silently)
**File**: `app/routers/components.py`, `PATCH /{barcode_id}/stock` endpoint
**Root cause**: Two stock update paths existed:
- `PATCH /{barcode_id}/stock` (old) — modifies `fp.quantity` directly, no `InventoryEvent`
- `POST /{component_id}/inventory-action` (new) — modifies `fp.quantity` + writes event

The old path is still reached by external callers and possibly by UI code. Any delta
applied through it was invisible to the inventory ledger.

**Change**: Added `InventoryEvent` creation to the old endpoint:
```python
ev = InventoryEvent(
    event_type="put" if delta >= 0 else "take",
    quantity_input=abs(delta),
    quantity_change=delta,
    ...
)
db.add(ev)
```

**New behavior**: Both code paths now write to `inventory_events`. The ledger is now
consistent regardless of which endpoint is called. The old endpoint is not removed —
it's a stable API surface.

---

## FIX-11 — Kit availability check was missing (core kit use case)

**Severity**: 7 (primary reason to have kits — "can I build N?" — was absent)
**File**: `app/routers/kits.py`
**Root cause**: No endpoint existed to compare kit component requirements against
current stock. Users had to navigate to each component individually.

**Change**: Added `GET /api/kits/{kit_id}/availability?quantity=1` endpoint:
- Fetches all `KitComponent` rows for the kit.
- Queries aggregate effective stock (`quantity + sigma_adjustment`) per component
  in a single grouped query.
- Returns per-component breakdown: `need_per_kit`, `need_total`, `have`, `can_build`,
  `shortage`, `ok`.
- Top-level `can_build`: `min(have // need_per_kit)` across all components.
- Top-level `missing`: filtered list of components where `ok == False`.

**Response shape**:
```json
{
  "kit_id": "...",
  "kit_name": "Sensor Kit v2",
  "want_to_build": 3,
  "can_build": 1,
  "components": [
    { "barcode_id": "R7X2M", "name": "10kΩ Resistor", "need_per_kit": 4,
      "need_total": 12, "have": 50, "can_build": 12, "shortage": 0, "ok": true },
    { "barcode_id": "C3PQR", "name": "100nF Cap", "need_per_kit": 2,
      "need_total": 6, "have": 2, "can_build": 1, "shortage": 4, "ok": false }
  ],
  "missing": [ ... ]
}
```

---

## FIX-12 — No DB-level uniqueness on active bin cell assignments

**Severity**: 6 (concurrent assignment of same cell produces inconsistent state)
**File**: `app/services/migrations.py`
**Root cause**: `bin_assignments` had no `UNIQUE` constraint. The assign endpoint
soft-deletes the previous active row before inserting, but concurrent requests
bypassing that sequence could create two active assignments for the same cell.
The grid would show one and silently ignore the other.

**Change**: Added migration v21:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_bin_assignments_active_cell
ON bin_assignments (box_id, cell_id)
WHERE active = TRUE
```

This is a PostgreSQL partial unique index — it only enforces uniqueness among rows
where `active = TRUE`. Soft-deleted rows (`active = FALSE`) are excluded from the
constraint, so historical data is preserved.

**New behavior**: A second concurrent active assignment to the same cell gets a
PostgreSQL unique constraint violation rather than silently succeeding. The route-level
soft-delete + insert sequence still works correctly because the DELETE happens before
the INSERT within the same transaction.

---

## FIX-13 — Bulk import loops had no per-row transaction isolation (session poisoning)

**Severity**: 10 (a bad row in import silently aborts all subsequent rows in same batch)
**File**: `app/routers/components.py`, `import_components_new` and `import_components_modifications`
**Root cause**: Both import loops caught `Exception` per-row, but did not use SAVEPOINTs.
In PostgreSQL, once a statement raises an error inside a transaction, the entire
transaction is in an aborted state — no further SQL can execute until a `ROLLBACK` or
`ROLLBACK TO SAVEPOINT`. SQLAlchemy's `AsyncSession` does not auto-rollback on a caught
exception inside a loop. Result: row 3 of 100 fails with a duplicate `barcode_id` constraint
violation → session is now in error state → rows 4–100 all fail with
`InternalError: current transaction is aborted` even if they are perfectly valid data.
The caller receives `{"created": 3, "errors": [97 errors]}` — only 3 rows imported from 100.

**Change**: Wrapped each row's DB operations in `async with db.begin_nested():` (SQLAlchemy
savepoint). On failure, SQLAlchemy rolls back to the savepoint automatically, leaving the
session clean for the next row. Rows that succeed are held in the outer transaction and
committed by `get_db()` on exit.

Also removed the `await db.flush()` at the end of each loop — `begin_nested()` flushes
as it commits each savepoint, making the outer flush redundant.

```python
# Before (broken — one bad row aborts all subsequent rows)
for idx, r in enumerate(rows, start=2):
    try:
        db.add(comp)
        created += 1
    except Exception as e:
        errors.append(...)
await db.flush()

# After (correct — each row is independently isolated)
for idx, r in enumerate(rows, start=2):
    try:
        async with db.begin_nested():   # SAVEPOINT
            db.add(comp)
            created += 1
    except Exception as e:
        errors.append(...)              # savepoint auto-rolled back on exception
# no explicit flush needed
```

**New behavior**: Each row is independently committed to a savepoint. A constraint
violation on row 47 does not affect rows 48–100. The final result accurately reflects
which rows succeeded and which failed.

**Note**: `bulk_delete` and `bulk_merge` were intentionally NOT given per-item savepoints —
those operations are meant to be all-or-nothing. A failure in bulk delete should abort
the entire batch rather than partially deleting some components and not others.

---

## Summary of Files Modified

| File | Changes |
|---|---|
| `app/routers/components.py` | FIX-01, FIX-02, FIX-04, FIX-05, FIX-07, FIX-10, FIX-13 |
| `app/main.py` | FIX-03 |
| `app/services/barcode_svc.py` | FIX-06 |
| `app/routers/kits.py` | FIX-08, FIX-09, FIX-11 |
| `app/services/migrations.py` | FIX-12 (v21 added) |

## Architectural Issues — Deferred to Phase 2

See `ARCHITECTURE_PHASE2.md` for full design specs on each item.

| Issue | Phase | Notes |
|---|---|---|
| Footprint → StockLot rename (semantic overload) | 2A | Schema rename + new columns; no data loss |
| Generic/variant display fix (Option A) | 2B | Display labels only; no schema change |
| Location hierarchy (Zone → Fixture → Container) | 2C | New tables alongside existing boxes |
| Parametric BOM matching | 3A | Requires value normalization first |
| Audit/maintenance subsystem | 3B | New tables + workflow |
| Value field normalization (VARCHAR → FLOAT) | 3C | Prerequisite for parametric matching |
| Component list / box grid full-table loads | Ongoing | Pagination; add as needed |
| Two export endpoints divergence | Cosmetic | Extract shared helper |
| Dynamic type() class construction in main.py | Cosmetic | Replace with dataclasses |
| Duplicate name generation (components vs kits) | Cosmetic | Extract shared function |
| Inventory event_type as magic strings | Cosmetic | Add Enum; low breakage risk |
