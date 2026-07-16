"""
Integration test for the v22 multi-dimensional component taxonomy schema.

Run inside the container:
    docker compose exec app python -m app.scripts.test_new_schema

Creates one complete object graph, runs five assertion groups, then tears
down all test data so the script is safe to re-run.
"""
import asyncio
import sys
import uuid

from sqlalchemy import select, func, text, update
from sqlalchemy.orm import selectinload

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
from app.models.models import Kit, Project, Profile


# ── helpers ──────────────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


def check(label: str, condition: bool, got=None) -> None:
    if not condition:
        detail = f"  got: {got!r}" if got is not None else ""
        print(f"  FAIL  {label}{detail}", file=sys.stderr)
        raise AssertionError(label)
    print(f"  ok    {label}")


# ── main test ─────────────────────────────────────────────────────────────────

async def test_new_schema() -> None:
    """
    Builds the full object graph described in the task, queries it five ways,
    then deletes every row it created.
    """
    # Unique suffix keeps test rows from clashing with real data or each other
    # across concurrent runs.
    tag = uuid.uuid4().hex[:8]

    async with AsyncSessionLocal() as db:

        # ── 1. Component taxonomy ─────────────────────────────────────────────
        print("\n[1] Building component taxonomy …")

        root = ComponentTaxonomy(
            id=_uid(), path=f"passives_{tag}",
            name="Passives",
        )
        resistor = ComponentTaxonomy(
            id=_uid(), path=f"passives_{tag}/resistor",
            name="Resistor", parent_id=root.id,
        )
        fixed = ComponentTaxonomy(
            id=_uid(), path=f"passives_{tag}/resistor/fixed",
            name="Fixed Resistor", parent_id=resistor.id,
            spec_schema={"fields": ["value", "tolerance", "power_rating", "package"]},
        )
        db.add_all([root, resistor, fixed])
        await db.flush()

        check("taxonomy root created", root.id is not None)
        check("taxonomy child linked", resistor.parent_id == root.id)

        # ── 2. Parametric spec ────────────────────────────────────────────────
        print("\n[2] Creating parametric spec …")

        spec = ParametricSpec(
            id=_uid(),
            barcode_id=f"R{tag.upper()[:5]}",
            component_type_id=fixed.id,
            value="10000", unit="Ω",
            tolerance="±1%",
            power_rating=0.1,
            package="0603",
            name="10kΩ ±1% 0603 Resistor",
            description="Standard thick-film SMD resistor",
        )
        db.add(spec)
        await db.flush()

        check("parametric spec created", spec.id is not None)
        check("spec links to taxonomy", spec.component_type_id == fixed.id)

        # ── 3. Manufacturer + MPN ─────────────────────────────────────────────
        print("\n[3] Creating manufacturer and MPN …")

        mfr = Manufacturer2(
            id=_uid(), name=f"Yageo_{tag}",
            url="https://www.yageo.com",
        )
        db.add(mfr)
        await db.flush()

        part = ManufacturerPart(
            id=_uid(),
            mpn="RC0603FR-0710KL",
            manufacturer_id=mfr.id,
            parametric_spec_id=spec.id,
            digikey_pn="311-10KHRCT-ND",
            lcsc_pn="C98220",
            lifecycle_status="active",
        )
        db.add(part)
        await db.flush()

        check("manufacturer created", mfr.id is not None)
        check("MPN links to spec", part.parametric_spec_id == spec.id)

        # ── 4. Stock lot ──────────────────────────────────────────────────────
        print("\n[4] Creating stock lot …")

        lot = StockLot(
            id=_uid(),
            manufacturer_part_id=part.id,
            quantity=50,
            sigma_adjustment=0,
            packaging_type="tape_reel",
            tape_color="yellow",
            lot_code=f"LOT-{tag}",
        )
        db.add(lot)
        await db.flush()

        check("stock lot created", lot.id is not None)

        # ── 5. Location hierarchy ─────────────────────────────────────────────
        print("\n[5] Building location hierarchy …")

        zone = Zone(
            id=_uid(), name=f"Test Bench_{tag}",
            description="Integration test zone",
        )
        fixture = Fixture(
            id=_uid(), zone_id=zone.id, name="Rack A",
        )
        ctype = ContainerType(
            id=_uid(), name=f"BOXALL144_{tag}",
            grid_rows=12, grid_cols=12,
            allow_multi_spec=False,
            require_same_package=True,
        )
        container = Container(
            id=_uid(),
            label=f"BOX-TEST-{tag}",
            container_type_id=ctype.id,
            fixture_id=fixture.id,
            slot_index=0,
        )
        db.add_all([zone, fixture, ctype, container])
        await db.flush()

        cell = CellAssignment(
            id=_uid(),
            container_id=container.id,
            cell_id="R1C1",
            stock_lot_id=lot.id,
            quantity=50,
            active=True,
        )
        db.add(cell)
        await db.flush()

        check("zone created", zone.id is not None)
        check("fixture links to zone", fixture.zone_id == zone.id)
        check("container in fixture", container.fixture_id == fixture.id)
        check("cell assignment active", cell.active is True)

        # ── 6. Kit with 2 parametric lines ────────────────────────────────────
        print("\n[6] Creating kit with parametric lines …")

        kit = Kit(
            id=_uid(),
            barcode_id=f"K{tag.upper()[:6]}",
            name=f"Test Kit {tag}",
        )
        db.add(kit)
        await db.flush()

        # Line 1: exact spec match
        kline_exact = KitLine(
            id=_uid(), kit_id=kit.id,
            position=0, quantity=1,
            parametric_spec_id=spec.id,
        )
        # Line 2: parametric range match (any 0603 resistor 1kΩ–100kΩ)
        kline_range = KitLine(
            id=_uid(), kit_id=kit.id,
            position=1, quantity=1,
            component_type_id=fixed.id,
            value_min=1000.0, value_max=100000.0,
            package="0603",
            param_constraints={"power_rating_min": 0.063},
        )
        db.add_all([kline_exact, kline_range])
        await db.flush()

        check("exact kit line has spec_id", kline_exact.parametric_spec_id == spec.id)
        check("range kit line has type_id", kline_range.component_type_id == fixed.id)
        check("range kit line value_min set", kline_range.value_min == 1000.0)

        # ── 7. Project with BOM line + allocation ─────────────────────────────
        print("\n[7] Creating project, BOM line, and allocation …")

        # Reuse the seeded default profile if available, else create a stub
        profile_row = (await db.execute(select(Profile).limit(1))).scalars().first()
        if profile_row is None:
            profile_row = Profile(id=_uid(), name="Test User", email=f"test_{tag}@test.invalid")
            db.add(profile_row)
            await db.flush()

        project = Project(
            id=_uid(), name=f"Test Project {tag}",
            profile_id=profile_row.id,
        )
        db.add(project)
        await db.flush()

        bom_line = BomLine(
            id=_uid(), project_id=project.id,
            ref_des="R1", position=0, quantity=10,
            parametric_spec_id=spec.id,
            notes="Pull-down resistor",
        )
        db.add(bom_line)
        await db.flush()

        allocation = BomAllocation(
            id=_uid(),
            bom_line_id=bom_line.id,
            stock_lot_id=lot.id,
            quantity_allocated=10,
        )
        db.add(allocation)
        await db.flush()

        check("project created", project.id is not None)
        check("BOM line ref_des set", bom_line.ref_des == "R1")
        check("allocation quantity set", allocation.quantity_allocated == 10)

        await db.commit()

        # ── Assertion group 1: total stock for the spec ───────────────────────
        print("\n[Q1] Total stock for parametric spec …")

        row = await db.execute(
            select(func.sum(StockLot.quantity))
            .join(ManufacturerPart, StockLot.manufacturer_part_id == ManufacturerPart.id)
            .where(ManufacturerPart.parametric_spec_id == spec.id)
        )
        total_stock = row.scalar() or 0
        check("total stock == 50", total_stock == 50, got=total_stock)

        # ── Assertion group 2: location path ──────────────────────────────────
        print("\n[Q2] Location path of stock lot …")

        loc_row = await db.execute(
            select(
                Zone.name.label("zone_name"),
                Fixture.name.label("fixture_name"),
                Container.label.label("container_label"),
                CellAssignment.cell_id,
            )
            .select_from(StockLot)
            .join(CellAssignment, CellAssignment.stock_lot_id == StockLot.id)
            .join(Container, Container.id == CellAssignment.container_id)
            .join(Fixture, Fixture.id == Container.fixture_id)
            .join(Zone, Zone.id == Fixture.zone_id)
            .where(StockLot.id == lot.id)
            .where(CellAssignment.active.is_(True))
        )
        loc = loc_row.one()
        check(f"zone is 'Test Bench_{tag}'",    loc.zone_name == f"Test Bench_{tag}",      got=loc.zone_name)
        check("fixture is 'Rack A'",            loc.fixture_name == "Rack A",              got=loc.fixture_name)
        check(f"container is 'BOX-TEST-{tag}'", loc.container_label == f"BOX-TEST-{tag}",  got=loc.container_label)
        check("cell is 'R1C1'",                 loc.cell_id == "R1C1",                     got=loc.cell_id)

        # ── Assertion group 3: kit availability ───────────────────────────────
        print("\n[Q3] Kit availability (exact-spec line) …")

        avail_row = await db.execute(
            select(func.sum(StockLot.quantity))
            .join(ManufacturerPart, StockLot.manufacturer_part_id == ManufacturerPart.id)
            .where(ManufacturerPart.parametric_spec_id == kline_exact.parametric_spec_id)
        )
        available = avail_row.scalar() or 0
        kits_buildable = available // kline_exact.quantity
        check("can build ≥50 kits", kits_buildable >= 50, got=kits_buildable)

        # ── Assertion group 4: BOM allocation ────────────────────────────────
        print("\n[Q4] BOM allocation integrity …")

        alloc_row = await db.execute(
            select(BomAllocation)
            .where(BomAllocation.bom_line_id == bom_line.id)
            .where(BomAllocation.stock_lot_id == lot.id)
        )
        alloc = alloc_row.scalars().one()
        check("allocation links correct lot",  alloc.stock_lot_id == lot.id)
        check("allocated quantity is 10",      alloc.quantity_allocated == 10, got=alloc.quantity_allocated)

        # Verify BOM does not over-allocate (allocated ≤ stock)
        check("allocation ≤ available stock", alloc.quantity_allocated <= total_stock)

        # ── Assertion group 5: GENERATED column ───────────────────────────────
        print("\n[Q5] GENERATED ALWAYS AS effective_quantity …")

        # Update via raw SQL so the generated column recalculates server-side.
        # ORM UPDATE would try to write effective_quantity and hit the GENERATED error.
        await db.execute(
            text("UPDATE stock_lots SET quantity = 45, sigma_adjustment = 5 WHERE id = :id"),
            {"id": lot.id},
        )
        await db.commit()

        refreshed = await db.execute(
            select(StockLot.quantity, StockLot.sigma_adjustment, StockLot.effective_quantity)
            .where(StockLot.id == lot.id)
        )
        r = refreshed.one()
        check("quantity updated to 45",        r.quantity == 45,           got=r.quantity)
        check("sigma_adjustment updated to 5", r.sigma_adjustment == 5,    got=r.sigma_adjustment)
        check("effective_quantity == 50",      r.effective_quantity == 50, got=r.effective_quantity)

        # ── Cleanup ───────────────────────────────────────────────────────────
        print("\n[cleanup] Removing test data …")

        await db.execute(text("DELETE FROM bom_allocations WHERE id = :id"),   {"id": allocation.id})
        await db.execute(text("DELETE FROM bom_lines      WHERE id = :id"),     {"id": bom_line.id})
        await db.execute(text("DELETE FROM projects       WHERE id = :id"),     {"id": project.id})
        await db.execute(text("DELETE FROM kit_lines      WHERE kit_id = :id"), {"id": kit.id})
        await db.execute(text("DELETE FROM kits           WHERE id = :id"),     {"id": kit.id})
        await db.execute(text("DELETE FROM cell_assignments WHERE id = :id"),   {"id": cell.id})
        await db.execute(text("DELETE FROM containers     WHERE id = :id"),     {"id": container.id})
        await db.execute(text("DELETE FROM container_types WHERE id = :id"),    {"id": ctype.id})
        await db.execute(text("DELETE FROM fixtures       WHERE id = :id"),     {"id": fixture.id})
        await db.execute(text("DELETE FROM zones          WHERE id = :id"),     {"id": zone.id})
        await db.execute(text("DELETE FROM stock_lots     WHERE id = :id"),     {"id": lot.id})
        await db.execute(text("DELETE FROM manufacturer_parts WHERE id = :id"), {"id": part.id})
        await db.execute(text("DELETE FROM manufacturers2  WHERE id = :id"),    {"id": mfr.id})
        await db.execute(text("DELETE FROM parametric_specs WHERE id = :id"),   {"id": spec.id})
        await db.execute(
            text("DELETE FROM component_taxonomy WHERE path LIKE :p"),
            {"p": f"%{tag}%"},
        )
        await db.commit()

        print("\n✓ All tests passed")


if __name__ == "__main__":
    asyncio.run(test_new_schema())
