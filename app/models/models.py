from sqlalchemy import Column, String, Integer, Float, ForeignKey, Text, DateTime, Boolean
from sqlalchemy.orm import relationship, DeclarativeBase
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
    component_type = relationship("ComponentType", back_populates="components")
    footprints = relationship("Footprint", back_populates="component")
    bins = relationship("BinAssignment", back_populates="component")


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
