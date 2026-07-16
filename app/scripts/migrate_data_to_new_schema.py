"""
Migrate all components from the old flat schema into the new
ParametricSpec / ManufacturerPart / StockLot hierarchy.

Run inside the container:
    docker compose exec app python -m app.scripts.migrate_data_to_new_schema [--dry-run] [--verbose] [--force]

Flags:
    --dry-run   Build full object graph and validate, then ROLLBACK (no writes).
    --verbose   Log every component as it is migrated.
    --force     Skip the empty-table guard (allows re-run after partial failure).

Safety:
    - Entire migration runs in one transaction. Any error triggers full rollback.
    - SAVEPOINT per component so a single bad row is reported precisely.
    - Validation checks count parity, stock total parity, and FK integrity before commit.

Old schema → new taxonomy path + specs mapping:

  type_path=NULL, unit in (Ω, kΩ, MΩ)   → resistor/fixed/carbon-film
      specs: resistance (converted to Ω), power_rating, tolerance, package
  type_path='passives/resistors'          → resistor/fixed/carbon-film (same)
  type_path='passives/capacitors'         → capacitor/film (tantalum caps)
      specs: capacitance (converted to F), voltage_rating
  type_path='actives' (MPN like 1N*)      → diode/rectifier
      specs: max_forward_current, reverse_voltage (from mpn lookup)
  type_path='actives/diodes'              → diode/rectifier
      specs: max_forward_current, reverse_voltage (parsed from value field "1A 400V")
  type_path=NULL, name contains "LED"     → diode/led
      specs: color, size_mm, lens_type (parsed from name)
  type_path='modules/mcu-module'          → active/ic
      specs: (name as-is, no structured params)
"""
import argparse
import asyncio
import json
import re
import sys
import uuid
from typing import Optional

from sqlalchemy import select, text

from app.models.database import AsyncSessionLocal
from app.models.models import BinAssignment, Box, Component, Footprint, Manufacturer
from app.models.new_models import (
    CellAssignment,
    ComponentTaxonomy,
    Container,
    ContainerType,
    Fixture,
    Manufacturer2,
    ManufacturerPart,
    ParametricSpec,
    StockLot,
    Zone,
)
from app.services.taxonomy_svc import collect_inherited_schema, render_label


# ---------------------------------------------------------------------------
# Taxonomy path mapping: (type_path, discriminator) → new taxonomy full path
# ---------------------------------------------------------------------------

# These are the EXACT paths that exist in the seeded component_taxonomy table.
TAXONOMY_RESISTOR    = "component/electronic/passive/resistor/fixed/carbon-film"
TAXONOMY_CAPACITOR   = "component/electronic/passive/capacitor/film"
TAXONOMY_DIODE_RECT  = "component/electronic/active/diode/rectifier"
TAXONOMY_DIODE_LED   = "component/electronic/active/diode/led"
TAXONOMY_IC          = "component/electronic/active/ic"

# Unit → resistance multiplier (convert to Ohms for storage)
_RESISTANCE_UNIT_MULT = {"Ω": 1.0, "kΩ": 1_000.0, "MΩ": 1_000_000.0}
# Unit → capacitance multiplier (convert to Farads)
_CAPACITANCE_UNIT_MULT = {"F": 1.0, "mF": 1e-3, "uF": 1e-6, "µF": 1e-6,
                           "nF": 1e-9, "pF": 1e-12}


class MigrationError(RuntimeError):
    pass


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Per-run lookup caches
# ---------------------------------------------------------------------------
taxonomy_cache:     dict[str, str] = {}  # taxonomy_path → ComponentTaxonomy.id
manufacturer_cache: dict[str, str] = {}  # old Manufacturer.id → Manufacturer2.id
box_to_container:   dict[str, str] = {}  # old Box.id → Container.id
component_to_spec:  dict[str, str] = {}  # old Component.id → ParametricSpec.id


# ---------------------------------------------------------------------------
# Spec-field conversion helpers
# ---------------------------------------------------------------------------

def _classify_component(comp: Component) -> str:
    """Return the target taxonomy full path for a component."""
    name_lower = (comp.name or "").lower()
    unit = (comp.unit or "").strip()
    type_path = (comp.type_path or "").strip()

    # LEDs (no type_path, name contains "led")
    if "led" in name_lower and not type_path:
        return TAXONOMY_DIODE_LED

    # THT resistors: no type_path, unit is Ω / kΩ / MΩ
    if not type_path and unit in _RESISTANCE_UNIT_MULT:
        return TAXONOMY_RESISTOR

    # Typed resistors
    if type_path == "passives/resistors":
        return TAXONOMY_RESISTOR

    # Typed capacitors
    if type_path == "passives/capacitors":
        return TAXONOMY_CAPACITOR

    # Actives/diodes sub-table (value="1A 400V" style)
    if type_path in ("actives", "actives/diodes"):
        return TAXONOMY_DIODE_RECT

    # MCU module
    if type_path == "modules/mcu-module":
        return TAXONOMY_IC

    # Fallback: generic IC
    return TAXONOMY_IC


def _build_specs(comp: Component, taxonomy_path: str) -> dict:
    """Convert old flat columns to new specs JSONB for the given taxonomy path."""
    name_lower = (comp.name or "").lower()
    unit = (comp.unit or "").strip()
    raw_value = comp.value  # string in old schema

    if taxonomy_path == TAXONOMY_RESISTOR:
        specs: dict = {}
        # Convert value + unit → resistance in Ohms
        if raw_value is not None:
            try:
                mult = _RESISTANCE_UNIT_MULT.get(unit, 1.0)
                specs["resistance"] = float(raw_value) * mult
            except (TypeError, ValueError):
                pass
        if comp.power_rating is not None:
            specs["power_rating"] = float(comp.power_rating)
        else:
            # All known rows are 1/4W THT
            specs["power_rating"] = 0.25
        if comp.tolerance:
            specs["tolerance"] = comp.tolerance
        else:
            specs["tolerance"] = "5%"
        # Package: "Through-hole" → taxonomy uses "TH"
        pkg = (comp.package or "").strip()
        if pkg.lower() in ("through-hole", "th", "through hole", "tht"):
            specs["package"] = "TH"
        elif pkg:
            specs["package"] = pkg
        else:
            specs["package"] = "TH"
        return specs

    if taxonomy_path == TAXONOMY_CAPACITOR:
        specs = {}
        if raw_value is not None:
            try:
                mult = _CAPACITANCE_UNIT_MULT.get(unit, 1e-6)
                specs["capacitance"] = float(raw_value) * mult
            except (TypeError, ValueError):
                pass
        if comp.voltage_rating is not None:
            specs["voltage_rating"] = float(comp.voltage_rating)
        return specs

    if taxonomy_path == TAXONOMY_DIODE_RECT:
        specs = {}
        # Try parsing "value" field: "1A 400V", "200mA 100V", "3A 1000V"
        val_str = (raw_value or "").strip()
        if val_str:
            # current: number + A/mA
            cur_m = re.search(r'(\d+(?:\.\d+)?)\s*(m?A)', val_str, re.IGNORECASE)
            if cur_m:
                amps = float(cur_m.group(1))
                if cur_m.group(2).lower() == "ma":
                    amps /= 1000
                specs["max_forward_current"] = amps
            # voltage: number + V
            v_m = re.search(r'(\d+(?:\.\d+)?)\s*V', val_str, re.IGNORECASE)
            if v_m:
                specs["reverse_voltage"] = float(v_m.group(1))
        # Also pull from current_rating if set
        if not specs.get("max_forward_current") and comp.current_rating is not None:
            specs["max_forward_current"] = float(comp.current_rating)
        return specs

    if taxonomy_path == TAXONOMY_DIODE_LED:
        specs = {}
        # Name: "3mm Red LED (Clear)", "5mm Blue LED (Diffused)"
        name = comp.name or ""
        # Size
        size_m = re.match(r'(\d+)\s*mm', name, re.IGNORECASE)
        if size_m:
            specs["size_mm"] = int(size_m.group(1))
        # Color
        for color in ("red", "green", "blue", "white", "yellow", "amber", "IR", "UV"):
            if color.lower() in name.lower():
                specs["color"] = color.lower()
                break
        # Lens type
        if "(clear)" in name.lower():
            specs["lens_type"] = "clear"
        elif "(diffused)" in name.lower():
            specs["lens_type"] = "diffused"
        return specs

    # IC / fallback — no structured specs
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_taxonomy_id(db, path: str) -> str:
    """Return ComponentTaxonomy.id for the given full path. Raises on miss."""
    if path in taxonomy_cache:
        return taxonomy_cache[path]

    row = (await db.execute(
        select(ComponentTaxonomy.id).where(ComponentTaxonomy.path == path)
    )).scalar_one_or_none()
    if not row:
        raise MigrationError(
            f"Taxonomy path '{path}' not found in component_taxonomy table. "
            f"Verify the taxonomy is fully seeded."
        )
    taxonomy_cache[path] = row
    return row


async def _load_all_taxonomy_nodes(db) -> list[dict]:
    """Return all taxonomy nodes as plain dicts for label rendering."""
    rows = (await db.execute(select(ComponentTaxonomy))).scalars().all()
    return [{"path": r.path, "spec_schema": r.spec_schema or {}} for r in rows]


async def _get_or_create_manufacturer(db, old_mfr_id: Optional[str]) -> str:
    cache_key = old_mfr_id or "__unknown__"
    if cache_key in manufacturer_cache:
        return manufacturer_cache[cache_key]

    if old_mfr_id:
        old_mfr = (await db.execute(
            select(Manufacturer).where(Manufacturer.id == old_mfr_id)
        )).scalar_one_or_none()
    else:
        old_mfr = None

    if old_mfr:
        existing = (await db.execute(
            select(Manufacturer2).where(Manufacturer2.name == old_mfr.name)
        )).scalar_one_or_none()
        if existing:
            manufacturer_cache[cache_key] = existing.id
            return existing.id
        new_mfr = Manufacturer2(id=_uid(), name=old_mfr.name,
                                url=old_mfr.url, notes=old_mfr.notes)
    else:
        existing = (await db.execute(
            select(Manufacturer2).where(Manufacturer2.name == "Unknown")
        )).scalar_one_or_none()
        if existing:
            manufacturer_cache[cache_key] = existing.id
            return existing.id
        new_mfr = Manufacturer2(id=_uid(), name="Unknown",
                                notes="Migrated components with no manufacturer")

    db.add(new_mfr)
    await db.flush()
    manufacturer_cache[cache_key] = new_mfr.id
    return new_mfr.id


async def _get_or_create_container(db, old_box_id: str) -> str:
    if old_box_id in box_to_container:
        return box_to_container[old_box_id]

    old_box = (await db.execute(
        select(Box).where(Box.id == old_box_id)
    )).scalar_one_or_none()
    if not old_box:
        raise MigrationError(f"Box id='{old_box_id}' not found in old schema")

    model_norm = (old_box.model or "BOXALL144").upper().replace(" ", "")
    if "144" in model_norm:
        ct_name = "BOXALL144"
    elif "96" in model_norm:
        ct_name = "BOXALL96"
    elif "48" in model_norm:
        ct_name = "BOXALL48"
    else:
        print(f"  ⚠  Unknown box model '{old_box.model}' → defaulting to BOXALL144")
        ct_name = "BOXALL144"

    ctype = (await db.execute(
        select(ContainerType).where(ContainerType.name == ct_name)
    )).scalar_one_or_none()
    if not ctype:
        raise MigrationError(f"ContainerType '{ct_name}' not found — seed it first.")

    zone = (await db.execute(
        select(Zone).where(Zone.name == "Migrated Storage")
    )).scalar_one_or_none()
    if not zone:
        zone = Zone(id=_uid(), name="Migrated Storage",
                    description="Legacy boxes migrated from old schema", sort_order=0)
        db.add(zone)
        await db.flush()

    fixture_name = (old_box.location or "").strip() or "Legacy Boxes"
    fixture = (await db.execute(
        select(Fixture)
        .where(Fixture.zone_id == zone.id)
        .where(Fixture.name == fixture_name)
    )).scalar_one_or_none()
    if not fixture:
        fixture = Fixture(id=_uid(), zone_id=zone.id, name=fixture_name, sort_order=0)
        db.add(fixture)
        await db.flush()

    container = (await db.execute(
        select(Container).where(Container.label == old_box.label)
    )).scalar_one_or_none()
    if not container:
        container = Container(
            id=_uid(), label=old_box.label,
            container_type_id=ctype.id, fixture_id=fixture.id,
            slot_index=old_box.slot_index or 0,
        )
        db.add(container)
        await db.flush()

    box_to_container[old_box_id] = container.id
    return container.id


# ---------------------------------------------------------------------------
# Core per-component migration
# ---------------------------------------------------------------------------

async def _migrate_component(db, comp: Component, all_nodes: list[dict], verbose: bool) -> str:
    """Migrate one Component row. Returns new ParametricSpec.id."""
    # Idempotency
    existing = (await db.execute(
        select(ParametricSpec.id).where(ParametricSpec.barcode_id == comp.barcode_id)
    )).scalar_one_or_none()
    if existing:
        component_to_spec[comp.id] = existing
        if verbose:
            print(f"  skip  {comp.barcode_id:10s}  (already migrated)")
        return existing

    # Resolve taxonomy
    taxonomy_path = _classify_component(comp)
    taxonomy_id = await _resolve_taxonomy_id(db, taxonomy_path)

    # Build specs dict (new JSONB column, replaces all old scattered columns)
    specs = _build_specs(comp, taxonomy_path)

    # Auto-generate name from label_format + specs
    tax_row = (await db.execute(
        select(ComponentTaxonomy).where(ComponentTaxonomy.id == taxonomy_id)
    )).scalar_one_or_none()
    label_format = tax_row.label_format if tax_row else None
    inherited_schema = collect_inherited_schema(taxonomy_path, all_nodes)
    generated_name = render_label(label_format, specs, inherited_schema)
    # Fall back to original component name if label render fails
    name = generated_name or comp.name or comp.barcode_id

    spec = ParametricSpec(
        id=_uid(),
        barcode_id=comp.barcode_id,
        component_type_id=taxonomy_id,
        specs=specs,
        name=name,
        description=comp.description,
        notes=comp.notes,
        image_path=comp.image_path,
        search_alias=comp.search_alias,
    )
    db.add(spec)
    await db.flush()

    if verbose:
        print(f"  map  {comp.barcode_id:10s}  [{taxonomy_path.split('/')[-1]}]  → '{name}'  specs={specs}")

    # Generic / no-MPN: spec only, no ManufacturerPart or StockLot
    if comp.is_generic or not comp.mpn:
        return spec.id

    # Manufacturer
    mfr_id = await _get_or_create_manufacturer(db, comp.manufacturer_id)

    # ManufacturerPart
    mfr_part = (await db.execute(
        select(ManufacturerPart)
        .where(ManufacturerPart.manufacturer_id == mfr_id)
        .where(ManufacturerPart.mpn == comp.mpn)
    )).scalar_one_or_none()
    if not mfr_part:
        mfr_part = ManufacturerPart(
            id=_uid(), mpn=comp.mpn,
            manufacturer_id=mfr_id, parametric_spec_id=spec.id,
            digikey_pn=comp.digikey_pn, lcsc_pn=comp.lcsc_pn,
            datasheet_url=comp.datasheet_url,
        )
        db.add(mfr_part)
        await db.flush()

    # Footprints → StockLots + CellAssignments
    footprints = (await db.execute(
        select(Footprint).where(Footprint.component_id == comp.id)
    )).scalars().all()

    lot_count = 0
    cell_count = 0
    last_lot_id = None

    for fp in footprints:
        stock_lot = StockLot(
            id=_uid(), manufacturer_part_id=mfr_part.id,
            quantity=fp.quantity or 0,
            sigma_adjustment=fp.sigma_adjustment or 0,
            tape_color=fp.tape_color, stripe_color=fp.stripe_color,
            low_stock_threshold=fp.low_stock_threshold if fp.low_stock_threshold is not None else 10,
        )
        db.add(stock_lot)
        await db.flush()
        last_lot_id = stock_lot.id
        lot_count += 1

        assignments_via_fp = (await db.execute(
            select(BinAssignment)
            .where(BinAssignment.footprint_id == fp.id)
            .where(BinAssignment.active.is_(True))
        )).scalars().all()
        for asgn in assignments_via_fp:
            container_id = await _get_or_create_container(db, asgn.box_id)
            db.add(CellAssignment(
                id=_uid(), container_id=container_id, cell_id=asgn.cell_id,
                stock_lot_id=stock_lot.id, quantity=stock_lot.quantity, active=True,
            ))
            cell_count += 1
        await db.flush()

    # BinAssignments linked via component_id with no footprint
    assignments_via_comp = (await db.execute(
        select(BinAssignment)
        .where(BinAssignment.component_id == comp.id)
        .where(BinAssignment.footprint_id.is_(None))
        .where(BinAssignment.active.is_(True))
    )).scalars().all()

    if assignments_via_comp:
        if not footprints:
            synth_lot = StockLot(
                id=_uid(), manufacturer_part_id=mfr_part.id,
                quantity=0, sigma_adjustment=0, low_stock_threshold=10,
            )
            db.add(synth_lot)
            await db.flush()
            last_lot_id = synth_lot.id
            lot_count += 1
        for asgn in assignments_via_comp:
            container_id = await _get_or_create_container(db, asgn.box_id)
            db.add(CellAssignment(
                id=_uid(), container_id=container_id, cell_id=asgn.cell_id,
                stock_lot_id=last_lot_id, quantity=0, active=True,
            ))
            cell_count += 1
        await db.flush()

    if verbose:
        print(f"         + MPN '{comp.mpn}' + {lot_count} lot(s) + {cell_count} cell(s)")

    return spec.id


# ---------------------------------------------------------------------------
# Prerequisite & container-type seed checks
# ---------------------------------------------------------------------------

async def _verify_prerequisites(db, force: bool) -> None:
    tax_count = (await db.execute(text("SELECT COUNT(*) FROM component_taxonomy"))).scalar()
    if tax_count < 10:
        raise MigrationError(
            f"Taxonomy not seeded: only {tax_count} nodes found. "
            f"Run: docker compose exec app python -m app.scripts.seed_taxonomy"
        )

    if not force:
        spec_count = (await db.execute(text("SELECT COUNT(*) FROM parametric_specs"))).scalar()
        if spec_count > 0:
            raise MigrationError(
                f"parametric_specs already has {spec_count} rows. "
                f"Pass --force to skip this check and re-run idempotently."
            )


async def _ensure_container_types(db) -> None:
    defaults = [
        ("BOXALL144", 12, 12),
        ("BOXALL96",   8, 12),
        ("BOXALL48",   6,  8),
    ]
    for name, rows, cols in defaults:
        existing = (await db.execute(
            select(ContainerType).where(ContainerType.name == name)
        )).scalar_one_or_none()
        if not existing:
            db.add(ContainerType(
                id=_uid(), name=name, grid_rows=rows, grid_cols=cols,
                allow_multi_spec=False, require_same_package=True,
            ))
            print(f"  seeded ContainerType '{name}'")
    await db.flush()


# ---------------------------------------------------------------------------
# Post-migration validation
# ---------------------------------------------------------------------------

async def _validate(db, expected_count: int) -> tuple[bool, dict]:
    report: dict = {}

    old_count = (await db.execute(text("SELECT COUNT(*) FROM components"))).scalar()
    new_count = (await db.execute(text("SELECT COUNT(*) FROM parametric_specs"))).scalar()
    report["old_components"] = old_count
    report["new_specs"] = new_count
    report["count_match"] = new_count == expected_count
    report["expected_new_specs"] = expected_count

    old_stock = (await db.execute(text("""
        SELECT COALESCE(SUM(f.quantity + COALESCE(f.sigma_adjustment, 0)), 0)
        FROM footprints f
        JOIN components c ON c.id = f.component_id
        WHERE c.mpn IS NOT NULL AND c.mpn != ''
    """))).scalar()
    new_stock = (await db.execute(text(
        "SELECT COALESCE(SUM(effective_quantity), 0) FROM stock_lots"
    ))).scalar()
    report["old_stock_total"] = old_stock
    report["new_stock_total"] = new_stock
    report["stock_match"] = old_stock == new_stock

    orphan_cells = (await db.execute(text(
        "SELECT COUNT(*) FROM cell_assignments "
        "WHERE stock_lot_id NOT IN (SELECT id FROM stock_lots)"
    ))).scalar()
    report["orphan_cell_assignments"] = orphan_cells

    broken_specs = (await db.execute(text(
        "SELECT COUNT(*) FROM parametric_specs "
        "WHERE component_type_id NOT IN (SELECT id FROM component_taxonomy)"
    ))).scalar()
    report["broken_taxonomy_fks"] = broken_specs

    broken_parts = (await db.execute(text(
        "SELECT COUNT(*) FROM manufacturer_parts "
        "WHERE parametric_spec_id IS NOT NULL "
        "AND parametric_spec_id NOT IN (SELECT id FROM parametric_specs)"
    ))).scalar()
    report["broken_mfr_part_fks"] = broken_parts

    # Check name generation quality
    null_names = (await db.execute(text(
        "SELECT COUNT(*) FROM parametric_specs WHERE name IS NULL OR name = ''"
    ))).scalar()
    report["null_names"] = null_names

    # Sample: show 5 resistors for manual spot-check
    samples = (await db.execute(text("""
        SELECT barcode_id, name, specs
        FROM parametric_specs
        LIMIT 5
    """))).fetchall()
    report["sample_specs"] = [
        {"barcode_id": r[0], "name": r[1], "specs": r[2]} for r in samples
    ]

    success = (
        report["count_match"]
        and report["stock_match"]
        and orphan_cells == 0
        and broken_specs == 0
        and broken_parts == 0
        and null_names == 0
    )
    return success, report


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def migrate(dry_run: bool = False, verbose: bool = False, force: bool = False) -> dict:
    print(f"\n{'='*60}")
    print(f"  LabInventory data migration to new schema")
    print(f"  dry_run={dry_run}  verbose={verbose}  force={force}")
    print(f"{'='*60}\n")

    async with AsyncSessionLocal() as db:
        print("[0] Seeding container types …")
        await _ensure_container_types(db)
        await db.commit()

        try:
            print("[1] Verifying prerequisites …")
            await _verify_prerequisites(db, force)

            # Load all taxonomy nodes once for label rendering
            all_nodes = await _load_all_taxonomy_nodes(db)

            components = (await db.execute(
                select(Component).order_by(Component.barcode_id)
            )).scalars().all()
            print(f"[2] Migrating {len(components)} components …\n")

            # Pre-pass: print classification summary
            class_counts: dict[str, int] = {}
            for comp in components:
                path = _classify_component(comp)
                class_counts[path] = class_counts.get(path, 0) + 1
            print("  Classification preview:")
            for path, cnt in sorted(class_counts.items()):
                print(f"    {cnt:3d}  →  {path}")
            print()

            expected_count = len(components)

            for i, comp in enumerate(components):
                try:
                    async with db.begin_nested():
                        spec_id = await _migrate_component(db, comp, all_nodes, verbose)
                        component_to_spec[comp.id] = spec_id
                except Exception as exc:
                    raise MigrationError(
                        f"Failed on component barcode_id='{comp.barcode_id}' "
                        f"name='{comp.name}': {exc}"
                    ) from exc

                if (i + 1) % 10 == 0 or (i + 1) == len(components):
                    print(f"  … {i + 1}/{len(components)} processed")

            print(f"\n[3] Validating …")
            success, report = await _validate(db, expected_count)

            print(f"\nValidation report:")
            print(json.dumps(report, indent=2))

            if dry_run:
                print("\n⟳  DRY RUN — rolling back (no data written)")
                await db.rollback()
            elif success:
                print("\n✅  Validation passed — committing")
                await db.commit()
                print("    Migration committed successfully.")
            else:
                print("\n❌  Validation failed — rolling back")
                await db.rollback()
                raise MigrationError("Post-migration validation failed. See report above.")

            return report

        except MigrationError:
            await db.rollback()
            raise
        except Exception as exc:
            await db.rollback()
            raise MigrationError(f"Unexpected error: {exc}") from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate labinventory data to new schema")
    parser.add_argument("--dry-run",  action="store_true", help="Build and validate, then rollback")
    parser.add_argument("--verbose",  action="store_true", help="Log every component")
    parser.add_argument("--force",    action="store_true", help="Skip non-empty table guard")
    args = parser.parse_args()

    try:
        asyncio.run(migrate(dry_run=args.dry_run, verbose=args.verbose, force=args.force))
    except MigrationError as e:
        print(f"\n💥  MIGRATION FAILED: {e}", file=sys.stderr)
        sys.exit(1)
