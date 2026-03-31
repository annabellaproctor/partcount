"""
Schema migration system.
Each migration runs in its own transaction.
DDL uses raw asyncpg connection for proper autocommit behavior.
"""
import logging
from app.models.database import engine
from sqlalchemy import text

log = logging.getLogger("migrations")

MIGRATIONS = [
    (1, "initial schema", [
        """CREATE TABLE IF NOT EXISTS schema_versions (
            version INTEGER PRIMARY KEY,
            description VARCHAR NOT NULL,
            applied_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS profiles (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL DEFAULT 'Annabella',
            email VARCHAR UNIQUE,
            initials VARCHAR DEFAULT 'AP',
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS api_keys (
            id VARCHAR PRIMARY KEY,
            key VARCHAR UNIQUE NOT NULL,
            label VARCHAR NOT NULL,
            profile_id VARCHAR REFERENCES profiles(id),
            active BOOLEAN DEFAULT TRUE,
            last_used TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS component_types (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            description TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS manufacturers (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            aliases TEXT,
            url VARCHAR,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS components (
            id VARCHAR PRIMARY KEY,
            barcode_id VARCHAR UNIQUE NOT NULL,
            name VARCHAR NOT NULL,
            value VARCHAR,
            unit VARCHAR,
            package VARCHAR,
            voltage_rating FLOAT,
            tolerance VARCHAR,
            notes TEXT,
            image_path VARCHAR,
            datasheet_url VARCHAR,
            description TEXT,
            mpn VARCHAR,
            digikey_pn VARCHAR,
            lcsc_pn VARCHAR,
            manufacturer_id VARCHAR REFERENCES manufacturers(id),
            type_id VARCHAR REFERENCES component_types(id),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS footprints (
            id VARCHAR PRIMARY KEY,
            component_id VARCHAR REFERENCES components(id),
            manufacturer VARCHAR,
            source VARCHAR,
            stripe_color VARCHAR,
            tape_color VARCHAR,
            quantity INTEGER DEFAULT 0,
            low_stock_threshold INTEGER DEFAULT 10,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS boxes (
            id VARCHAR PRIMARY KEY,
            label VARCHAR UNIQUE NOT NULL,
            model VARCHAR,
            cell_count INTEGER,
            location VARCHAR,
            slot_index INTEGER DEFAULT 0,
            notes TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS bin_assignments (
            id VARCHAR PRIMARY KEY,
            box_id VARCHAR REFERENCES boxes(id),
            cell_id VARCHAR NOT NULL,
            component_id VARCHAR REFERENCES components(id),
            footprint_id VARCHAR REFERENCES footprints(id),
            active BOOLEAN DEFAULT TRUE,
            assigned_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS projects (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            description TEXT,
            status VARCHAR DEFAULT 'active',
            profile_id VARCHAR REFERENCES profiles(id),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS todo_items (
            id VARCHAR PRIMARY KEY,
            project_id VARCHAR REFERENCES projects(id),
            text VARCHAR NOT NULL,
            done BOOLEAN DEFAULT FALSE,
            priority INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS bom_items (
            id VARCHAR PRIMARY KEY,
            project_id VARCHAR REFERENCES projects(id),
            component_id VARCHAR REFERENCES components(id),
            description VARCHAR NOT NULL,
            quantity_needed INTEGER DEFAULT 1,
            quantity_have INTEGER DEFAULT 0,
            notes TEXT,
            sourced BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS suppliers (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            url VARCHAR,
            notes TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS component_suppliers (
            id VARCHAR PRIMARY KEY,
            component_id VARCHAR REFERENCES components(id),
            supplier_id VARCHAR REFERENCES suppliers(id),
            sku VARCHAR,
            mpn VARCHAR,
            digikey_pn VARCHAR,
            lcsc_pn VARCHAR,
            unit_price FLOAT,
            pack_size INTEGER DEFAULT 1,
            url VARCHAR,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS purchase_orders (
            id VARCHAR PRIMARY KEY,
            supplier_id VARCHAR REFERENCES suppliers(id),
            reference VARCHAR,
            status VARCHAR DEFAULT 'pending',
            order_date TIMESTAMP,
            expected_date TIMESTAMP,
            received_date TIMESTAMP,
            total_cost FLOAT,
            order_url VARCHAR,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS purchase_order_items (
            id VARCHAR PRIMARY KEY,
            order_id VARCHAR REFERENCES purchase_orders(id),
            component_id VARCHAR REFERENCES components(id),
            component_supplier_id VARCHAR REFERENCES component_suppliers(id),
            quantity_ordered INTEGER DEFAULT 1,
            quantity_received INTEGER DEFAULT 0,
            unit_price FLOAT,
            notes TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS component_lookups (
            id VARCHAR PRIMARY KEY,
            query VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            result_json TEXT NOT NULL,
            fetched_at TIMESTAMP DEFAULT NOW()
        )""",
    ]),

    (2, "additive columns — safe ALTER IF NOT EXISTS", [
        "ALTER TABLE component_suppliers ADD COLUMN IF NOT EXISTS digikey_pn VARCHAR",
        "ALTER TABLE component_suppliers ADD COLUMN IF NOT EXISTS lcsc_pn VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS manufacturer_id VARCHAR REFERENCES manufacturers(id)",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS mpn VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS digikey_pn VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS lcsc_pn VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS slot_index INTEGER DEFAULT 0",
    ]),

    (3, "system_settings table, component_lookups full_text + unique index", [
        """CREATE TABLE IF NOT EXISTS system_settings (
            key VARCHAR PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        "ALTER TABLE component_lookups ADD COLUMN IF NOT EXISTS full_text TEXT",
        # Unique index on query so ON CONFLICT (query) works for upserts.
        # If index already exists the CREATE INDEX IF NOT EXISTS is a no-op.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_component_lookups_query ON component_lookups (query)",
    ]),

    (4, "generic components — is_generic flag and parent_id self-referential FK", [
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS is_generic BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS parent_id VARCHAR REFERENCES components(id)",
    ]),

    (5, "api usage tracking for rate limit monitoring", [
        """CREATE TABLE IF NOT EXISTS api_usage (
            id VARCHAR PRIMARY KEY,
            api_name VARCHAR NOT NULL,
            endpoint VARCHAR,
            timestamp TIMESTAMP DEFAULT NOW(),
            success BOOLEAN DEFAULT TRUE,
            error_message TEXT,
            response_time_ms INTEGER
        )""",
        "CREATE INDEX IF NOT EXISTS idx_api_usage_api_name ON api_usage (api_name)",
        "CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage (timestamp)",
    ]),

    (6, "kits system + hierarchical component type fields", [
        """CREATE TABLE IF NOT EXISTS kits (
            id VARCHAR PRIMARY KEY,
            barcode_id VARCHAR UNIQUE NOT NULL,
            name VARCHAR NOT NULL,
            description TEXT,
            image_path VARCHAR,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS kit_components (
            id VARCHAR PRIMARY KEY,
            kit_id VARCHAR REFERENCES kits(id) ON DELETE CASCADE,
            component_id VARCHAR REFERENCES components(id) ON DELETE CASCADE,
            quantity INTEGER DEFAULT 1,
            notes TEXT,
            position INTEGER,
            UNIQUE(kit_id, component_id)
        )""",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS type_path VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS type_data JSONB",
        "CREATE INDEX IF NOT EXISTS idx_components_type_path ON components (type_path)",
    ]),

    (7, "component label short titles", [
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS short_title VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS short_title_manual BOOLEAN NOT NULL DEFAULT FALSE",
        "UPDATE components SET short_title = COALESCE(NULLIF(value, ''), name) WHERE short_title IS NULL",
    ]),

    (8, "component searchable aliases for title overwrite", [
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS search_alias TEXT",
    ]),

    (9, "component image generation query", [
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS image_query TEXT",
    ]),

    (10, "component sticker tag number", [
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS sticker_tag_no INTEGER",
    ]),

    (11, "label print profiles and calibration revisions", [
        """CREATE TABLE IF NOT EXISTS label_print_profiles (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            settings JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            calibration_revision INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
    ]),

        (12, "strip hyphens from existing barcode IDs", [
                """UPDATE components c
                     SET barcode_id = REPLACE(c.barcode_id, '-', '')
                     WHERE c.barcode_id LIKE '%-%'
                         AND NOT EXISTS (
                             SELECT 1 FROM components c2
                             WHERE c2.id <> c.id
                                 AND c2.barcode_id = REPLACE(c.barcode_id, '-', '')
                         )""",
                """UPDATE kits k
                     SET barcode_id = REPLACE(k.barcode_id, '-', '')
                     WHERE k.barcode_id LIKE '%-%'
                         AND NOT EXISTS (
                             SELECT 1 FROM kits k2
                             WHERE k2.id <> k.id
                                 AND k2.barcode_id = REPLACE(k.barcode_id, '-', '')
                         )""",
        ]),

                    (13, "barcode print tracking jobs and placement rows", [
                        """CREATE TABLE IF NOT EXISTS barcode_print_jobs (
                            id VARCHAR PRIMARY KEY,
                            sheet_code VARCHAR NOT NULL UNIQUE,
                            profile_id VARCHAR REFERENCES label_print_profiles(id),
                            printed_at TIMESTAMP DEFAULT NOW()
                        )""",
                        """CREATE TABLE IF NOT EXISTS barcode_print_items (
                            id VARCHAR PRIMARY KEY,
                            job_id VARCHAR NOT NULL REFERENCES barcode_print_jobs(id) ON DELETE CASCADE,
                            component_id VARCHAR REFERENCES components(id),
                            barcode_id VARCHAR NOT NULL,
                            sheet_row INTEGER NOT NULL,
                            sheet_col INTEGER NOT NULL,
                            cell_slot INTEGER NOT NULL,
                            box_label VARCHAR,
                            box_row INTEGER,
                            box_col INTEGER,
                            created_at TIMESTAMP DEFAULT NOW()
                        )""",
                        "CREATE INDEX IF NOT EXISTS idx_barcode_print_items_barcode_id ON barcode_print_items (barcode_id)",
                        "CREATE INDEX IF NOT EXISTS idx_barcode_print_items_job_id ON barcode_print_items (job_id)",
                    ]),

        (14, "enforce hyphen-free barcodes and randomize legacy M0001", [
                """UPDATE components c
                     SET barcode_id = REPLACE(c.barcode_id, '-', '')
                     WHERE c.barcode_id LIKE '%-%'
                         AND NOT EXISTS (
                             SELECT 1 FROM components c2
                             WHERE c2.id <> c.id
                                 AND c2.barcode_id = REPLACE(c.barcode_id, '-', '')
                         )""",
                """UPDATE kits k
                     SET barcode_id = REPLACE(k.barcode_id, '-', '')
                     WHERE k.barcode_id LIKE '%-%'
                         AND NOT EXISTS (
                             SELECT 1 FROM kits k2
                             WHERE k2.id <> k.id
                                 AND k2.barcode_id = REPLACE(k.barcode_id, '-', '')
                         )""",
                """UPDATE barcode_print_items b
                     SET barcode_id = REPLACE(b.barcode_id, '-', '')
                     WHERE b.barcode_id LIKE '%-%'""",
                """WITH candidates AS (
                                 SELECT ('M' || UPPER(SUBSTRING(MD5(RANDOM()::text || CLOCK_TIMESTAMP()::text) FROM 1 FOR 4))) AS bid
                                 FROM generate_series(1, 256)
                         ),
                         pick AS (
                                 SELECT c.bid
                                 FROM candidates c
                                 WHERE NOT EXISTS (
                                         SELECT 1 FROM components x WHERE x.barcode_id = c.bid
                                 )
                                 LIMIT 1
                         )
                         UPDATE components t
                         SET barcode_id = (SELECT bid FROM pick)
                         WHERE t.barcode_id = 'M0001'
                             AND EXISTS (SELECT 1 FROM pick)""",
        ]),

                (15, "sheet tracking mode columns for front/barcode parity", [
                    "ALTER TABLE barcode_print_jobs ADD COLUMN IF NOT EXISTS print_mode VARCHAR DEFAULT 'barcode'",
                    "ALTER TABLE barcode_print_items ADD COLUMN IF NOT EXISTS print_mode VARCHAR DEFAULT 'barcode'",
                    "UPDATE barcode_print_jobs SET print_mode = 'barcode' WHERE print_mode IS NULL",
                    "UPDATE barcode_print_items SET print_mode = 'barcode' WHERE print_mode IS NULL",
                ]),
]


async def _get_current_version(conn) -> int:
    try:
        r = await conn.execute(text(
            "SELECT MAX(version) FROM schema_versions"
        ))
        val = r.scalar()
        return val if val is not None else 0
    except Exception:
        return 0


async def _verify_tables(conn) -> dict:
    checks = {}
    for table in ["profiles", "components", "boxes", "component_types", "manufacturers"]:
        try:
            r = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
            checks[table] = r.scalar()
        except Exception as e:
            checks[table] = f"ERR:{e}"
    return checks


async def run_migrations():
    # Each migration runs in its own separate transaction
    async with engine.connect() as conn:
        await conn.execute(text("ROLLBACK"))  # clear any stale txn state

    for version, description, statements in MIGRATIONS:
        # Check current version in a fresh connection each loop
        async with engine.connect() as conn:
            await conn.execute(text("BEGIN"))
            current = await _get_current_version(conn)
            await conn.execute(text("ROLLBACK"))

        if version <= current:
            log.info(f"Migration v{version} already applied, skipping")
            continue

        log.info(f"Applying migration v{version}: {description}")

        # Run each statement in its own autocommit connection
        for sql in statements:
            async with engine.connect() as conn:
                # Use SAVEPOINT so a failed IF NOT EXISTS doesn't kill the block
                try:
                    await conn.execute(text("BEGIN"))
                    await conn.execute(text(sql))
                    await conn.execute(text("COMMIT"))
                except Exception as e:
                    await conn.execute(text("ROLLBACK"))
                    err = str(e)
                    # IF NOT EXISTS errors are non-fatal for ALTER TABLE
                    if "already exists" in err.lower() or "duplicate" in err.lower():
                        log.warning(f"  Skipping (already exists): {sql[:60]}...")
                    else:
                        log.error(f"  FAILED: {sql[:80]}\n  Error: {err}")
                        raise RuntimeError(f"Migration v{version} failed at statement: {sql[:80]}\nError: {err}")

        # Record version
        async with engine.connect() as conn:
            await conn.execute(text("BEGIN"))
            await conn.execute(text(
                "INSERT INTO schema_versions (version, description) "
                "VALUES (:v, :d) ON CONFLICT (version) DO NOTHING"
            ), {"v": version, "d": description})
            checks = await _verify_tables(conn)
            await conn.execute(text("COMMIT"))
            log.info(f"v{version} committed. Table row counts: {checks}")

    log.info("Migrations complete")
