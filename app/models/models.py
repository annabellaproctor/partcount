from sqlalchemy import Column, String, Integer, Float, ForeignKey, Text, DateTime, Boolean
from sqlalchemy.orm import relationship, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncAttrs
from datetime import datetime
import uuid


def gen_uuid():
    return str(uuid.uuid4())


class Base(AsyncAttrs, DeclarativeBase):
    pass


class ComponentType(Base):
    __tablename__ = "component_types"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False, unique=True)  # resistor, capacitor, diode...
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    components = relationship("Component", back_populates="component_type")


class Component(Base):
    __tablename__ = "components"

    id = Column(String, primary_key=True, default=gen_uuid)
    barcode_id = Column(String, unique=True, nullable=False)  # R047, C012, D003...
    name = Column(String, nullable=False)                     # 10kΩ 0805
    value = Column(String)                                    # 10k, 100n, 1N4148
    unit = Column(String)                                     # Ω, F, V
    package = Column(String)                                  # 0805, TO-92, DIP8
    voltage_rating = Column(Float)
    tolerance = Column(String)
    notes = Column(Text)
    image_path = Column(String)                               # PNG no-background
    datasheet_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    type_id = Column(String, ForeignKey("component_types.id"))
    component_type = relationship("ComponentType", back_populates="components")

    footprints = relationship("Footprint", back_populates="component")
    bins = relationship("BinAssignment", back_populates="component")


class Footprint(Base):
    """Physical variant of a component — same value, different manufacturer/tape color/source"""
    __tablename__ = "footprints"

    id = Column(String, primary_key=True, default=gen_uuid)
    component_id = Column(String, ForeignKey("components.id"), nullable=False)
    manufacturer = Column(String)                             # Bojack, ELEGOO, YAGEO
    source = Column(String)                                   # Amazon, AliExpress, Digikey
    stripe_color = Column(String)                             # for resistor band identification
    tape_color = Column(String)
    quantity = Column(Integer, default=0)
    low_stock_threshold = Column(Integer, default=10)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    component = relationship("Component", back_populates="footprints")
    bin_assignments = relationship("BinAssignment", back_populates="footprint")


class Box(Base):
    """Physical AideTek box"""
    __tablename__ = "boxes"

    id = Column(String, primary_key=True, default=gen_uuid)
    label = Column(String, nullable=False, unique=True)       # BOX-A, BOX-B
    model = Column(String)                                    # BOXALL144, BOXALL48
    cell_count = Column(Integer)
    location = Column(String)                                 # desk-ledge, shelf-1
    notes = Column(Text)

    bins = relationship("BinAssignment", back_populates="box")


class BinAssignment(Base):
    """Maps a component/footprint to a physical cell in a box"""
    __tablename__ = "bin_assignments"

    id = Column(String, primary_key=True, default=gen_uuid)
    box_id = Column(String, ForeignKey("boxes.id"), nullable=False)
    cell_id = Column(String, nullable=False)                  # R3C4, cell index
    component_id = Column(String, ForeignKey("components.id"), nullable=False)
    footprint_id = Column(String, ForeignKey("footprints.id"))
    active = Column(Boolean, default=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    box = relationship("Box", back_populates="bins")
    component = relationship("Component", back_populates="bins")
    footprint = relationship("Footprint", back_populates="bin_assignments")
