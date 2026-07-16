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

    (16, "inventory ledger + sigma calibration offsets", [
        "ALTER TABLE footprints ADD COLUMN IF NOT EXISTS sigma_adjustment INTEGER NOT NULL DEFAULT 0",
        "UPDATE footprints SET sigma_adjustment = 0 WHERE sigma_adjustment IS NULL",
        """CREATE TABLE IF NOT EXISTS inventory_events (
            id VARCHAR PRIMARY KEY,
            component_id VARCHAR NOT NULL REFERENCES components(id),
            footprint_id VARCHAR REFERENCES footprints(id),
            event_type VARCHAR NOT NULL,
            quantity_input INTEGER NOT NULL DEFAULT 0,
            quantity_change INTEGER NOT NULL DEFAULT 0,
            sigma_change INTEGER NOT NULL DEFAULT 0,
            resulting_raw_quantity INTEGER NOT NULL DEFAULT 0,
            resulting_effective_quantity INTEGER NOT NULL DEFAULT 0,
            reference_id VARCHAR,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_inventory_events_component_created ON inventory_events (component_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_inventory_events_footprint_created ON inventory_events (footprint_id, created_at DESC)",
    ]),

    (17, "component current and power rating fields", [
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS current_rating FLOAT",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS power_rating FLOAT",
    ]),

    (18, "ai sources and compacted context cache", [
        """CREATE TABLE IF NOT EXISTS ai_sources (
            id VARCHAR PRIMARY KEY,
            source_kind VARCHAR NOT NULL DEFAULT 'paste',
            title VARCHAR NOT NULL,
            mime_type VARCHAR,
            source_hash VARCHAR UNIQUE,
            storage_path VARCHAR,
            raw_text TEXT,
            extracted_text TEXT,
            summary TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ai_sources_source_kind ON ai_sources (source_kind)",
        """CREATE TABLE IF NOT EXISTS ai_context_cache (
            id VARCHAR PRIMARY KEY,
            cache_key VARCHAR NOT NULL UNIQUE,
            scope VARCHAR NOT NULL,
            context_json TEXT NOT NULL,
            summary TEXT,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ai_context_cache_scope ON ai_context_cache (scope)",
    ]),

    (19, "external catalog item cache for partner-friendly ranked search", [
        """CREATE TABLE IF NOT EXISTS external_catalog_items (
            id VARCHAR PRIMARY KEY,
            item_key VARCHAR NOT NULL UNIQUE,
            source VARCHAR NOT NULL,
            source_item_id VARCHAR,
            mpn VARCHAR,
            manufacturer VARCHAR,
            name VARCHAR,
            description TEXT,
            package VARCHAR,
            datasheet_url VARCHAR,
            image_url VARCHAR,
            product_url VARCHAR,
            search_text TEXT,
            payload_json TEXT,
            retention_policy VARCHAR DEFAULT 'full',
            importance_score FLOAT DEFAULT 0,
            first_seen TIMESTAMP DEFAULT NOW(),
            last_seen TIMESTAMP DEFAULT NOW(),
            seen_count INTEGER DEFAULT 1
        )""",
        "CREATE INDEX IF NOT EXISTS idx_external_catalog_items_source ON external_catalog_items (source)",
        "CREATE INDEX IF NOT EXISTS idx_external_catalog_items_last_seen ON external_catalog_items (last_seen DESC)",
        "CREATE INDEX IF NOT EXISTS idx_external_catalog_items_mpn ON external_catalog_items (mpn)",
    ]),
    # ------------------------------------------------------------------
    # v20-v24 were applied directly to the production database on 2026-05-08
    # and their source was never committed (see CHANGES.md). Reconstructed
    # here from the live schema so a fresh deploy reproduces production.
    # Every statement is idempotent: on the existing database these are no-ops.
    # ------------------------------------------------------------------
    (20, "chat sessions and messages for persistent assistant history", [
        """CREATE TABLE IF NOT EXISTS chat_sessions (
            id VARCHAR PRIMARY KEY,
            title VARCHAR DEFAULT 'New conversation'::character varying NOT NULL,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS chat_messages (
            id VARCHAR PRIMARY KEY,
            session_id VARCHAR NOT NULL,
            role VARCHAR NOT NULL,
            content TEXT DEFAULT ''::text NOT NULL,
            steps_json TEXT,
            created_at TIMESTAMP DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions USING btree (updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages USING btree (session_id, created_at)",
    ]),
    (21, "unique partial index: one active assignment per box cell", [
        # A grid cell holds exactly one component. Two active rows for the same
        # cell made the grid show one and silently ignore the other. Soft-deleted
        # rows (active = FALSE) are excluded so history is preserved.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_bin_assignments_active_cell "
        "ON bin_assignments (box_id, cell_id) WHERE active = TRUE",
    ]),
    (22, "multi-dimensional component taxonomy: specs, MPNs, stock lots, location hierarchy, parametric kits/BOMs", [
        """CREATE TABLE IF NOT EXISTS component_taxonomy (
            id VARCHAR PRIMARY KEY,
            path TEXT NOT NULL,
            name VARCHAR NOT NULL,
            parent_id VARCHAR,
            spec_schema JSONB,
            level INTEGER,
            label_format TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS parametric_specs (
            id VARCHAR PRIMARY KEY,
            barcode_id TEXT NOT NULL,
            component_type_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description TEXT,
            notes TEXT,
            image_path VARCHAR,
            search_alias TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now(),
            specs JSONB
        )""",
        """CREATE TABLE IF NOT EXISTS manufacturer_parts (
            id VARCHAR PRIMARY KEY,
            mpn TEXT NOT NULL,
            manufacturer_id VARCHAR NOT NULL,
            parametric_spec_id VARCHAR,
            digikey_pn VARCHAR,
            mouser_pn VARCHAR,
            lcsc_pn VARCHAR,
            datasheet_url VARCHAR,
            lifecycle_status VARCHAR DEFAULT 'active'::character varying,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS stock_lots (
            id VARCHAR PRIMARY KEY,
            manufacturer_part_id VARCHAR NOT NULL,
            quantity INTEGER DEFAULT 0 NOT NULL,
            sigma_adjustment INTEGER DEFAULT 0 NOT NULL,
            effective_quantity INTEGER,
            acquired_date TIMESTAMP,
            purchase_order_id VARCHAR,
            unit_cost FLOAT,
            lot_code VARCHAR,
            packaging_type VARCHAR,
            tape_color VARCHAR,
            stripe_color VARCHAR,
            low_stock_threshold INTEGER DEFAULT 10,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS container_types (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            grid_rows INTEGER NOT NULL,
            grid_cols INTEGER NOT NULL,
            cell_volume_mm3 FLOAT,
            allow_multi_spec BOOLEAN DEFAULT false NOT NULL,
            allow_value_range_pct FLOAT,
            require_same_package BOOLEAN DEFAULT true NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS containers (
            id VARCHAR PRIMARY KEY,
            label VARCHAR NOT NULL,
            container_type_id VARCHAR NOT NULL,
            fixture_id VARCHAR NOT NULL,
            slot_index INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS zones (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            description TEXT,
            sort_order INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS fixtures (
            id VARCHAR PRIMARY KEY,
            zone_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            description TEXT,
            sort_order INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS bom_lines (
            id VARCHAR PRIMARY KEY,
            project_id VARCHAR NOT NULL,
            ref_des TEXT,
            position INTEGER DEFAULT 0,
            quantity INTEGER DEFAULT 1 NOT NULL,
            notes TEXT,
            parametric_spec_id VARCHAR,
            component_type_id VARCHAR,
            value_min FLOAT,
            value_max FLOAT,
            tolerance_max VARCHAR,
            voltage_min FLOAT,
            package VARCHAR,
            param_constraints JSONB,
            created_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS bom_allocations (
            id VARCHAR PRIMARY KEY,
            bom_line_id VARCHAR NOT NULL,
            stock_lot_id VARCHAR NOT NULL,
            quantity_allocated INTEGER DEFAULT 0 NOT NULL,
            created_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS todo_items (
            id VARCHAR PRIMARY KEY,
            project_id VARCHAR NOT NULL,
            text VARCHAR NOT NULL,
            done BOOLEAN,
            priority INTEGER,
            created_at TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS manufacturers2 (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            url VARCHAR,
            notes TEXT,
            created_at TIMESTAMP DEFAULT now()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS component_taxonomy_path_key ON component_taxonomy USING btree (path)",
        "CREATE INDEX IF NOT EXISTS idx_component_taxonomy_path ON component_taxonomy USING btree (path)",
        "CREATE INDEX IF NOT EXISTS idx_component_taxonomy_parent ON component_taxonomy USING btree (parent_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS parametric_specs_barcode_id_key ON parametric_specs USING btree (barcode_id)",
        "CREATE INDEX IF NOT EXISTS idx_parametric_specs_specs_gin ON parametric_specs USING gin (specs)",
        "CREATE UNIQUE INDEX IF NOT EXISTS manufacturer_parts_manufacturer_id_mpn_key ON manufacturer_parts USING btree (manufacturer_id, mpn)",
        "CREATE INDEX IF NOT EXISTS idx_manufacturer_parts_spec ON manufacturer_parts USING btree (parametric_spec_id)",
        "CREATE INDEX IF NOT EXISTS idx_stock_lots_mfr_part ON stock_lots USING btree (manufacturer_part_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS container_types_name_key ON container_types USING btree (name)",
        "CREATE UNIQUE INDEX IF NOT EXISTS containers_label_key ON containers USING btree (label)",
        "CREATE UNIQUE INDEX IF NOT EXISTS containers_fixture_id_slot_index_key ON containers USING btree (fixture_id, slot_index)",
        "CREATE UNIQUE INDEX IF NOT EXISTS zones_name_key ON zones USING btree (name)",
        "CREATE UNIQUE INDEX IF NOT EXISTS fixtures_zone_id_name_key ON fixtures USING btree (zone_id, name)",
        "CREATE UNIQUE INDEX IF NOT EXISTS bom_allocations_bom_line_id_stock_lot_id_key ON bom_allocations USING btree (bom_line_id, stock_lot_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS manufacturers2_name_key ON manufacturers2 USING btree (name)",
    ]),
    (23, "supplier kits: bulk assortment kits purchased from suppliers", [
        """CREATE TABLE IF NOT EXISTS supplier_kits (
            id VARCHAR PRIMARY KEY,
            barcode_id VARCHAR NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            notes TEXT,
            supplier TEXT,
            supplier_sku TEXT,
            supplier_url TEXT,
            image_path TEXT,
            purchase_order_id TEXT,
            received_at TIMESTAMP,
            received_by TEXT,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS supplier_kit_items (
            id VARCHAR PRIMARY KEY,
            kit_id VARCHAR NOT NULL,
            position INTEGER DEFAULT 0,
            description TEXT NOT NULL,
            quantity INTEGER DEFAULT 1 NOT NULL,
            barcode_id VARCHAR,
            auto_matched BOOLEAN DEFAULT false NOT NULL,
            match_confidence FLOAT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT now(),
            component_id TEXT
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS supplier_kits_barcode_id_key ON supplier_kits USING btree (barcode_id)",
        "CREATE INDEX IF NOT EXISTS idx_supplier_kit_items_kit ON supplier_kit_items USING btree (kit_id)",
    ]),
    (24, "parametric_specs: consolidate scattered columns into specs JSONB, add label_format to taxonomy", [
        "ALTER TABLE parametric_specs ADD COLUMN IF NOT EXISTS specs JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE component_taxonomy ADD COLUMN IF NOT EXISTS label_format VARCHAR",
    ]),
    (25, "filing crate boxes: box_type, dividers, and per-divider bag assignment", [
        # Non-grid storage archetype. A filing crate indexes static bags behind
        # colour-coded dividers instead of addressing them by row/column.
        "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS box_type VARCHAR DEFAULT 'grid'",
        "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS box_metadata JSONB DEFAULT '{}'::jsonb",
        "UPDATE boxes SET box_type = 'grid' WHERE box_type IS NULL",
        # A divider holds MANY bags, so position cannot live in cell_id: the v21
        # partial unique index on (box_id, cell_id) would reject the second bag
        # filed behind any divider. Filing rows therefore leave cell_id NULL --
        # Postgres treats NULLs as distinct in a unique index -- and carry their
        # location here instead. Ordering within a divider is derived at read
        # time from the component's shelf key, never stored.
        "ALTER TABLE bin_assignments ADD COLUMN IF NOT EXISTS divider_id VARCHAR",
        "ALTER TABLE bin_assignments ALTER COLUMN cell_id DROP NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_bin_assignments_divider "
        "ON bin_assignments (box_id, divider_id) WHERE active = TRUE",
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
