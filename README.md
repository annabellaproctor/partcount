<div align="center">
  <img src="assets/logo.svg" alt="partcount" width="180" />
</div>

<br/>

Partcount is an inventory management system for electronics components, kits, and projects. It is designed to be straightforward and fast to use.

## Features

- **Boxes & Cells:** Organize physical storage locations down to the individual grid cell.
- **Kits & Projects:** Group components together to build product bills of materials (BOMs).
- **Labels:** Built-in designer for printing component and box labels.
- **Scanner Support:** Quickly scan barcodes to update stock or locate parts.
- **Natural Language Parsing:** Paste a messy BOM or component description to automatically extract manufacturer part numbers and quantities.

## Tech Stack

- **Backend:** FastAPI, SQLAlchemy, SQLite
- **Frontend:** HTMX, vanilla JavaScript, basic CSS

## Setup

1. Clone the repository
2. `pip install -r requirements.txt`
3. `uvicorn main:app --reload --port 8437`
4. Open `http://localhost:8437`
