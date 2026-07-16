"""
One-time migration: Copy 5 supplier kits from kits → supplier_kits.

Run: docker compose exec app python -m app.scripts.migrate_supplier_kits
"""
import asyncio
import uuid
from sqlalchemy import select, text
from app.models.database import AsyncSessionLocal
from app.models.models import Kit, KitComponent, Component

# HARDCODED: Only these 5 kits
TARGET_KITS = ['K001', 'K002', 'K004', 'K005', 'K006']


def extract_supplier(name: str, description: str) -> str:
    combined = f"{name} {description or ''}".upper()
    if 'BOJACK' in combined:
        return 'BOJACK'
    elif 'ALLECIN' in combined:
        return 'ALLECIN'
    elif 'ELEGOO' in combined:
        return 'ELEGOO'
    else:
        return 'Unknown'


def new_barcode(old: str) -> str:
    if old.startswith('K') and len(old) >= 2:
        return 'SK' + old[1:]
    raise ValueError(f"Invalid barcode format: {old}")


async def migrate():
    async with AsyncSessionLocal() as db:
        try:
            print("=" * 60)
            print("  Supplier Kits Migration (5 kits only)")
            print("=" * 60)

            # 1. VERIFY ALL 5 KITS EXIST
            print("\n[1/5] Verifying source kits exist...")
            kits = (await db.execute(
                select(Kit).where(Kit.barcode_id.in_(TARGET_KITS))
            )).scalars().all()

            found_ids = {k.barcode_id for k in kits}
            missing = set(TARGET_KITS) - found_ids
            if missing:
                raise ValueError(f"Missing kits: {missing}. Expected all 5: {TARGET_KITS}")
            print(f"  ✓ Found all 5 kits: {sorted(found_ids)}")

            # 2. CHECK FOR BARCODE COLLISIONS
            print("\n[2/5] Checking for barcode collisions...")
            new_barcodes = [new_barcode(bc) for bc in TARGET_KITS]
            existing = (await db.execute(
                text("SELECT barcode_id FROM supplier_kits WHERE barcode_id = ANY(:ids)"),
                {"ids": new_barcodes}
            )).scalars().all()
            if existing:
                raise ValueError(
                    f"Collision: These supplier_kits already exist: {list(existing)}. "
                    f"Delete them first or skip this migration."
                )
            print(f"  ✓ No collisions for: {new_barcodes}")

            # 3. MIGRATE KITS
            print("\n[3/5] Migrating kits...")
            kit_id_map = {}  # old kit.id → new supplier_kit.id

            for kit in sorted(kits, key=lambda k: k.barcode_id):
                supplier = extract_supplier(kit.name, kit.description or '')
                new_id = str(uuid.uuid4())
                new_bc = new_barcode(kit.barcode_id)

                await db.execute(text("""
                    INSERT INTO supplier_kits (
                        id, barcode_id, name, description, notes,
                        supplier, created_at
                    ) VALUES (
                        :id, :barcode_id, :name, :description, :notes,
                        :supplier, :created_at
                    )
                """), {
                    "id": new_id,
                    "barcode_id": new_bc,
                    "name": kit.name,
                    "description": kit.description,
                    "notes": kit.notes,
                    "supplier": supplier,
                    "created_at": kit.created_at,
                })

                kit_id_map[kit.id] = new_id
                print(f"  ✓ {kit.barcode_id} → {new_bc}  (supplier: {supplier})  \"{kit.name}\"")

            # 4. MIGRATE KIT COMPONENTS
            print("\n[4/5] Migrating kit components...")
            total_items = 0

            for kit in sorted(kits, key=lambda k: k.barcode_id):
                new_kit_id = kit_id_map[kit.id]
                kit_comps = (await db.execute(
                    select(KitComponent, Component)
                    .join(Component, Component.id == KitComponent.component_id)
                    .where(KitComponent.kit_id == kit.id)
                    .order_by(KitComponent.position)
                )).all()

                for kc, comp in kit_comps:
                    await db.execute(text("""
                        INSERT INTO supplier_kit_items (
                            id, kit_id, component_id, barcode_id,
                            description, quantity, position,
                            auto_matched, match_confidence, created_at
                        ) VALUES (
                            :id, :kit_id, :component_id, :barcode_id,
                            :description, :quantity, :position,
                            :auto_matched, :match_confidence, NOW()
                        )
                    """), {
                        "id": str(uuid.uuid4()),
                        "kit_id": new_kit_id,
                        "component_id": comp.id,
                        "barcode_id": comp.barcode_id,
                        "description": comp.name,
                        "quantity": kc.quantity or 1,
                        "position": kc.position or 0,
                        "auto_matched": False,
                        "match_confidence": None,
                    })
                    total_items += 1

                new_bc = new_barcode(kit.barcode_id)
                print(f"  ✓ {new_bc}: {len(kit_comps)} item(s)")

            print(f"  ✓ Total migrated: {total_items} component items")

            # 5. VERIFICATION
            print("\n[5/5] Verifying migration...")

            sk_count = (await db.execute(
                text("SELECT COUNT(*) FROM supplier_kits WHERE barcode_id = ANY(:ids)"),
                {"ids": new_barcodes}
            )).scalar()
            if sk_count != 5:
                raise ValueError(f"Expected 5 supplier_kits, found {sk_count}")

            items_count = (await db.execute(text("""
                SELECT COUNT(*) FROM supplier_kit_items
                WHERE kit_id IN (
                    SELECT id FROM supplier_kits WHERE barcode_id = ANY(:ids)
                )
            """), {"ids": new_barcodes})).scalar()
            if items_count != total_items:
                raise ValueError(f"Expected {total_items} items, found {items_count}")

            unlinked = (await db.execute(text("""
                SELECT COUNT(*) FROM supplier_kit_items
                WHERE kit_id IN (
                    SELECT id FROM supplier_kits WHERE barcode_id = ANY(:ids)
                )
                AND component_id IS NULL
            """), {"ids": new_barcodes})).scalar()
            if unlinked > 0:
                print(f"  ⚠ Warning: {unlinked} item(s) have no component_id")

            print(f"  ✓ supplier_kits: {sk_count} rows")
            print(f"  ✓ supplier_kit_items: {items_count} rows")
            print(f"  ✓ All component links preserved ({unlinked} unlinked)")

            # COMMIT
            await db.commit()

            print("\n" + "=" * 60)
            print("✅  Migration complete!")
            print("=" * 60)
            print("\nMigrated kits:")
            for bc in TARGET_KITS:
                print(f"  {bc} → {new_barcode(bc)}")
            print("\nOld kits remain in kits table (not deleted).")
            print("Access supplier kits via Components page UI.")

        except Exception as e:
            print(f"\n💥 Migration failed: {e}")
            await db.rollback()
            raise


if __name__ == "__main__":
    asyncio.run(migrate())
