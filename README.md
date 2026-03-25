# LCSC MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for searching and retrieving LCSC/JLCPCB electronic components — using the official JLCPCB API and a local SQLite cache for fast offline queries.

Designed to complement AI-assisted PCB design workflows, particularly alongside [KiCAD-MCP-Server](https://github.com/mageoch/KiCAD-MCP-Server).

---

## Features

- **Parametric search** for resistors, capacitors, and inductors (value, package, tolerance, voltage, power rating, etc.)
- **Free-text search** across 2.5M+ parts in the JLCPCB catalog
- **Local SQLite cache** for fast offline queries — downloaded once, refreshed on demand
- **Live API fallback** for real-time pricing and stock data
- **Alternative suggestions** ranked by library type (Basic first), price, and stock
- **KiCAD component download** — fetch EasyEDA symbols, footprints, and 3D models directly from LCSC
- **JLCPCB Assembly library** support — Basic (no surcharge) and Extended components

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- JLCPCB API credentials (`JLCPCB_APP_ID`, `JLCPCB_API_KEY`, `JLCPCB_API_SECRET`)

### Getting API credentials

Register at the [JLCPCB Developer Portal](https://jlcpcb.com/developer) to obtain your credentials.

---

## Installation

```bash
git clone https://github.com/mageoch/LCSC-MCP-Server.git
cd LCSC-MCP-Server
uv sync
```

---

## Configuration

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `JLCPCB_APP_ID` | Yes | Your JLCPCB application ID |
| `JLCPCB_API_KEY` | Yes | Your JLCPCB API key |
| `JLCPCB_API_SECRET` | Yes | Your JLCPCB API secret |
| `LCSC_DB_PATH` | No | Custom SQLite DB path (default: `./data/lcsc_parts.db`) |
| `LCSC_CACHE_TTL_HOURS` | No | Cache TTL in hours (default: `24`) |

### Claude Code

Add to your project's `.mcp.json` or `~/.claude.json`:

```json
{
  "mcpServers": {
    "lcsc": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/LCSC-MCP-Server",
        "lcsc-mcp"
      ],
      "env": {
        "JLCPCB_APP_ID": "your_app_id",
        "JLCPCB_API_KEY": "your_api_key",
        "JLCPCB_API_SECRET": "your_api_secret"
      }
    }
  }
}
```

### Claude Desktop

Edit `~/.config/Claude/claude_desktop_config.json` (Linux/macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) — use the same format as above.

---

## Usage

### First run — populate the database

The local database must be populated before you can search. Two options:

**Assembly library only** (~30k parts, fast — recommended to start):
```
download_library(library_type="all")
```

**Full catalog** (2.5M+ parts, takes longer — supports resume if interrupted):
```
download_database()
```

### Tools

| Tool | Description |
|------|-------------|
| `download_database` | Download the full LCSC catalog to local DB (supports resume) |
| `download_library` | Download JLCPCB assembly library (Basic / Extended / all) |
| `search_parts` | Free-text and category search across all parts |
| `search_resistors` | Parametric search (value, tolerance, power, package) |
| `search_capacitors` | Parametric search (value, voltage, dielectric, package) |
| `search_inductors` | Parametric search (value, current rating, package) |
| `get_part` | Full details for a part by LCSC code |
| `download_kicad_component` | Download EasyEDA symbol, footprint, and 3D model for a part |
| `suggest_alternatives` | Find cheaper or better-stocked alternatives |
| `get_stats` | Database statistics (parts count, stock, DB size) |
| `rebuild_component_specs` | Re-extract parametric specs (run after a DB upgrade) |

### Example queries

```python
# Find a Basic 10kΩ 0402 resistor
search_resistors(value="10k", package="0402", library_type="Basic")

# Find 100nF X7R capacitors rated 50V or more
search_capacitors(value="100nF", dielectric="X7R", voltage_min_v=50)

# Look up a specific part
get_part("C25804")

# Find cheaper alternatives
suggest_alternatives("C25804")

# Download KiCAD files for a component
download_kicad_component("C25804", output_dir="/path/to/project/libs")
```

---

## Architecture

```
lcsc_mcp/
├── server.py    # FastMCP server — tool definitions and entry point
├── client.py    # JLCPCB API client (HMAC-SHA256 authentication)
└── db.py        # SQLite manager — import, FTS5, parametric spec extraction
```

- Uses [FastMCP](https://github.com/jlowin/fastmcp) for the MCP protocol layer
- Local SQLite with full-text search (FTS5) and a `component_specs` table for numeric range queries
- SI prefix parsing for all passive values (Ω, kΩ, MΩ, nF, µF, µH, mH, etc.)
- Batch streaming import with checkpoint/resume support for large downloads

---

## License

MIT — Copyright (c) 2026 mageo services Ltd. See [LICENSE](LICENSE).

Created and maintained by [@mageo](https://github.com/mageo).
