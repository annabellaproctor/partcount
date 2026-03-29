# Lab Inventory

Component inventory system with barcode tracking, InfluxDB event logging, and real-time WebSocket UI.

## Stack
- FastAPI + Uvicorn
- PostgreSQL 16
- InfluxDB3 (existing instance at 192.168.1.4)
- HTMX + Bootstrap 5
- python-barcode (Code 128)
- Pillow (image autocrop)

## Setup

### 1. Configure environment
```bash
cp .env .env.local
# edit .env.local — set POSTGRES_PASSWORD, INFLUX_TOKEN, SECRET_KEY
```

Generate a secret key:
```bash
openssl rand -hex 32
```

### 2. Start
```bash
docker compose --env-file .env up -d --build
```

### 3. Access
- App: http://192.168.1.4:8090
- pgAdmin: http://192.168.1.4:5050
- Traefik: http://inventory.local (if Traefik is running)

### 4. First run — seed component types
```bash
docker exec -it labinv_app python -c "
import asyncio
from app.models.database import AsyncSessionLocal
from app.models.models import ComponentType

async def seed():
    async with AsyncSessionLocal() as db:
        types = [
            ('resistor', 'R'),
            ('capacitor', 'C'),
            ('diode', 'D'),
            ('transistor', 'T'),
            ('ic', 'U'),
            ('inductor', 'L'),
            ('connector', 'J'),
            ('relay', 'K'),
            ('led', 'LED'),
            ('mosfet', 'Q'),
            ('module', 'MOD'),
        ]
        for name, _ in types:
            db.add(ComponentType(name=name))
        await db.commit()
        print('seeded')

asyncio.run(seed())
"
```

## Barcode IDs
- Auto-generated on component creation
- Format: prefix + 3-digit sequence — R001, C047, D003
- Prefix derived from component type first letter
- Code 128 format — compatible with Tera 5100 scanner

## Label printing
- Outside label (human readable): `/labels/print/{barcode_id}`
- Inside label (Code 128 barcode): `/labels/print-inside/{barcode_id}`
- Full sheet batch: `/labels/sheet`
- Open in browser → Ctrl+P → set paper size to 1"×0.5" or Letter

## WebSocket events
All events broadcast to connected clients:
- `scan` — barcode scanned
- `stock_change` — quantity updated
- `component_created` — new component added

HID scanner input is captured globally on all pages via keydown buffer.

## InfluxDB measurements
- `scan_event` — tags: barcode_id, box, cell | fields: component_name, scanned
- `stock_change` — tags: barcode_id, footprint_id | fields: component_name, delta, quantity

## Directory structure
```
labinventory/
├── app/
│   ├── main.py
│   ├── models/
│   │   ├── database.py
│   │   └── models.py
│   ├── routers/
│   │   ├── components.py
│   │   ├── boxes.py
│   │   └── labels.py
│   ├── services/
│   │   ├── barcode_svc.py
│   │   ├── influx.py
│   │   └── ws_manager.py
│   └── static/
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── components.html
│   ├── boxes.html
│   └── scan.html
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env
```
