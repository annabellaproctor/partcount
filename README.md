<img src="assets/logo.svg" alt="partcount" width="180" />

<br/>

**Partcount** is a precision-engineered inventory management system built specifically for electronics labs, hardware startups, and maker spaces. It tracks components, active projects, bills of materials (BOMs), and physical storage down to the individual grid cell.

## Core Philosophy

Partcount is designed to solve the friction of tracking tiny, high-quantity items across multiple storage boxes. It prioritizes speed, data density, and keyboard-driven workflows. The UI is completely flat and responsive, ensuring that you spend less time clicking and more time building.

## Key Features

### 📦 Hierarchical Storage Management
- **Boxes & Cells:** Define physical storage boxes with highly customizable dimensions. Map every single component to a specific cell.
- **Visual Minimap:** Instantly locate parts using a visual grid representation of your storage containers.
- **Stock Operations:** Rapidly take, put, and calibrate stock with a few clicks.

### 🛠️ Projects & Kits (BOMs)
- **Kits:** Construct complex product Bills of Materials from your existing components.
- **Projects:** Track active builds, required quantities, and assembly statuses.
- **BOM Parsing:** Automatically extract component lists and quantities from raw text or PDFs using the integrated AI assistant.

### 🏷️ Labeling & Scanning
- **Built-in Designer:** A complete label designer interface for printing component and box labels directly from the browser.
- **Barcode Scanning:** Optimized for hardware barcode scanners. Simply scan a part or box to instantly pull up its record and adjust inventory.

### 🤖 AI Assistant (Sparky)
- **Natural Language Input:** Paste messy distributor data or BOMs and let the assistant extract manufacturer part numbers (MPNs), descriptions, and quantities.
- **Context-Aware:** Drop files, datasheets, or CSVs directly into the assistant to use as reference sources for queries.

## Architecture

Partcount is built on a high-performance stack designed for low latency and zero-build complexity:

- **Backend:** FastAPI (Python), SQLAlchemy, and an SQLite database for robust, portable data storage.
- **Frontend:** HTMX combined with Vanilla JavaScript for dynamic interactions without a heavy frontend framework.
- **Styling:** Custom CSS with CSS variables, allowing for rapid iteration and a lightweight footprint.
- **WebSocket Integration:** Real-time event logging and updates for stock changes and scans across multiple clients.

## Getting Started

### Prerequisites
- Python 3.10+
- A modern web browser

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/annabellaproctor/partcount.git
   cd partcount
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start the server:**
   ```bash
   uvicorn main:app --reload --port 8437
   ```

4. **Access the application:**
   Open your browser and navigate to `http://localhost:8437`. The database will automatically initialize on your first run.

## API Integration

Partcount provides a full REST API for integrating with other tools in your hardware pipeline. You can manage components, query stock, and adjust inventory programmatically. Check the `/docs` endpoint on your local server for the interactive Swagger UI.
