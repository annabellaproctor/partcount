"""
Verifies that all DB-level constraints in the v22 schema actually fire.

Run inside the container:
    docker compose exec app python -m app.scripts.verify_constraints

Each sub-test deliberately inserts bad data, catches the expected
IntegrityError, and prints which constraint triggered.  A pass means the
database rejected the bad row — not that the insert succeeded.
"""
import asyncio
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.database import AsyncSessionLocal
from app.models.new_models import (
    BomAllocation,
    BomLine,
    CellAssignment,
    ComponentTaxonomy,
    Container,
    ContainerType,
    Fixture,
    KitLine,
    Manufacturer2,
    ManufacturerPart,
    ParametricSpec,
    StockLot,
    Zone,
)
from app.models.models import Kit, Profile, Project


def _uid() -> str:
    return str(uuid.uuid4())


def ok(label: str, detail: str = "") -> None:
    suffix = f"  ({detail})" if detail else ""
    print(f"  PASS  {label}{suffix}")


def fail(label: str, reason: str) -> None:
    print(f"  FAIL  {label}  —  {reason}", file=sys.stderr)
    raise AssertionError(label)


# ── fixtures shared across sub-tests ─────────────────────────────────────────

async def _build_base(db, tag: str) -> dict:
    """
    Creates the minimal object graph needed by the constraint tests.
    Returns a dict of all created IDs.
    """
    taxon = ComponentTaxonomy(
        id=_uid(), path=f"vc_passives_{tag}/resistor/fixed",
        name="Fixed Resistor",
    )
    spec = ParametricSpec(
        id=_uid(),
        barcode_id=f"VC{tag.upper()[:6]}",
        component_type_id=taxon.id,
        value="4700", unit="Ω", tolerance="±5%", package="0402",
        name="4.7kΩ ±5% 0402",
    )
    mfr = Manufacturer2(id=_uid(), name=f"TestMfr_{tag}")
    db.add_all([taxon, spec, mfr])
    await db.flush()

    part = ManufacturerPart(
        id=_uid(), mpn="RC0402FR-074K7L",
        manufacturer_id=mfr.id,
        parametric_spec_id=spec.id,
    )
    db.add(part)
    await db.flush()

    lot = StockLot(
        id=_uid(), manufacturer_part_id=part.id,
        quantity=200, sigma_adjustment=0,
    )
    db.add(lot)
    await db.flush()

    zone = Zone(id=_uid(), name=f"VC Zone {tag}")
    fixture = Fixture(id=_uid(), zone_id=zone.id, name="Shelf 1")
    ctype = ContainerType(
        id=_uid(), name=f"VC_CTYPE_{tag}",
        grid_rows=6, grid_cols=6,
    )
    db.add_all([zone, fixture, ctype])
    await db.flush()

    container = Container(
        id=_uid(), label=f"VC-BOX-{tag}",
        container_type_id=ctype.id,
        fixture_id=fixture.id,
        slot_index=0,
    )
    db.add(container)
    await db.flush()

    kit = Kit(id=_uid(), barcode_id=f"KVC{tag[:6].upper()}", name=f"VC Kit {tag}")
    db.add(kit)
    await db.flush()

    # Use existing profile or create stub
    from sqlalchemy import select
    profile = (await db.execute(select(Profile).limit(1))).scalars().first()
    if profile is None:
        profile = Profile(id=_uid(), name="VC Test", email=f"vc_{tag}@test.invalid")
        db.add(profile)
        await db.flush()

    project = Project(id=_uid(), name=f"VC Project {tag}", profile_id=profile.id)
    db.add(project)
    await db.flush()

    await db.commit()

    return dict(
        taxon_id=taxon.id, spec_id=spec.id,
        mfr_id=mfr.id, part_id=part.id, lot_id=lot.id,
        zone_id=zone.id, fixture_id=fixture.id,
        ctype_id=ctype.id, container_id=container.id,
        kit_id=kit.id, project_id=project.id,
        tag=tag,
    )


async def _teardown(db, ids: dict) -> None:
    tag = ids["tag"]
    for sql, param in [
        ("DELETE FROM bom_allocations WHERE stock_lot_id = :id",   ids["lot_id"]),
        ("DELETE FROM bom_lines WHERE project_id = :id",           ids["project_id"]),
        ("DELETE FROM projects WHERE id = :id",                    ids["project_id"]),
        ("DELETE FROM kit_lines WHERE kit_id = :id",               ids["kit_id"]),
        ("DELETE FROM kits WHERE id = :id",                        ids["kit_id"]),
        ("DELETE FROM cell_assignments WHERE container_id = :id",  ids["container_id"]),
        ("DELETE FROM containers WHERE id = :id",                  ids["container_id"]),
        ("DELETE FROM container_types WHERE id = :id",             ids["ctype_id"]),
        ("DELETE FROM fixtures WHERE id = :id",                    ids["fixture_id"]),
        ("DELETE FROM zones WHERE id = :id",                       ids["zone_id"]),
        ("DELETE FROM stock_lots WHERE id = :id",                  ids["lot_id"]),
        ("DELETE FROM manufacturer_parts WHERE id = :id",          ids["part_id"]),
        ("DELETE FROM manufacturers2 WHERE id = :id",              ids["mfr_id"]),
        ("DELETE FROM parametric_specs WHERE id = :id",            ids["spec_id"]),
        ("DELETE FROM component_taxonomy WHERE path LIKE :id",     f"%vc_passives_{tag}%"),
    ]:
        await db.execute(text(sql), {"id": param})
    await db.commit()


# ── individual constraint tests ───────────────────────────────────────────────

async def test_check_kit_line_neither_spec_nor_type(ids: dict) -> None:
    """
    INSERT a kit_line with both FK columns NULL → CHECK constraint fires.
    """
    label = "CHECK ck_kit_lines_has_spec_or_type"
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("""
                INSERT INTO kit_lines (id, kit_id, position, quantity)
                VALUES (:id, :kit_id, 0, 1)
            """), {"id": _uid(), "kit_id": ids["kit_id"]})
            await db.commit()
            fail(label, "INSERT succeeded — constraint missing!")
        except IntegrityError as e:
            err = str(e.orig).lower()
            # PostgreSQL raises: violates check constraint "ck_kit_lines_has_spec_or_type"
            if "ck_kit_lines_has_spec_or_type" in err or "check" in err:
                ok(label, "CHECK constraint fired as expected")
            else:
                fail(label, f"Wrong error: {e.orig}")
        except Exception as e:
            fail(label, f"Unexpected exception: {e}")


async def test_unique_active_cell_assignment(ids: dict) -> None:
    """
    INSERT two active CellAssignments for (container_id, cell_id, stock_lot_id)
    → partial UNIQUE index fires on the second insert.
    """
    label = "UNIQUE uq_cell_assignments_active (container, cell, lot)"
    async with AsyncSessionLocal() as db:
        shared = {"container_id": ids["container_id"], "cell_id": "R2C2", "lot_id": ids["lot_id"]}
        await db.execute(text("""
            INSERT INTO cell_assignments (id, container_id, cell_id, stock_lot_id, quantity, active)
            VALUES (:id, :cid, :cell, :lid, 10, TRUE)
        """), {"id": _uid(), "cid": shared["container_id"],
               "cell": shared["cell_id"], "lid": shared["lot_id"]})
        await db.commit()

    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("""
                INSERT INTO cell_assignments (id, container_id, cell_id, stock_lot_id, quantity, active)
                VALUES (:id, :cid, :cell, :lid, 5, TRUE)
            """), {"id": _uid(), "cid": shared["container_id"],
                   "cell": shared["cell_id"], "lid": shared["lot_id"]})
            await db.commit()
            fail(label, "Duplicate active assignment accepted — constraint missing!")
        except IntegrityError as e:
            err = str(e.orig).lower()
            if "uq_cell_assignments_active" in err or "unique" in err:
                ok(label, "UNIQUE partial index fired as expected")
            else:
                fail(label, f"Wrong error: {e.orig}")
        finally:
            # Clean up the first (valid) row
            async with AsyncSessionLocal() as cleanup:
                await cleanup.execute(text(
                    "DELETE FROM cell_assignments WHERE container_id=:cid AND cell_id='R2C2'"
                ), {"cid": shared["container_id"]})
                await cleanup.commit()


async def test_unique_manufacturer_part(ids: dict) -> None:
    """
    INSERT a ManufacturerPart with the same (manufacturer_id, mpn) as an existing row
    → UNIQUE constraint fires.
    """
    label = "UNIQUE uq_manufacturer_parts_mfr_mpn"
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("""
                INSERT INTO manufacturer_parts (id, mpn, manufacturer_id)
                VALUES (:id, 'RC0402FR-074K7L', :mfr_id)
            """), {"id": _uid(), "mfr_id": ids["mfr_id"]})
            await db.commit()
            fail(label, "Duplicate MPN accepted — constraint missing!")
        except IntegrityError as e:
            err = str(e.orig).lower()
            if "uq_manufacturer_parts_mfr_mpn" in err or "unique" in err:
                ok(label, "UNIQUE constraint fired as expected")
            else:
                fail(label, f"Wrong error: {e.orig}")


async def test_generated_effective_quantity(ids: dict) -> None:
    """
    Create a StockLot with quantity=100, sigma_adjustment=-5.
    Read effective_quantity from DB → must be 95 (server-computed, not Python).
    Then update to quantity=90, sigma_adjustment=10 → effective_quantity must be 100.
    """
    label = "GENERATED effective_quantity = quantity + sigma_adjustment"
    async with AsyncSessionLocal() as db:
        lot_id = _uid()
        await db.execute(text("""
            INSERT INTO stock_lots
                (id, manufacturer_part_id, quantity, sigma_adjustment)
            VALUES (:id, :pid, 100, -5)
        """), {"id": lot_id, "pid": ids["part_id"]})
        await db.commit()

    async with AsyncSessionLocal() as db:
        row = (await db.execute(text(
            "SELECT quantity, sigma_adjustment, effective_quantity FROM stock_lots WHERE id=:id"
        ), {"id": lot_id})).one()

        if row.effective_quantity != 95:
            fail(label, f"expected 95, got {row.effective_quantity}")
        ok(f"{label} (initial: 100 + -5 = 95)")

        # Update and re-check
        await db.execute(text(
            "UPDATE stock_lots SET quantity=90, sigma_adjustment=10 WHERE id=:id"
        ), {"id": lot_id})
        await db.commit()

    async with AsyncSessionLocal() as db:
        row2 = (await db.execute(text(
            "SELECT effective_quantity FROM stock_lots WHERE id=:id"
        ), {"id": lot_id})).one()

        if row2.effective_quantity != 100:
            fail(label, f"after update expected 100, got {row2.effective_quantity}")
        ok(f"{label} (after update: 90 + 10 = 100)")

        await db.execute(text("DELETE FROM stock_lots WHERE id=:id"), {"id": lot_id})
        await db.commit()


# ── main ──────────────────────────────────────────────────────────────────────

async def verify_constraints() -> None:
    tag = uuid.uuid4().hex[:8]

    print("\n[setup] Building base fixtures …")
    async with AsyncSessionLocal() as db:
        ids = await _build_base(db, tag)

    print("\n[1] KitLine CHECK constraint (neither spec nor type) …")
    await test_check_kit_line_neither_spec_nor_type(ids)

    print("\n[2] CellAssignment UNIQUE partial index (active duplicate) …")
    await test_unique_active_cell_assignment(ids)

    print("\n[3] ManufacturerPart UNIQUE (manufacturer_id, mpn) …")
    await test_unique_manufacturer_part(ids)

    print("\n[4] GENERATED ALWAYS AS effective_quantity …")
    await test_generated_effective_quantity(ids)

    print("\n[teardown] Removing test fixtures …")
    async with AsyncSessionLocal() as db:
        await _teardown(db, ids)

    print("\n✓ All constraint checks passed")


if __name__ == "__main__":
    asyncio.run(verify_constraints())
