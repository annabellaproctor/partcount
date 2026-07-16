<img src="assets/logo.svg" alt="partcount" width="300" />

**Partcount** is a high-density, precision-engineered inventory management system built specifically for electronics labs, hardware startups, and maker spaces. It solves the friction of tracking tiny, high-quantity surface-mount and through-hole components across multiple storage boxes by prioritizing speed and keyboard-driven workflows. The UI is completely flat and responsive, ensuring that you spend less time clicking and more time building.

## Key Features

### 📦 Hierarchical Storage Management
- **Boxes & Cells:** Define physical storage boxes with highly customizable row and column dimensions. Map every single component to a specific coordinate cell.
- **Visual Minimap:** Instantly locate parts using a visual grid representation of your storage containers that highlights the exact cell a part is located in.
- **Stock Operations:** Rapidly take, put, and calibrate stock with a few clicks. The system maintains a complete event log of every transaction.

### 🛠️ Projects & Kits (BOMs)
- **Kits:** Construct complex product Bills of Materials from your existing components. Group components logically and define quantities required per kit.
- **Projects:** Track active builds, required quantities against current stock, and assembly statuses. Deduct entire kits from your inventory with a single action when a project is completed.
- **BOM Parsing:** Automatically extract component lists and quantities from raw text, datasheets, or PDFs using the integrated AI assistant.

### 🏷️ Labeling & Scanning
- **Built-in Designer:** A complete label designer interface for printing component and box labels directly from the browser. Supports ZPL and visual layouts.
- **Barcode Scanning:** Heavily optimized for hardware barcode scanners. Simply scan a part or box to instantly pull up its record and adjust inventory without touching the keyboard.

### 🤖 AI Assistant (Sparky)
- **Natural Language Input:** Paste messy distributor data or BOMs and let the assistant extract manufacturer part numbers (MPNs), descriptions, package sizes, and quantities.
- **Context-Aware:** Drop files, datasheets, or CSVs directly into the assistant to use as reference sources for complex queries about your inventory.

## Architecture & Tech Stack

Partcount is built on a high-performance, lightweight stack designed for low latency and zero-build complexity.

- **Backend:** 
  - **FastAPI:** Provides a blazing fast async REST API.
  - **SQLAlchemy:** Manages the relational data model for components, boxes, kits, and events.
  - **SQLite:** A robust, portable database that requires zero configuration.
- **Frontend:** 
  - **HTMX:** Powers dynamic HTML updates over the wire without a heavy SPA framework.
  - **Vanilla JS:** Used sparingly for keyboard shortcuts, drag-and-drop, and the AI assistant sidebar.
- **Styling:** 
  - **Custom CSS:** Built entirely with CSS variables for rapid iteration, custom themes, and a lightweight footprint.
- **Real-Time:** 
  - **WebSockets:** Broadcasts stock changes, scan events, and log updates to all connected clients instantly.

## Database Schema Overview

The database revolves around a few core models:
- `Component`: The central entity representing a part, including its MPN, footprint, value, and total stock.
- `Box`: Represents a physical storage container with specific grid dimensions.
- `Cell`: A specific location within a box containing a single component.
- `Kit` & `KitItem`: Represents a BOM and its constituent components.
- `Project`: An active assembly run tied to a Kit.
- `InventoryEvent`: An append-only ledger tracking all stock movements, ensuring full traceability.

## Getting Started

### Prerequisites
- Python 3.10+
- A modern web browser (Chrome, Firefox, Safari)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/annabellaproctor/partcount.git
   cd partcount
   ```

2. **Set up a virtual environment (optional but recommended):**
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Start the server:**
   ```bash
   uvicorn main:app --reload --port 8437
   ```

5. **Access the application:**
   Open your browser and navigate to `http://localhost:8437`. The SQLite database (`partcount.db`) will automatically initialize on your first run.

## API Integration

Partcount provides a full REST API for integrating with other tools in your hardware pipeline. You can manage components, query stock, and adjust inventory programmatically via Python scripts or CI/CD pipelines. Check the `/docs` endpoint on your local server for the interactive OpenAPI/Swagger UI.
