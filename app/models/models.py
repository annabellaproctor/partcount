from sqlalchemy import Column, String, Integer, Float, ForeignKey, Text, DateTime, Boolean, UniqueConstraint, JSON
from sqlalchemy.orm import relationship, backref, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncAttrs
from datetime import datetime
import uuid
import secrets


def gen_uuid():
    return str(uuid.uuid4())


def gen_api_key():
    return f"labinv_{secrets.token_urlsafe(32)}"


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"
    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, default="Annabella")
    email = Column(String, unique=True)
    initials = Column(String, default="AP")
    created_at = Column(DateTime, default=datetime.utcnow)
    api_keys = relationship("APIKey", back_populates="profile")
    projects = relationship("Project", back_populates="profile")


class APIKey(Base):
    __tablename__ = "api_keys"
    id = Column(String, primary_key=True, default=gen_uuid)
    key = Column(String, unique=True, nullable=False, default=gen_api_key)
    label = Column(String, nullable=False)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False)
    active = Column(Boolean, default=True)
    last_used = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    profile = relationship("Profile", back_populates="api_keys")


class Project(Base):
    __tablename__ = "projects"
    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    description = Column(Text)
    status = Column(String, default="active")
    profile_id = Column(String, ForeignKey("profiles.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    profile = relationship("Profile", back_populates="projects")
    todos = relationship("TodoItem", back_populates="project", cascade="all, delete-orphan")
    bom_items = relationship("BOMItem", back_populates="project", cascade="all, delete-orphan")


class TodoItem(Base):
    __tablename__ = "todo_items"
    id = Column(String, primary_key=True, default=gen_uuid)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    text = Column(String, nullable=False)
    done = Column(Boolean, default=False)
    priority = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="todos")


class BOMItem(Base):
    __tablename__ = "bom_items"
    id = Column(String, primary_key=True, default=gen_uuid)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    component_id = Column(String, ForeignKey("components.id"), nullable=True)
    description = Column(String, nullable=False)
    quantity_needed = Column(Integer, default=1)
    quantity_have = Column(Integer, default=0)
    notes = Column(Text)
    sourced = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="bom_items")
    component = relationship("Component")


class ComponentType(Base):
    __tablename__ = "component_types"
    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    components = relationship("Component", back_populates="component_type")


class Component(Base):
    __tablename__ = "components"
    id = Column(String, primary_key=True, default=gen_uuid)
    barcode_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    value = Column(String)
    unit = Column(String)
    package = Column(String)
    voltage_rating = Column(Float)
    tolerance = Column(String)
    notes = Column(Text)
    image_path = Column(String)
    datasheet_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    type_id = Column(String, ForeignKey("component_types.id"))
    manufacturer_id = Column(String, ForeignKey("manufacturers.id"), nullable=True)
    mpn = Column(String)
    digikey_pn = Column(String)
    lcsc_pn = Column(String)
    description = Column(Text)
    short_title = Column(String)
    short_title_manual = Column(Boolean, default=False, nullable=False)
    # Generic component support
    is_generic = Column(Boolean, default=False, nullable=False)
    parent_id = Column(String, ForeignKey("components.id"), nullable=True)
    # Hierarchical type path and type-specific attributes
    type_path = Column(String)
    type_data = Column(JSON)
    component_type = relationship("ComponentType", back_populates="components")
    footprints = relationship("Footprint", back_populates="component")
    bins = relationship("BinAssignment", back_populates="component")
    component_suppliers = relationship("ComponentSupplier", back_populates="component")
    # Self-referential: a generic parent has many brand-specific children
    children = relationship(
        "Component",
        backref=backref("parent", remote_side="Component.id"),
        foreign_keys="Component.parent_id",
    )
    kit_components = relationship("KitComponent", back_populates="component")


class Kit(Base):
    __tablename__ = "kits"
    id = Column(String, primary_key=True, default=gen_uuid)
    barcode_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    image_path = Column(String)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    kit_components = relationship("KitComponent", back_populates="kit", cascade="all, delete-orphan")


class KitComponent(Base):
    __tablename__ = "kit_components"
    id = Column(String, primary_key=True, default=gen_uuid)
    kit_id = Column(String, ForeignKey("kits.id", ondelete="CASCADE"), nullable=False)
    component_id = Column(String, ForeignKey("components.id", ondelete="CASCADE"), nullable=False)
    quantity = Column(Integer, default=1)
    notes = Column(Text)
    position = Column(Integer)

    kit = relationship("Kit", back_populates="kit_components")
    component = relationship("Component", back_populates="kit_components")

    __table_args__ = (
        UniqueConstraint("kit_id", "component_id"),
    )


class Footprint(Base):
    __tablename__ = "footprints"
    id = Column(String, primary_key=True, default=gen_uuid)
    component_id = Column(String, ForeignKey("components.id"), nullable=False)
    manufacturer = Column(String)
    source = Column(String)
    stripe_color = Column(String)
    tape_color = Column(String)
    quantity = Column(Integer, default=0)
    low_stock_threshold = Column(Integer, default=10)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    component = relationship("Component", back_populates="footprints")
    bin_assignments = relationship("BinAssignment", back_populates="footprint")


class Box(Base):
    __tablename__ = "boxes"
    id = Column(String, primary_key=True, default=gen_uuid)
    label = Column(String, nullable=False, unique=True)
    model = Column(String)
    cell_count = Column(Integer)
    location = Column(String)
    slot_index = Column(Integer, default=0)
    notes = Column(Text)
    bins = relationship("BinAssignment", back_populates="box")


class BinAssignment(Base):
    __tablename__ = "bin_assignments"
    id = Column(String, primary_key=True, default=gen_uuid)
    box_id = Column(String, ForeignKey("boxes.id"), nullable=False)
    cell_id = Column(String, nullable=False)
    component_id = Column(String, ForeignKey("components.id"), nullable=False)
    footprint_id = Column(String, ForeignKey("footprints.id"))
    active = Column(Boolean, default=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)
    box = relationship("Box", back_populates="bins")
    component = relationship("Component", back_populates="bins")
    footprint = relationship("Footprint", back_populates="bin_assignments")


class Manufacturer(Base):
    __tablename__ = "manufacturers"
    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, unique=True)
    aliases = Column(Text)
    url = Column(String)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Suppliers + Purchase Orders
# ---------------------------------------------------------------------------

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)            # Amazon, AliExpress, Digikey, Molex
    url = Column(String)
    notes = Column(Text)
    component_suppliers = relationship("ComponentSupplier", back_populates="supplier")
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")


class ComponentSupplier(Base):
    """Links a component to a supplier with SKU/MPN/price"""
    __tablename__ = "component_suppliers"
    id = Column(String, primary_key=True, default=gen_uuid)
    component_id = Column(String, ForeignKey("components.id"), nullable=False)
    supplier_id = Column(String, ForeignKey("suppliers.id"), nullable=False)
    sku = Column(String)                             # supplier SKU / ASIN
    mpn = Column(String)                             # manufacturer part number
    unit_price = Column(Float)
    pack_size = Column(Integer, default=1)
    url = Column(String)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    component = relationship("Component", back_populates="component_suppliers")
    supplier = relationship("Supplier", back_populates="component_suppliers")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(String, primary_key=True, default=gen_uuid)
    supplier_id = Column(String, ForeignKey("suppliers.id"))
    reference = Column(String)                       # Amazon order number, etc
    status = Column(String, default="pending")       # pending, ordered, received, cancelled
    order_date = Column(DateTime)
    expected_date = Column(DateTime)
    received_date = Column(DateTime)
    total_cost = Column(Float)
    order_url = Column(String)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    supplier = relationship("Supplier", back_populates="purchase_orders")
    items = relationship("PurchaseOrderItem", back_populates="order", cascade="all, delete-orphan")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    id = Column(String, primary_key=True, default=gen_uuid)
    order_id = Column(String, ForeignKey("purchase_orders.id"), nullable=False)
    component_id = Column(String, ForeignKey("components.id"))
    component_supplier_id = Column(String, ForeignKey("component_suppliers.id"))
    quantity_ordered = Column(Integer, default=1)
    quantity_received = Column(Integer, default=0)
    unit_price = Column(Float)
    notes = Column(Text)
    order = relationship("PurchaseOrder", back_populates="items")
    component = relationship("Component")
    component_supplier = relationship("ComponentSupplier")


class SystemSetting(Base):
    """Key-value store for system-level settings (e.g. OAuth tokens)."""
    __tablename__ = "system_settings"
    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class APIUsage(Base):
    """Track API usage for rate limit monitoring."""
    __tablename__ = "api_usage"
    id = Column(String, primary_key=True, default=gen_uuid)
    api_name = Column(String, nullable=False, index=True)  # 'digikey', 'mouser', 'brave', 'tavily', 'gemini', etc
    endpoint = Column(String)  # specific endpoint called
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    success = Column(Boolean, default=True)
    error_message = Column(Text)
    response_time_ms = Column(Integer)  # milliseconds
