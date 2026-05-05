# JLCPCB MCP Server

[![Built with Claude Code](https://img.shields.io/badge/built%20with-Claude%20Code-blueviolet?logo=anthropic&logoColor=white)](https://claude.ai/code)

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for searching and retrieving JLCPCB assembly-library components (LCSC parts) — backed by the official JLCPCB API, with a local SQLite layer used purely as a short-lived cache.

Designed to complement AI-assisted PCB design workflows, particularly alongside [KiCAD-MCP-Server](https://github.com/mageoch/KiCAD-MCP-Server).

---

## Features

- **Parametric search** for resistors, capacitors, and inductors (value, package, tolerance, voltage, power rating, etc.)
- **Free-text search** across the JLCPCB assembly library (Basic + Extended)
- **API-first** — the local SQLite database is treated as a 6h / 24h cache, not as a source of truth (see [Cache model](#cache-model))
- **Auto-refresh** — membership and stock/price data are refreshed transparently before each search
- **Alternative suggestions** ranked by library type (Basic first), price, and stock
- **KiCAD component download** — fetch EasyEDA symbols, footprints, and 3D models directly from LCSC

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
git clone https://github.com/mageoch/JLCPCB-MCP-Server.git
cd JLCPCB-MCP-Server
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
| `JLCPCB_DB_PATH` | No | Custom SQLite DB path (default: `./data/lcsc_parts.db`) |
| `JLCPCB_CACHE_TTL_HOURS` | No | Per-row stock/price TTL in hours (default: `6`) |
| `JLCPCB_MEMBERSHIP_TTL_HOURS` | No | Library-membership TTL in hours (default: `24`) |

### Claude Code

Add to your project's `.mcp.json` or `~/.claude.json`:

```json
{
  "mcpServers": {
    "jlcpcb": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/JLCPCB-MCP-Server",
        "jlcpcb-mcp"
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

### First run

No manual setup required. The first search will trigger an automatic membership refresh of the JLCPCB assembly library (Basic + Extended — typically tens of thousands of parts; takes a couple of minutes). Subsequent searches reuse the cached membership for `JLCPCB_MEMBERSHIP_TTL_HOURS` (24h by default) and refresh per-row stock/price for the returned rows when they're older than `JLCPCB_CACHE_TTL_HOURS` (6h by default).

To force a full re-fetch (e.g. to refresh stock/price for the whole library at once), call `download_library` explicitly.

### Tools

| Tool | Description |
|------|-------------|
| `download_library` | Force-refresh the entire JLCPCB assembly library (Basic + Extended) |
| `search_parts` | Free-text and category search; auto-refreshes the cache |
| `search_resistors` | Parametric search (value, tolerance, power, package) |
| `search_capacitors` | Parametric search (value, voltage, dielectric, package) |
| `search_inductors` | Parametric search (value, current rating, package) |
| `get_part` | Full details for a part by LCSC code; refreshes if older than `JLCPCB_CACHE_TTL_HOURS` |
| `download_kicad_component` | Download EasyEDA symbol, footprint, and 3D model for a part |
| `suggest_alternatives` | Find cheaper or better-stocked alternatives |
| `get_stats` | Database statistics (parts count, stock, DB size, membership age) |
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
download_kicad_component("C25804", output="/path/to/project/libs/EasyEDA")
```

---

## Architecture

```
jlcpcb_mcp/
├── server.py    # FastMCP server — tool definitions and entry point
├── client.py    # JLCPCB API client (HMAC-SHA256 authentication)
└── db.py        # SQLite manager — import, FTS5, parametric spec extraction
```

- Uses [FastMCP](https://github.com/jlowin/fastmcp) for the MCP protocol layer
- Local SQLite with full-text search (FTS5) and a `component_specs` table for numeric range queries
- SI prefix parsing for all passive values (Ω, kΩ, MΩ, nF, µF, µH, mH, etc.)

### Cache model

The server treats the local SQLite database as a short-lived cache layered on top of the JLCPCB API, not as a source of truth:

1. **Membership cache (`JLCPCB_MEMBERSHIP_TTL_HOURS`, 24h default)** — tracked via `basic_library_refreshed_at`. When stale, the server paginates `getComponentLibraryList`, diffs the returned codes against the local DB, fetches full details for new codes via `getComponentDetailByCode` (batched up to 1000 per call), and deletes codes that are no longer part of the library.
2. **Per-row detail cache (`JLCPCB_CACHE_TTL_HOURS`, 6h default)** — tracked via `last_updated` on each row. After a search returns results, the rows whose timestamp is older than the TTL are refetched in a single batched API call before being returned to the caller — so the displayed stock and prices are at most a few seconds old in practice.

This means searches can occasionally block on a membership refresh (~30s for stub pagination + a few seconds per 1000 new codes), but day-to-day calls only pay for the small detail refresh of rows actually returned.

The JLCPCB API does not expose a delta or "modified-since" filter, so the membership pagination is necessarily full — but the `componentLibraryInfoVOS` payload is small (3 fields per stub) and the detail enrichment is restricted to codes the server doesn't already have, which makes the typical 24h refresh much cheaper than a full re-fetch.

---

## License

MIT — Copyright (c) 2026 mageo services Ltd. See [LICENSE](LICENSE).

Created and maintained by [@mageo](https://github.com/mageo).
