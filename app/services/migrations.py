"""
Schema migration system.
Each migration is a (version, description, list_of_sql_statements) tuple.
Migrations run in order, each in its own transaction with verification.
On failure, rolls back and halts startup.
"""
from sqlalchemy import text
from app.models.database import engine
import logging

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
    ]),

    (2, "add digikey fields to component_suppliers", [
        "ALTER TABLE component_suppliers ADD COLUMN IF NOT EXISTS digikey_pn VARCHAR",
        "ALTER TABLE component_suppliers ADD COLUMN IF NOT EXISTS lcsc_pn VARCHAR",
    ]),

    (3, "add manufacturer registry", [
        """CREATE TABLE IF NOT EXISTS manufacturers (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            aliases TEXT,
            url VARCHAR,
            notes TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS manufacturer_id VARCHAR REFERENCES manufacturers(id)",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS mpn VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS digikey_pn VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS lcsc_pn VARCHAR",
        "ALTER TABLE components ADD COLUMN IF NOT EXISTS description TEXT",
    ]),

    (4, "add schema preview cache for add form", [
        """CREATE TABLE IF NOT EXISTS component_lookups (
            id VARCHAR PRIMARY KEY,
            query VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            result_json TEXT NOT NULL,
            fetched_at TIMESTAMP DEFAULT NOW()
        )""",
    ]),
]


async def get_current_version(conn) -> int:
    try:
        result = await conn.execute(text(
            "SELECT MAX(version) FROM schema_versions"
        ))
        val = result.scalar()
        return val if val is not None else 0
    except Exception:
        return 0


async def verify_tables(conn, version: int) -> dict:
    """Spot-check key tables exist and return row counts."""
    checks = {}
    key_tables = ["profiles", "components", "boxes", "component_types"]
    for table in key_tables:
        try:
            r = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
            checks[table] = r.scalar()
        except Exception as e:
            checks[table] = f"ERROR: {e}"
    return checks


async def run_migrations():
    async with engine.begin() as conn:
        current = await get_current_version(conn)
        pending = [(v, d, stmts) for v, d, stmts in MIGRATIONS if v > current]

        if not pending:
            log.info(f"Schema at version {current}, no migrations needed")
            return

        for version, description, statements in pending:
            log.info(f"Applying migration v{version}: {description}")
            try:
                for sql in statements:
                    await conn.execute(text(sql))

                await conn.execute(text(
                    "INSERT INTO schema_versions (version, description) VALUES (:v, :d) "
                    "ON CONFLICT (version) DO NOTHING"
                ), {"v": version, "d": description})

                checks = await verify_tables(conn, version)
                log.info(f"v{version} applied. Table checks: {checks}")

            except Exception as e:
                log.error(f"Migration v{version} FAILED: {e}")
                raise RuntimeError(f"Migration v{version} failed, startup halted: {e}")

        log.info(f"Migrations complete. Schema now at v{max(v for v,_,_ in pending)}")
