"""
Clean up orphaned test data from failed test runs.
Run: docker compose exec app python -m app.scripts.cleanup_test_data
"""
import asyncio
from sqlalchemy import text
from app.models.database import AsyncSessionLocal

async def cleanup():
    async with AsyncSessionLocal() as db:
        # Delete in STRICT FK dependency order (deepest children first)
        
        print("Cleaning up test data in FK-safe order...\n")
        
        queries = [
            # LEVEL 5: Deepest children (nothing references these)
            ("bom_allocations", "DELETE FROM bom_allocations WHERE bom_line_id IN (SELECT id FROM bom_lines WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'Test%'))"),
            
            # LEVEL 4: Second-level children
            ("bom_lines", "DELETE FROM bom_lines WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'Test%')"),
            ("kit_lines", "DELETE FROM kit_lines WHERE kit_id IN (SELECT id FROM kits WHERE name LIKE 'Test%')"),
            ("cell_assignments (test lots)", "DELETE FROM cell_assignments WHERE stock_lot_id IN (SELECT id FROM stock_lots WHERE id LIKE '%test%')"),
            ("cell_assignments (test containers)", "DELETE FROM cell_assignments WHERE container_id IN (SELECT id FROM containers WHERE label LIKE 'BOX-TEST%')"),
            
            # LEVEL 3: Third-level children
            ("stock_lots", "DELETE FROM stock_lots WHERE id LIKE '%test%'"),
            
            # LEVEL 2: Fourth-level children
            ("manufacturer_parts", "DELETE FROM manufacturer_parts WHERE id LIKE '%test%'"),
            ("parametric_specs", "DELETE FROM parametric_specs WHERE id LIKE '%test%'"),
            
            # LEVEL 1: Location hierarchy (Container → Fixture → Zone order matters!)
            ("containers", "DELETE FROM containers WHERE label LIKE 'BOX-TEST%'"),
            ("container_types", "DELETE FROM container_types WHERE name LIKE 'TEST%'"),
            ("fixtures", "DELETE FROM fixtures WHERE name LIKE 'Test%' OR name LIKE 'Rack%'"),
            ("zones", "DELETE FROM zones WHERE name LIKE 'Test%'"),
            
            # LEVEL 0: Root taxonomy (only after all parametric_specs are gone)
            ("component_taxonomy", "DELETE FROM component_taxonomy WHERE path LIKE 'passives_%'"),
        ]
        
        total_deleted = 0
        for table, sql in queries:
            try:
                result = await db.execute(text(sql))
                if result.rowcount > 0:
                    print(f"✓ {table:30s} deleted {result.rowcount} rows")
                    total_deleted += result.rowcount
                else:
                    print(f"  {table:30s} (nothing to delete)")
            except Exception as e:
                print(f"✗ {table:30s} ERROR: {str(e)[:80]}")
                raise
        
        await db.commit()
        print(f"\n✓ Cleanup complete: {total_deleted} total rows deleted")
        
        # Verify cleanup
        remaining = (await db.execute(text("SELECT COUNT(*) FROM parametric_specs WHERE id LIKE '%test%'"))).scalar()
        if remaining > 0:
            print(f"⚠ Warning: {remaining} orphaned test specs still remain")
        else:
            print("✓ No orphaned test data remains")

if __name__ == "__main__":
    asyncio.run(cleanup())
