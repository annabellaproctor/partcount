# LAB::INV — Electronics Lab Inventory System

> A self-hosted, full-stack electronics component inventory system built for a real maker lab.  
> Designed to replace the chaos of unlabeled component trays, mid-solder "where is my 100nF cap" moments, and the mental overhead of managing hundreds of part types across multiple storage boxes.

---

## Why This Exists

This project started from a real problem: building a growing electronics lab from scratch — ESP32 firmware, ESPHome, KiCad PCB design, motor control, BLE trilateration, home automation — and constantly losing track of what components were on hand, where they lived, and when stock was getting low.

The trigger: running out of solder mid-joint and out of lever nuts mid-wire, not noticing until it mattered. Counting components by hand mid-project is not acceptable when you're deep in a debug session.

The lab has:
- 12× ESP32-DevKitC USB-C as primary MCU platform
- BOJACK resistor/capacitor/diode/transistor assortment kits (1000+ individual components)
- JST PH/XH/SH connector kits, IWISS Dupont kit
- 3× AideTek BOXALL144 + 3× BOXALL96 + 3× BOXALL48 for storage
- HM305P 30V/5A bench PSU, Bambu A1 3D printer, KiCad 9 workflow
- Active projects: gate access controller (ESPHome + Wiegand + mag lock), BLE cat tracker (12× ESP32 anchor nodes), custom motor controller PCBs, STM32+ESP32 split-MCU robotics

What was needed: a system that tracks every component by barcode, maps it to a physical bin location, integrates with DigiKey and LCSC for pricing/ordering, and works with a Tera 5100 handheld barcode scanner over HID. Everything self-hosted, LAN-only, no cloud dependency.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Python 3.12, async SQLAlchemy |
| Database | PostgreSQL 16 |
| Time-series | InfluxDB3 (scan events, stock changes) |
| Frontend | HTMX + Bootstrap 5 + JetBrains Mono |
| AI | Gemini 2.0 Flash (parse, merge, classify) |
| Images | DuckDuckGo search + rembg background removal |
| Barcodes | Code 128 via python-barcode |
| Deployment | Docker Compose, `network_mode: host`, LAN only |
| Server | Intel N95 mini PC, WSL2, 192.168.1.4 |

---

## Features

### Component Registry
- Alphanumeric barcode IDs: `{PREFIX}-{4chars}` e.g. `R-4K2M`, `M-A3FP`
- Excludes confusable characters (0/O, 1/I/L) for reliable scanning
- Code 128 barcodes — scanner-compatible at small label sizes
- Label system: Code 128 on inside lid, human-readable on outside lid

### Lookup Engine
- DigiKey v4 API + LCSC `wwwapi.lcsc.com` queried **in parallel**
- Each source cached independently (24h) — if one is cached, other still queries fresh
- Never caches results with empty name + manufacturer + MPN (data source errors)
- Relevance scoring: exact MPN match, major brand bonus, has image/datasheet, previously-used manufacturer preference, unknown manufacturer penalty
- Gemini 2.0 Flash merges top results from both sources when available
- ★ High confidence badge for score ≥ 0.6 + image + MPN/datasheet

### AI Integration (Gemini 2.0 Flash, schema-enforced)
- **Parse**: paste any Amazon listing, datasheet snippet, product description → structured component record, all fields autofilled
- **Merge**: when DigiKey + LCSC both return results, Gemini picks best name, image, description, resolves conflicts
- **Classify**: type detection (resistor/capacitor/IC/module/etc.) + barcode prefix suggestion, fires as async fallback when local keyword heuristics fail
- Uses `responseMimeType: application/json` + `responseSchema` — guaranteed valid JSON, zero cleaning

### Physical Storage Mapping
- AideTek BOXALL box registry with proportional mini-grid visualization
- Cells colored green (assigned) / gray (empty) loaded live from API
- Box grid split horizontally — top/bottom half divider matches physical box layout
- Drag-and-drop cell assignment with amber preview-before-confirm
- Ledge layout drag-to-reorder with slot persistence

### Scan / Search / Order
- **Scan mode**: HID barcode scanner input, component card with bin location
- **Search mode**: full-text search across components + boxes + projects simultaneously
- **Order mode**: live DigiKey/LCSC results with images, pricing, datasheet, buy link, one-click import to inventory

### Images
- DuckDuckGo image search (no API key)
- Background removal via `rembg` (deep learning) with white-pixel fallback
- Always PNG with transparency; UI renders white behind transparent images

### Infrastructure
- Versioned schema migrations (auto-run on startup, per-statement transactions)
- WebSocket broadcast for real-time scan/stock events
- InfluxDB3 time-series for scan history and stock changes
- Grafana-compatible for dashboards

---

## AI Usage — Conservation Notes

Gemini calls are made only when:
1. User explicitly clicks **AI Parse** (paste → parse)
2. Both DigiKey AND LCSC return results (merge — one call per search)
3. Local keyword heuristics fail to classify a component type (one call per selection, async, non-blocking)

DigiKey/LCSC results are cached 24h per source independently. Gemini-merged results are not separately cached — they derive from cached source data. Cache can be force-busted with Shift+Enter or by double-searching within 60 seconds.

**Model used**: `gemini-2.0-flash` — fastest/cheapest Gemini tier, appropriate for structured extraction tasks. Schema enforcement means no retry logic needed.

---

## Project Structure

```
app/
├── main.py                    # FastAPI app, routes, lifespan, migrations
├── models/
│   ├── database.py            # AsyncSession, engine
│   └── models.py              # SQLAlchemy models
├── routers/
│   ├── components.py          # /api/components/ — CRUD, types, search, scan
│   ├── boxes.py               # /api/boxes/ — grid, assign, swap, slot
│   ├── lookup.py              # /api/lookup/ — search, part detail, cache clear
│   ├── images.py              # /api/images/ — DDG search, fetch, upload, bg removal
│   ├── ai_parse.py            # /api/ai/ — parse, merge, classify (Gemini)
│   ├── labels.py              # /api/labels/ — Code128 + QR generation
│   ├── projects.py            # /api/projects/ — CRUD, todos, BOM
│   ├── suppliers.py           # /api/suppliers/ — orders, component_suppliers
│   └── manufacturers.py       # /api/manufacturers/
└── services/
    ├── migrations.py          # Versioned schema migrations v1-v5
    ├── digikey.py             # DigiKey v4 API, client credentials, cut-tape preference
    ├── lcsc.py                # LCSC wwwapi.lcsc.com/v1, field mapping
    ├── lookup_engine.py       # Parallel fetch, dedupe, score, Gemini merge
    ├── generic_icons.py       # Schematic SVG icons for all component types
    ├── barcode_svc.py         # Code128, QR, alphanumeric ID generation
    └── influx.py              # InfluxDB3 write for scan/stock events

templates/
├── base.html                  # Layout, nav, WebSocket, add popup JS, Gemini classify
├── add_popup.html             # Global add popup — lookup, AI paste, form
├── scan.html                  # Scan / Search / Order three-mode page
├── boxes.html                 # Box registry with visual mini-grids
├── box_grid.html              # Full grid — top/bottom split, drag-drop
├── component_detail.html      # Detail — image search, bg removal, suppliers
└── ...
```

---

## Environment Variables

```env
DATABASE_URL=postgresql+asyncpg://labinv:password@localhost/labinventory
INFLUX_URL=http://localhost:8181
INFLUX_TOKEN=...
INFLUX_ORG=lab
INFLUX_BUCKET=labinventory
DIGIKEY_CLIENT_ID=...
DIGIKEY_CLIENT_SECRET=...
GEMINI_API_KEY=...
IMAGE_DIR=/app/images
```

---

## Potential Improvements (for AI assistants reading this)

- **DigiKey OAuth token refresh**: currently cached in-memory, lost on restart — add persistent token storage to DB or file
- **LCSC rate limiting**: no backoff implemented, could get 429'd under heavy search load  
- **Gemini merge caching**: merged results aren't cached — if same query fires twice quickly, Gemini gets called twice
- **Component quantity tracking**: footprints table exists but stock count UX is incomplete — no quick +/- buttons on the component list
- **Low stock alerts**: InfluxDB records stock changes but no alert threshold UI or Home Assistant webhook integration yet
- **BOM auto-match**: projects have BOM items but no automatic "do I have enough of this?" check against current stock
- **Label batch printing**: Avery 5267 sheet layout exists but needs calibration for different printers
- **KiCad BOM import**: planned but not implemented — would auto-create BOM items from KiCad netlist export
- **Barcode scanner UART**: Tera 5100 HID mode works; UART TX pad on P1 FPC still unidentified for future native integration
- **Image search quality**: DuckDuckGo vqd token extraction is fragile — consider a proper image API fallback
- **rembg cold start**: first background removal call downloads ~100MB model — add a pre-warm endpoint or startup task
