"""Seed data — runs once on startup if tables are empty."""
from sqlalchemy import text
from app.models.database import AsyncSessionLocal
import uuid, logging

log = logging.getLogger("seed")

COMPONENT_TYPES = [
    ("resistor", "R"), ("capacitor", "C"), ("diode", "D"),
    ("transistor", "T"), ("mosfet", "Q"), ("ic", "U"),
    ("inductor", "L"), ("connector", "J"), ("relay", "K"),
    ("led", "LED"), ("module", "MOD"), ("crystal", "Y"),
    ("fuse", "F"), ("switch", "SW"), ("sensor", "S"),
]

MANUFACTURERS = [
    ("Bojack", "bojack"),
    ("ELEGOO", "elegoo"),
    ("YAGEO", "yageo,yaego"),
    ("Vishay", "vishay,siliconix"),
    ("Murata", "murata"),
    ("Samsung Electro-Mechanics", "samsung,sem"),
    ("TDK", "tdk"),
    ("Wurth Elektronik", "wurth,we"),
    ("Panasonic", "panasonic"),
    ("Nichicon", "nichicon"),
    ("Kemet", "kemet"),
    ("AVX", "avx"),
    ("Bourns", "bourns"),
    ("Susumu", "susumu"),
    ("Rohm", "rohm"),
    ("ON Semiconductor", "on semi,onsemi"),
    ("STMicroelectronics", "st,stm"),
    ("Texas Instruments", "ti"),
    ("Microchip", "microchip,atmel"),
    ("NXP", "nxp,freescale"),
    ("Infineon", "infineon,irf"),
    ("Toshiba", "toshiba"),
    ("Renesas", "renesas,intersil"),
    ("Analog Devices", "adi,maxim"),
    ("Linear Technology", "linear,ltc"),
    ("Diodes Inc", "diodes,zetex"),
    ("Fairchild", "fairchild"),
    ("Molex", "molex"),
    ("JST", "jst"),
    ("Amphenol", "amphenol"),
    ("TE Connectivity", "te,tyco,amp"),
    ("Hirose", "hirose"),
    ("Würth", "wurth"),
    ("Alps Alpine", "alps"),
    ("Omron", "omron"),
    ("Littelfuse", "littelfuse"),
    ("Bel Fuse", "bel fuse"),
    ("Schurter", "schurter"),
    ("EPCOS", "epcos,tdk epcos"),
    ("Coilcraft", "coilcraft"),
    ("Sumida", "sumida"),
    ("Abracon", "abracon"),
    ("CTS", "cts"),
    ("Maxim Integrated", "maxim"),
    ("Semtech", "semtech"),
    ("Silicon Labs", "silicon labs,silabs"),
    ("Espressif", "espressif,esp"),
    ("Nordic Semiconductor", "nordic,nrf"),
    ("Bosch Sensortec", "bosch,bme,bmp,bno"),
    ("InvenSense", "invensense,mpu"),
    ("AMS", "ams,osram"),
    ("Broadcom", "broadcom,avago"),
    ("FTDI", "ftdi"),
    ("WCH", "wch,ch340,ch343"),
    ("HiLetgo", "hiletgo"),
    ("WeAct", "weact"),
    ("Adafruit", "adafruit"),
    ("SparkFun", "sparkfun"),
    ("Generic / Unknown", "generic,unknown,noname"),
]

SUPPLIERS = [
    ("Amazon", "https://amazon.com"),
    ("AliExpress", "https://aliexpress.com"),
    ("DigiKey", "https://digikey.com"),
    ("Mouser", "https://mouser.com"),
    ("LCSC", "https://lcsc.com"),
    ("Adafruit", "https://adafruit.com"),
    ("SparkFun", "https://sparkfun.com"),
    ("eBay", "https://ebay.com"),
]


async def seed_all():
    async with AsyncSessionLocal() as db:
        # component types
        result = await db.execute(text("SELECT COUNT(*) FROM component_types"))
        if result.scalar() == 0:
            for name, _ in COMPONENT_TYPES:
                await db.execute(text(
                    "INSERT INTO component_types (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING"
                ), {"id": str(uuid.uuid4()), "name": name})
            log.info(f"Seeded {len(COMPONENT_TYPES)} component types")

        # manufacturers
        result = await db.execute(text("SELECT COUNT(*) FROM manufacturers"))
        if result.scalar() == 0:
            for name, aliases in MANUFACTURERS:
                await db.execute(text(
                    "INSERT INTO manufacturers (id, name, aliases) VALUES (:id, :name, :aliases) ON CONFLICT DO NOTHING"
                ), {"id": str(uuid.uuid4()), "name": name, "aliases": aliases})
            log.info(f"Seeded {len(MANUFACTURERS)} manufacturers")

        # suppliers
        result = await db.execute(text("SELECT COUNT(*) FROM suppliers"))
        if result.scalar() == 0:
            for name, url in SUPPLIERS:
                await db.execute(text(
                    "INSERT INTO suppliers (id, name, url) VALUES (:id, :name, :url) ON CONFLICT DO NOTHING"
                ), {"id": str(uuid.uuid4()), "name": name, "url": url})
            log.info(f"Seeded {len(SUPPLIERS)} suppliers")

        # default profile
        result = await db.execute(text("SELECT COUNT(*) FROM profiles"))
        if result.scalar() == 0:
            import secrets
            await db.execute(text(
                "INSERT INTO profiles (id, name, email, initials) VALUES (:id, :name, :email, :initials)"
            ), {"id": str(uuid.uuid4()), "name": "Annabella", "email": "annabellaproctor@gmail.com", "initials": "AP"})
            log.info("Seeded default profile")

        await db.commit()
