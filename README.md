# LCSC MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for searching and retrieving LCSC/JLCPCB electronic components — using the official JLCPCB API and a local SQLite cache for fast offline queries.

Designed to complement AI-assisted PCB design workflows, particularly alongside [KiCAD-MCP-Server](https://github.com/mageoch/KiCAD-MCP-Server).

## Features

- **Parametric search** for resistors, capacitors, and inductors (value, package, tolerance, voltage, power, etc.)
- **Free-text search** across 2.5M+ parts in the JLCPCB catalog
- **Local SQLite cache** for fast offline queries — downloaded once, refreshed on demand
- **Live API fallback** for real-time pricing and stock data
- **Alternative suggestions** ranked by library type (Basic first), price, and stock
- **JLCPCB Assembly library** support — Basic (free) and Extended components

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- JLCPCB API credentials (`JLCPCB_APP_ID`, `JLCPCB_API_KEY`, `JLCPCB_API_SECRET`)

### Getting API credentials

Register at the [JLCPCB Developer Portal](https://jlcpcb.com/developer) to obtain your API credentials.

## Installation

```bash
git clone https://github.com/mageoch/LCSC-MCP-Server.git
cd LCSC-MCP-Server
uv sync
```

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `JLCPCB_APP_ID` | Yes | Your JLCPCB application ID |
| `JLCPCB_API_KEY` | Yes | Your JLCPCB API key |
| `JLCPCB_API_SECRET` | Yes | Your JLCPCB API secret |
| `LCSC_DB_PATH` | No | Custom SQLite DB path (default: `./data/lcsc_parts.db`) |
| `LCSC_CACHE_TTL_HOURS` | No | Cache TTL in hours (default: `24`) |

### Claude Desktop / Claude Code

Add to your `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`) or `.mcp.json`:

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

## Usage

### First run — populate the database

Before searching, download the JLCPCB assembly library:

```
download_library(library_type="all")   # Downloads Basic + Extended (~30k parts, fast)
```

Or download the full catalog (2.5M+ parts, takes longer but supports all searches):

```
download_database()   # Supports resume if interrupted
```

### Tools

| Tool | Description |
|---|---|
| `download_database` | Download the full LCSC catalog to local DB (supports resume) |
| `download_library` | Download JLCPCB assembly library only (Basic / Extended / all) |
| `search_parts` | Free-text and category search across all parts |
| `search_resistors` | Parametric resistor search (value, tolerance, power, package) |
| `search_capacitors` | Parametric capacitor search (value, voltage, dielectric, package) |
| `search_inductors` | Parametric inductor search (value, current rating, package) |
| `get_part` | Get full details for a part by LCSC code |
| `suggest_alternatives` | Find cheaper/better-stocked alternatives to a given part |
| `get_stats` | Database statistics (total parts, stock, DB size) |
| `rebuild_component_specs` | Re-extract parametric specs (run after DB upgrade) |

### Example queries

```
# Find a Basic 10kΩ 0402 resistor
search_resistors(value="10k", package="0402", library_type="Basic")

# Find 100nF X7R capacitors rated for 50V or more
search_capacitors(value="100nF", dielectric="X7R", voltage_min_v=50)

# Look up a specific part
get_part("C25804")

# Find cheaper alternatives for a part
suggest_alternatives("C25804")
```

## Architecture

```
lcsc_mcp/
├── server.py     # FastMCP server — tool definitions and entrypoint
├── client.py     # JLCPCB API client (HMAC-SHA256 authentication)
└── db.py         # SQLite manager — import, FTS, parametric specs extraction
```

- Uses [FastMCP](https://github.com/jlowin/fastmcp) for the MCP protocol layer
- Local SQLite database with full-text search (FTS5) and a `component_specs` table for numeric range queries
- SI prefix parsing for all passive values (Ω, kΩ, MΩ, nF, µF, µH, mH, etc.)
- Batch streaming import with checkpoint/resume support for large downloads

## License

MIT — Copyright (c) 2026 mageo services Ltd. See [LICENSE](LICENSE).
