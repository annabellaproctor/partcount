"""
New multi-dimensional component taxonomy models.
These tables coexist with the legacy Component/Footprint/BinAssignment/Kit
tables — no existing tables are dropped or altered.

Hierarchy overview:
  ComponentTaxonomy  →  ParametricSpec  →  ManufacturerPart  →  StockLot
  Zone → Fixture → Container (typed by ContainerType) → CellAssignment → StockLot
  Kit → KitLine  (parametric OR exact spec)
  Project → BomLine (parametric OR exact spec) → BomAllocation → StockLot
"""

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, DateTime,
    ForeignKey, UniqueConstraint, CheckConstraint, Index, JSON, Computed,
)
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime
import uuid


def _uuid():
    return str(uuid.uuid4())


class NewBase(AsyncAttrs, DeclarativeBase):
    # Separate declarative base so these models stay isolated from the legacy
    # Base in models.py.  Both bases share the same AsyncEngine / metadata at
    # the SQL level because all tables land in the same PostgreSQL schema.
    pass


# ---------------------------------------------------------------------------
# Component Taxonomy
# ---------------------------------------------------------------------------

class ComponentTaxonomy(NewBase):
    """
    Hierarchical type tree: passives → resistor → fixed-film
    path is a slash-delimited unique key, e.g. "passives/resistor/fixed".
    spec_schema (JSONB) describes extra parametric fields valid for this node
    so the UI can render the right form fields without hard-coding per-type logic.
    """
    __tablename__ = "component_taxonomy"

    id = Column(String, primary_key=True, default=_uuid)
    path = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False)
    parent_id = Column(String, ForeignKey("component_taxonomy.id"), nullable=True)
    spec_schema = Column(JSON, nullable=True)
    label_format = Column(String, nullable=True)

    children = relationship(
        "ComponentTaxonomy",
        backref=backref("parent", remote_side="ComponentTaxonomy.id"),
        foreign_keys=[parent_id],
    )
    parametric_specs = relationship("ParametricSpec", back_populates="taxonomy_node")


# ---------------------------------------------------------------------------
# Parametric Specification
# ---------------------------------------------------------------------------

class ParametricSpec(NewBase):
    """
    Abstract component definition — what you *want*, not what brand/lot you have.
    Example: "10kΩ ±5% 0603" or "ESP32-S3 WROOM-1".

    barcode_id keeps the existing R4K2M scheme so legacy label stock still scans.
    type_params holds type-specific fields (led_color, clock_speed_mhz, etc.)
    that do not warrant their own column.
    """
    __tablename__ = "parametric_specs"

    id = Column(String, primary_key=True, default=_uuid)
    barcode_id = Column(String, unique=True, nullable=False)
    component_type_id = Column(String, ForeignKey("component_taxonomy.id"), nullable=False)

    # All field values stored as a single JSONB blob.
    # Keys are the field_name values from field_definitions / taxonomy spec_schema.
    # The name column is auto-generated from the taxonomy label_format + specs.
    specs = Column(JSON)

    name = Column(String, nullable=False)
    description = Column(Text)
    notes = Column(Text)
    image_path = Column(String)
    search_alias = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    taxonomy_node = relationship("ComponentTaxonomy", back_populates="parametric_specs")
    manufacturer_parts = relationship("ManufacturerPart", back_populates="parametric_spec")
    kit_lines = relationship("KitLine", back_populates="parametric_spec",
                             foreign_keys="KitLine.parametric_spec_id")
    bom_lines = relationship("BomLine", back_populates="parametric_spec",
                             foreign_keys="BomLine.parametric_spec_id")

    __table_args__ = ()  # GIN index on specs created by migration DDL


# ---------------------------------------------------------------------------
# Manufacturer + Manufacturer Part (MPN)
# ---------------------------------------------------------------------------

class Manufacturer2(NewBase):
    """
    Manufacturer registry for the new taxonomy system.
    Named Manufacturer2 in Python to avoid collision with legacy Manufacturer ORM
    class; the SQL table is named 'manufacturers2'.
    """
    __tablename__ = "manufacturers2"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False, unique=True)
    url = Column(String)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    parts = relationship("ManufacturerPart", back_populates="manufacturer")


class ManufacturerPart(NewBase):
    """
    Concrete MPN — the specific part a manufacturer sells that satisfies a spec.
    Example: Yageo RC0603FR-0710KL satisfies "10kΩ ±1% 0603".

    lifecycle_status: active | nrnd | eol | discontinued
    """
    __tablename__ = "manufacturer_parts"

    id = Column(String, primary_key=True, default=_uuid)
    mpn = Column(String, nullable=False)
    manufacturer_id = Column(String, ForeignKey("manufacturers2.id"), nullable=False)
    parametric_spec_id = Column(String, ForeignKey("parametric_specs.id"), nullable=True)

    digikey_pn = Column(String)
    mouser_pn = Column(String)
    lcsc_pn = Column(String)
    datasheet_url = Column(String)
    lifecycle_status = Column(String, default="active")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    manufacturer = relationship("Manufacturer2", back_populates="parts")
    parametric_spec = relationship("ParametricSpec", back_populates="manufacturer_parts")
    stock_lots = relationship("StockLot", back_populates="manufacturer_part")

    __table_args__ = (
        UniqueConstraint("manufacturer_id", "mpn", name="uq_manufacturer_parts_mfr_mpn"),
        Index("idx_manufacturer_parts_spec", "parametric_spec_id"),
    )


# ---------------------------------------------------------------------------
# Stock Lot
# ---------------------------------------------------------------------------

class StockLot(NewBase):
    """
    Physical inventory instance — one tape reel, one bag, one cut strip.
    effective_quantity is derived: quantity + sigma_adjustment.
    (PostgreSQL GENERATED ALWAYS AS is expressed via a DB-side column;
     SQLAlchemy reads it as a plain Integer column.)

    packaging_type: tape_reel | cut_tape | tray | bulk | tube | bag
    """
    __tablename__ = "stock_lots"

    id = Column(String, primary_key=True, default=_uuid)
    manufacturer_part_id = Column(String, ForeignKey("manufacturer_parts.id"), nullable=False)

    quantity = Column(Integer, nullable=False, default=0)
    sigma_adjustment = Column(Integer, nullable=False, default=0)
    # GENERATED ALWAYS AS (quantity + sigma_adjustment) STORED in PostgreSQL.
    # Computed() tells SQLAlchemy this column is server-managed: never written
    # in INSERT/UPDATE, always read back from the DB.
    effective_quantity = Column(Integer, Computed("quantity + sigma_adjustment", persisted=True))

    acquired_date = Column(DateTime)
    # References purchase_orders(id) at the DB level (enforced by migration DDL).
    # Declared as plain String here because purchase_orders lives under the legacy
    # Base and NewBase metadata has no visibility into it — SQLAlchemy would error
    # on cross-base FK resolution at mapper configure time.
    purchase_order_id = Column(String, nullable=True)
    unit_cost = Column(Float)
    lot_code = Column(String)

    packaging_type = Column(String)
    tape_color = Column(String)
    stripe_color = Column(String)
    low_stock_threshold = Column(Integer, default=10)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    manufacturer_part = relationship("ManufacturerPart", back_populates="stock_lots")
    cell_assignments = relationship("CellAssignment", back_populates="stock_lot")
    bom_allocations = relationship("BomAllocation", back_populates="stock_lot")

    __table_args__ = (
        Index("idx_stock_lots_mfr_part", "manufacturer_part_id"),
    )


# ---------------------------------------------------------------------------
# Location Hierarchy: Zone → Fixture → Container → CellAssignment
# ---------------------------------------------------------------------------

class Zone(NewBase):
    """
    Top-level physical location: 'Main Bench', 'Parts Shelf', 'SMD Cabinet'.
    sort_order controls display order in the UI.
    """
    __tablename__ = "zones"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text)
    sort_order = Column(Integer, default=0)

    fixtures = relationship("Fixture", back_populates="zone")


class Fixture(NewBase):
    """
    Mounting within a zone: a rack unit, a drawer unit, a shelf row.
    Names must be unique within a zone.
    """
    __tablename__ = "fixtures"

    id = Column(String, primary_key=True, default=_uuid)
    zone_id = Column(String, ForeignKey("zones.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    sort_order = Column(Integer, default=0)

    zone = relationship("Zone", back_populates="fixtures")
    containers = relationship("Container", back_populates="fixture")

    __table_args__ = (
        UniqueConstraint("zone_id", "name", name="uq_fixtures_zone_name"),
    )


class ContainerType(NewBase):
    """
    Reusable box/drawer template: BOXALL144 (12×12), 3U-DRAWER (4×6), etc.

    allow_multi_spec: can a single cell hold parts from different specs?
    allow_value_range_pct: max % difference between spec values in the same cell.
    require_same_package: enforce package match for mixed-value cells.
    """
    __tablename__ = "container_types"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False, unique=True)
    grid_rows = Column(Integer, nullable=False)
    grid_cols = Column(Integer, nullable=False)
    cell_volume_mm3 = Column(Float)

    allow_multi_spec = Column(Boolean, default=False, nullable=False)
    allow_value_range_pct = Column(Float)
    require_same_package = Column(Boolean, default=True, nullable=False)

    containers = relationship("Container", back_populates="container_type")


class Container(NewBase):
    """
    Physical box or drawer instance.
    label is a globally unique human-readable identifier (e.g. 'SMD-A3').
    slot_index orders containers within their fixture.
    """
    __tablename__ = "containers"

    id = Column(String, primary_key=True, default=_uuid)
    label = Column(String, nullable=False, unique=True)
    container_type_id = Column(String, ForeignKey("container_types.id"), nullable=False)
    fixture_id = Column(String, ForeignKey("fixtures.id"), nullable=False)
    slot_index = Column(Integer, default=0)

    container_type = relationship("ContainerType", back_populates="containers")
    fixture = relationship("Fixture", back_populates="containers")
    cell_assignments = relationship("CellAssignment", back_populates="container")

    __table_args__ = (
        UniqueConstraint("fixture_id", "slot_index", name="uq_containers_fixture_slot"),
    )


class CellAssignment(NewBase):
    """
    Maps a stock lot to a physical cell within a container.
    cell_id is a string like 'R3C7' (row 3, col 7).

    The UNIQUE partial index on (container_id, cell_id, stock_lot_id) WHERE active = TRUE
    prevents the same lot from appearing twice in the same cell while allowing
    historical (inactive) records for audit trails.

    Multiple different stock_lot_ids can map to the same cell only when the
    parent container_type.allow_multi_spec = TRUE.
    """
    __tablename__ = "cell_assignments"

    id = Column(String, primary_key=True, default=_uuid)
    container_id = Column(String, ForeignKey("containers.id"), nullable=False)
    cell_id = Column(String, nullable=False)
    stock_lot_id = Column(String, ForeignKey("stock_lots.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    container = relationship("Container", back_populates="cell_assignments")
    stock_lot = relationship("StockLot", back_populates="cell_assignments")

    __table_args__ = (
        Index("idx_cell_assignments_container_cell_active",
              "container_id", "cell_id", "active"),
        # Partial unique constraint expressed as a DDL Index; the migration
        # creates the real WHERE-clause version via raw SQL.
    )


# ---------------------------------------------------------------------------
# Kit Lines (parametric kit BOM)
# ---------------------------------------------------------------------------

class KitLine(NewBase):
    """
    One line in a parametric kit.  Satisfies the requirement by matching EITHER:
      a) an exact parametric_spec_id, OR
      b) any part whose taxonomy node + electrical constraints match.

    The CHECK constraint enforces that at least one of the two is set.
    param_constraints (JSONB) holds extra type-specific filters
    (e.g. {"led_color": "red", "clock_speed_mhz_min": 240}).
    """
    __tablename__ = "kit_lines"

    id = Column(String, primary_key=True, default=_uuid)
    # kits is a legacy-Base table; FK enforced by migration DDL, not ORM metadata.
    kit_id = Column(String, nullable=False)
    position = Column(Integer, default=0)
    quantity = Column(Integer, nullable=False, default=1)
    notes = Column(Text)

    # Exact match path
    parametric_spec_id = Column(String, ForeignKey("parametric_specs.id"), nullable=True)

    # Parametric match path
    component_type_id = Column(String, ForeignKey("component_taxonomy.id"), nullable=True)
    value_min = Column(Float)
    value_max = Column(Float)
    tolerance_max = Column(String)
    voltage_min = Column(Float)
    package = Column(String)
    param_constraints = Column(JSON)

    created_at = Column(DateTime, default=datetime.utcnow)

    parametric_spec = relationship("ParametricSpec", back_populates="kit_lines",
                                   foreign_keys=[parametric_spec_id])
    taxonomy_node = relationship("ComponentTaxonomy",
                                 foreign_keys=[component_type_id])

    __table_args__ = (
        CheckConstraint(
            "parametric_spec_id IS NOT NULL OR component_type_id IS NOT NULL",
            name="ck_kit_lines_has_spec_or_type",
        ),
    )


# ---------------------------------------------------------------------------
# BOM Lines + Allocations (project BOM)
# ---------------------------------------------------------------------------

class BomLine(NewBase):
    """
    One line in a project BOM.  Same parametric-or-exact duality as KitLine.
    ref_des holds the KiCad reference designator (e.g. 'R4', 'C17').
    """
    __tablename__ = "bom_lines"

    id = Column(String, primary_key=True, default=_uuid)
    # projects is a legacy-Base table; FK enforced by migration DDL, not ORM metadata.
    project_id = Column(String, nullable=False)
    ref_des = Column(String)
    position = Column(Integer, default=0)
    quantity = Column(Integer, nullable=False, default=1)
    notes = Column(Text)

    # Exact match path
    parametric_spec_id = Column(String, ForeignKey("parametric_specs.id"), nullable=True)

    # Parametric match path
    component_type_id = Column(String, ForeignKey("component_taxonomy.id"), nullable=True)
    value_min = Column(Float)
    value_max = Column(Float)
    tolerance_max = Column(String)
    voltage_min = Column(Float)
    package = Column(String)
    param_constraints = Column(JSON)

    created_at = Column(DateTime, default=datetime.utcnow)

    parametric_spec = relationship("ParametricSpec", back_populates="bom_lines",
                                   foreign_keys=[parametric_spec_id])
    taxonomy_node = relationship("ComponentTaxonomy",
                                 foreign_keys=[component_type_id])
    allocations = relationship("BomAllocation", back_populates="bom_line",
                               cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "parametric_spec_id IS NOT NULL OR component_type_id IS NOT NULL",
            name="ck_bom_lines_has_spec_or_type",
        ),
    )


class BomAllocation(NewBase):
    """
    Reserves a quantity from a specific stock lot for a BOM line.
    Prevents the same lot from being double-allocated across BOM lines
    (UNIQUE on bom_line_id + stock_lot_id).
    """
    __tablename__ = "bom_allocations"

    id = Column(String, primary_key=True, default=_uuid)
    bom_line_id = Column(String, ForeignKey("bom_lines.id", ondelete="CASCADE"), nullable=False)
    stock_lot_id = Column(String, ForeignKey("stock_lots.id"), nullable=False)
    quantity_allocated = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    bom_line = relationship("BomLine", back_populates="allocations")
    stock_lot = relationship("StockLot", back_populates="bom_allocations")

    __table_args__ = (
        UniqueConstraint("bom_line_id", "stock_lot_id", name="uq_bom_allocations_line_lot"),
    )
