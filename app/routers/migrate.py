"""One-shot migration helper — adds slot_index to boxes if missing"""
from fastapi import APIRouter
from app.models.database import engine
from sqlalchemy import text

router = APIRouter(prefix="/admin", tags=["admin"])

@router.post("/migrate")
async def run_migrations():
    async with engine.begin() as conn:
        # add slot_index if not exists
        await conn.execute(text("""
            ALTER TABLE boxes ADD COLUMN IF NOT EXISTS slot_index INTEGER DEFAULT 0;
        """))
        # add new tables if not exists
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS suppliers (
                id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                url VARCHAR,
                notes TEXT
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS component_suppliers (
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
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS purchase_orders (
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
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS purchase_order_items (
                id VARCHAR PRIMARY KEY,
                order_id VARCHAR REFERENCES purchase_orders(id),
                component_id VARCHAR REFERENCES components(id),
                component_supplier_id VARCHAR REFERENCES component_suppliers(id),
                quantity_ordered INTEGER DEFAULT 1,
                quantity_received INTEGER DEFAULT 0,
                unit_price FLOAT,
                notes TEXT
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS profiles (
                id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                email VARCHAR UNIQUE,
                initials VARCHAR DEFAULT 'AP',
                created_at TIMESTAMP DEFAULT NOW()
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id VARCHAR PRIMARY KEY,
                key VARCHAR UNIQUE NOT NULL,
                label VARCHAR NOT NULL,
                profile_id VARCHAR REFERENCES profiles(id),
                active BOOLEAN DEFAULT TRUE,
                last_used TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS projects (
                id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                description TEXT,
                status VARCHAR DEFAULT 'active',
                profile_id VARCHAR REFERENCES profiles(id),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS todo_items (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR REFERENCES projects(id),
                text VARCHAR NOT NULL,
                done BOOLEAN DEFAULT FALSE,
                priority INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bom_items (
                id VARCHAR PRIMARY KEY,
                project_id VARCHAR REFERENCES projects(id),
                component_id VARCHAR REFERENCES components(id),
                description VARCHAR NOT NULL,
                quantity_needed INTEGER DEFAULT 1,
                quantity_have INTEGER DEFAULT 0,
                notes TEXT,
                sourced BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """))
    return {"status": "ok", "message": "migrations applied"}
