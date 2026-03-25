"""
LCSC MCP Server — official JLCPCB API only.

Required env vars:
  JLCPCB_APP_ID
  JLCPCB_API_KEY
  JLCPCB_API_SECRET

Optional:
  LCSC_DB_PATH   — path to SQLite DB (default: ./data/lcsc_parts.db)
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from .client import JLCPCBClient
from .db import PartsDB

_LOG_FILE = Path(__file__).parent.parent / "data" / "lcsc_mcp.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_FILE),
    ],
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "LCSC Parts",
    instructions=(
        "Search and retrieve LCSC/JLCPCB electronic components from the local database "
        "or live from the official JLCPCB API. "
        "Run download_database first to populate the local DB."
    ),
)

_CHECKPOINT = Path(__file__).parent.parent / "data" / "download_checkpoint.json"

CACHE_TTL_HOURS = float(os.getenv("LCSC_CACHE_TTL_HOURS", "24"))


def _db() -> PartsDB:
    return PartsDB(os.getenv("LCSC_DB_PATH"))


def _client() -> JLCPCBClient:
    return JLCPCBClient()  # raises EnvironmentError if credentials missing


def _ensure_basic_library(db: PartsDB) -> Optional[str]:
    """
    Auto-refresh the Basic library if it is stale (older than CACHE_TTL_HOURS).
    Returns a warning string if refresh was triggered, None otherwise.
    """
    if not db.is_library_stale(CACHE_TTL_HOURS):
        return None

    age = db.library_age_hours()
    if age is None:
        logger.info("Basic library not yet populated — fetching from API…")
    else:
        logger.info("Basic library is %.1f h old — refreshing…", age)

    try:
        client = _client()
        total = 0

        def on_batch(parts: list) -> None:
            nonlocal total
            total += db.import_batch(parts)

        client.download_library(library_type="base", on_batch=on_batch)
        db.rebuild_fts()
        db.rebuild_specs()
        import time as _time
        db.set_metadata("basic_library_refreshed_at", str(_time.time()))
        logger.info("Basic library refreshed: %d parts", total)
        return None
    except Exception as exc:
        logger.warning("Basic library auto-refresh failed: %s", exc)
        return f"Auto-refresh failed ({exc}); results may be stale."


# ---------------------------------------------------------------------------
# Tool: download_database
# ---------------------------------------------------------------------------

@mcp.tool()
def download_database(force: bool = False) -> dict:
    """
    Download the complete LCSC/JLCPCB parts catalog to the local SQLite database.

    Streams data directly to disk — no full catalog in RAM.
    Supports resume: if interrupted, re-running continues from the last checkpoint.
    Non-electronic categories (cables, tools, hardware…) are automatically excluded.

    Args:
        force: If True, clears the existing database and re-downloads from scratch.

    Returns:
        Download statistics (total parts, basic/extended counts, DB size).
    """
    db = _db()
    client = _client()

    checkpoint: Optional[dict] = None

    if force:
        db.clear()
        if _CHECKPOINT.exists():
            _CHECKPOINT.unlink()
    elif _CHECKPOINT.exists():
        try:
            checkpoint = json.loads(_CHECKPOINT.read_text())
            logger.info("Resuming from checkpoint: %s parts already imported", checkpoint.get("total", 0))
        except Exception:
            checkpoint = None

    def on_batch(parts: list) -> None:  # pragma: no cover — dead code, patched_on_batch is used instead
        inserted = db.import_batch(parts)
        # Update checkpoint after each successful batch
        ck = {"last_key": current_last_key[0], "total": total_counter[0]}
        _CHECKPOINT.write_text(json.dumps(ck))

    # Mutable containers so the closure can update them
    current_last_key: list = [checkpoint.get("last_key") if checkpoint else None]
    total_counter: list = [checkpoint.get("total", 0) if checkpoint else 0]

    def patched_on_batch(parts: list) -> None:
        db.import_batch(parts)

    def on_progress(total: int, msg: str) -> None:
        total_counter[0] = total
        logger.info(msg)

    try:
        total, last_key = client.download(
            on_batch=patched_on_batch,
            on_progress=on_progress,
            checkpoint=checkpoint,
        )
        current_last_key[0] = last_key

        # Rebuild FTS and parametric specs once at the end
        db.rebuild_fts()
        db.rebuild_specs()

        # Remove checkpoint on successful completion
        if _CHECKPOINT.exists():
            _CHECKPOINT.unlink()

        stats = db.stats()
        return {
            "success": True,
            "message": f"Download complete: {total} parts fetched",
            **stats,
        }

    except Exception as exc:
        logger.error("Download failed: %s", exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: download_library
# ---------------------------------------------------------------------------

@mcp.tool()
def download_library(library_type: str = "all") -> dict:
    """
    Download only the Basic and/or Extended assembly library (much faster than
    the full catalog — these are the parts JLCPCB stocks for SMT assembly).

    Args:
        library_type: "basic" (free assembly), "extended" ($3 setup fee each),
                      or "all" (both). Default: "all".

    Returns:
        Download statistics.
    """
    db = _db()
    client = _client()

    api_type: Optional[str] = None
    if library_type.lower() == "basic":
        api_type = "base"
    elif library_type.lower() == "extended":
        api_type = "extend"

    total = 0

    def on_batch(parts: list) -> None:
        nonlocal total
        total += db.import_batch(parts)

    try:
        client.download_library(library_type=api_type, on_batch=on_batch)
        db.rebuild_fts()
        db.rebuild_specs()
        import time as _time
        if api_type in (None, "base"):
            db.set_metadata("basic_library_refreshed_at", str(_time.time()))
        stats = db.stats()
        return {
            "success": True,
            "message": f"Library download complete: {total} parts imported",
            **stats,
        }
    except Exception as exc:
        logger.error("Library download failed: %s", exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: search_parts
# ---------------------------------------------------------------------------

@mcp.tool()
def search_parts(
    query: Optional[str] = None,
    category: Optional[str] = None,
    package: Optional[str] = None,
    library_type: Optional[str] = None,
    manufacturer: Optional[str] = None,
    in_stock: bool = True,
    limit: int = 20,
) -> dict:
    """
    Search the local LCSC parts database.

    Args:
        query: Free-text search (e.g. '10k resistor 0603', 'ESP32', 'LDO 3.3V').
        category: Filter by category or subcategory (e.g. 'Resistors', 'Capacitors').
        package: Filter by package (e.g. '0603', 'SOT-23', 'QFN-32').
        library_type: 'Basic', 'Extended', 'Preferred', or None for all.
        manufacturer: Filter by manufacturer name.
        in_stock: Only return parts with stock > 0. Default: True.
        limit: Maximum results. Default: 20.

    Returns:
        List of matching parts with LCSC code, description, price breaks, stock.
    """
    db = _db()
    warning = _ensure_basic_library(db)
    parts = db.search(
        query=query,
        category=category,
        package=package,
        library_type=library_type,
        manufacturer=manufacturer,
        in_stock=in_stock,
        limit=limit,
    )
    result: dict = {"success": True, "count": len(parts), "parts": parts}
    if warning:
        result["warning"] = warning
    return result


# ---------------------------------------------------------------------------
# Tool: search_resistors
# ---------------------------------------------------------------------------

@mcp.tool()
def search_resistors(
    value: Optional[str] = None,
    value_min_ohms: Optional[float] = None,
    value_max_ohms: Optional[float] = None,
    package: Optional[str] = None,
    tolerance: Optional[str] = None,
    tolerance_max_pct: Optional[float] = None,
    power_rating: Optional[str] = None,
    power_min_w: Optional[float] = None,
    library_type: Optional[str] = None,
    in_stock: bool = True,
    limit: int = 20,
) -> dict:
    """
    Search resistors in the local LCSC database with parametric filters.

    Text filters use full-text search against JLCPCB descriptions.
    Numeric range filters use the extracted component_specs table (run
    rebuild_component_specs first if the DB was downloaded before this feature).

    Args:
        value: Resistance as text, e.g. '10kΩ', '100R', '4.7k'.
        value_min_ohms: Minimum resistance in Ohms, e.g. 1000 for 1 kΩ.
        value_max_ohms: Maximum resistance in Ohms, e.g. 100000 for 100 kΩ.
        package: SMD/THT package, e.g. '0402', '0603', '0805', 'AXIAL'.
        tolerance: Tolerance as text, e.g. '±1%', '±5%'.
        tolerance_max_pct: Maximum tolerance in percent, e.g. 1.0 for ±1 % or better.
        power_rating: Power rating as text, e.g. '1/10W', '1/4W'.
        power_min_w: Minimum power rating in Watts, e.g. 0.25 for 1/4 W.
        library_type: 'Basic', 'Extended', 'Preferred', or None for all.
        in_stock: Only return parts with stock > 0. Default: True.
        limit: Maximum results. Default: 20.

    Returns:
        List of matching resistors with LCSC code, description, package, price, stock.
    """
    db = _db()
    warning = _ensure_basic_library(db)
    parts = db.search_passive(
        component_type="resistor",
        value=value,
        value_min=value_min_ohms,
        value_max=value_max_ohms,
        package=package,
        tolerance=tolerance,
        tolerance_max_pct=tolerance_max_pct,
        power_rating=power_rating,
        power_min_w=power_min_w,
        library_type=library_type,
        in_stock=in_stock,
        limit=limit,
    )
    result: dict = {"success": True, "count": len(parts), "parts": parts}
    if warning:
        result["warning"] = warning
    return result


# ---------------------------------------------------------------------------
# Tool: search_capacitors
# ---------------------------------------------------------------------------

@mcp.tool()
def search_capacitors(
    value: Optional[str] = None,
    value_min_farads: Optional[float] = None,
    value_max_farads: Optional[float] = None,
    package: Optional[str] = None,
    voltage_rating: Optional[str] = None,
    voltage_min_v: Optional[float] = None,
    dielectric: Optional[str] = None,
    tolerance: Optional[str] = None,
    library_type: Optional[str] = None,
    in_stock: bool = True,
    limit: int = 20,
) -> dict:
    """
    Search capacitors in the local LCSC database with parametric filters.

    Text filters use full-text search against JLCPCB descriptions.
    Numeric range filters use the extracted component_specs table (run
    rebuild_component_specs first if the DB was downloaded before this feature).

    Args:
        value: Capacitance as text, e.g. '100nF', '10µF', '0.1uF', '1pF'.
        value_min_farads: Minimum capacitance in Farads, e.g. 100e-9 for 100 nF.
        value_max_farads: Maximum capacitance in Farads.
        package: SMD/THT package, e.g. '0402', '0603', '0805'.
        voltage_rating: Voltage as text, e.g. '50V', '100V'.
        voltage_min_v: Minimum voltage rating in Volts, e.g. 50.0.
        dielectric: Dielectric type, e.g. 'X5R', 'X7R', 'C0G', 'NP0', 'Y5V'.
        tolerance: Tolerance as text, e.g. '±10%', '±20%'.
        library_type: 'Basic', 'Extended', 'Preferred', or None for all.
        in_stock: Only return parts with stock > 0. Default: True.
        limit: Maximum results. Default: 20.

    Returns:
        List of matching capacitors with LCSC code, description, package, price, stock.
    """
    db = _db()
    warning = _ensure_basic_library(db)
    parts = db.search_passive(
        component_type="capacitor",
        value=value,
        value_min=value_min_farads,
        value_max=value_max_farads,
        package=package,
        tolerance=tolerance,
        voltage_rating=voltage_rating,
        voltage_min_v=voltage_min_v,
        dielectric=dielectric,
        library_type=library_type,
        in_stock=in_stock,
        limit=limit,
    )
    result: dict = {"success": True, "count": len(parts), "parts": parts}
    if warning:
        result["warning"] = warning
    return result


# ---------------------------------------------------------------------------
# Tool: search_inductors
# ---------------------------------------------------------------------------

@mcp.tool()
def search_inductors(
    value: Optional[str] = None,
    value_min_henries: Optional[float] = None,
    value_max_henries: Optional[float] = None,
    package: Optional[str] = None,
    current_rating: Optional[str] = None,
    current_min_a: Optional[float] = None,
    tolerance: Optional[str] = None,
    library_type: Optional[str] = None,
    in_stock: bool = True,
    limit: int = 20,
) -> dict:
    """
    Search inductors (and ferrite beads) in the local LCSC database with parametric filters.

    Text filters use full-text search against JLCPCB descriptions.
    Numeric range filters use the extracted component_specs table (run
    rebuild_component_specs first if the DB was downloaded before this feature).

    Args:
        value: Inductance as text, e.g. '10µH', '100nH', '4.7uH'.
        value_min_henries: Minimum inductance in Henries, e.g. 10e-6 for 10 µH.
        value_max_henries: Maximum inductance in Henries.
        package: SMD/THT package, e.g. '0402', '0603', '0805', 'CD43'.
        current_rating: Rated current as text, e.g. '100mA', '1A', '500mA'.
        current_min_a: Minimum current rating in Amperes, e.g. 1.0 for 1 A.
        tolerance: Tolerance as text, e.g. '±10%', '±20%'.
        library_type: 'Basic', 'Extended', 'Preferred', or None for all.
        in_stock: Only return parts with stock > 0. Default: True.
        limit: Maximum results. Default: 20.

    Returns:
        List of matching inductors with LCSC code, description, package, price, stock.
    """
    db = _db()
    warning = _ensure_basic_library(db)
    parts = db.search_passive(
        component_type="inductor",
        value=value,
        value_min=value_min_henries,
        value_max=value_max_henries,
        package=package,
        tolerance=tolerance,
        current_rating=current_rating,
        current_min_a=current_min_a,
        library_type=library_type,
        in_stock=in_stock,
        limit=limit,
    )
    result: dict = {"success": True, "count": len(parts), "parts": parts}
    if warning:
        result["warning"] = warning
    return result


# ---------------------------------------------------------------------------
# Tool: rebuild_component_specs
# ---------------------------------------------------------------------------

@mcp.tool()
def rebuild_component_specs() -> dict:
    """
    (Re)extract parametric specs for all passives already in the local database.

    Parses every component description to populate the component_specs table
    with structured numeric data (resistance/capacitance/inductance value,
    voltage, current, power, tolerance, dielectric type).

    Run this once after downloading the database for the first time, or after
    a server upgrade that added the component_specs table.

    Returns:
        Number of passive components indexed.
    """
    db = _db()
    try:
        count = db.rebuild_specs()
        return {"success": True, "passives_indexed": count}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: get_part
# ---------------------------------------------------------------------------

@mcp.tool()
def get_part(lcsc_code: str, live: bool = False) -> dict:
    """
    Get detailed information about a component by its LCSC code.

    Checks the local database first. If not found or if live=True, queries
    the official JLCPCB API directly (getComponentDetailByCode endpoint).

    Args:
        lcsc_code: LCSC part number, e.g. 'C25804'.
        live: Force a live API lookup even if the part exists in the local DB.

    Returns:
        Component details including description, package, stock, price breaks, datasheet.
    """
    db = _db()

    if not live:
        age = db.part_age_hours(lcsc_code)
        if age is not None and age < CACHE_TTL_HOURS:
            part = db.get(lcsc_code)
            if part:
                return {"success": True, "source": "local_db", "part": part}

    # Live API lookup (cache miss, stale, or live=True)
    try:
        client = _client()
        raw = client.get_part_detail(lcsc_code)
        if not raw:
            # Fall back to stale cache rather than returning nothing
            part = db.get(lcsc_code)
            if part:
                return {"success": True, "source": "local_db_stale", "part": part}
            return {"success": False, "error": f"Part {lcsc_code} not found via API"}

        # Upsert into local DB for future queries
        db.import_batch([raw])

        part = db.get(lcsc_code) or raw
        return {"success": True, "source": "api", "part": part}

    except Exception as exc:
        # Network error — fall back to stale cache if available
        part = db.get(lcsc_code)
        if part:
            logger.warning("API error for %s, serving stale cache: %s", lcsc_code, exc)
            return {"success": True, "source": "local_db_stale", "part": part}
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: suggest_alternatives
# ---------------------------------------------------------------------------

@mcp.tool()
def suggest_alternatives(lcsc_code: str, limit: int = 5) -> dict:
    """
    Suggest alternative components for a given LCSC part.

    Finds parts in the same category and package, ranked by:
    1. Library type (Basic first — free assembly at JLCPCB)
    2. Unit price (cheapest first)
    3. Stock availability (highest first)

    Args:
        lcsc_code: Reference LCSC part number, e.g. 'C25804'.
        limit: Maximum number of alternatives. Default: 5.

    Returns:
        List of alternative parts with comparison data.
    """
    db = _db()
    ref = db.get(lcsc_code)
    if not ref:
        return {"success": False, "error": f"Part {lcsc_code} not found in local database"}

    alternatives = db.suggest_alternatives(lcsc_code, limit=limit)

    ref_price = None
    if ref.get("price_breaks"):
        ref_price = ref["price_breaks"][0]["price"]

    return {
        "success": True,
        "reference": {
            "lcsc": lcsc_code,
            "library_type": ref.get("library_type"),
            "price": ref_price,
            "stock": ref.get("stock"),
        },
        "alternatives": alternatives,
    }


# ---------------------------------------------------------------------------
# Tool: get_stats
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> dict:
    """
    Return statistics about the local LCSC parts database.

    Returns:
        Total parts, basic/extended/preferred counts, in-stock count, DB file info.
    """
    try:
        db = _db()
        return {"success": True, **db.stats()}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: download_kicad_component
# ---------------------------------------------------------------------------

@mcp.tool()
def download_kicad_component(
    lcsc_id: str,
    output: Optional[str] = None,
    symbol: bool = True,
    footprint: bool = True,
    model_3d: bool = True,
    overwrite: bool = True,
    model_3d_path: Optional[str] = None,
) -> dict:
    """
    Download KiCAD symbol, footprint, and/or 3D model for an LCSC component.

    Uses the public EasyEDA Pro API (no credentials required) to fetch component
    data and convert it to KiCAD format.

    Args:
        lcsc_id: LCSC part number, e.g. 'C25117'.
        output: Base path for the library files, without extension.
                Example: '/path/to/hardware/libs/easyeda/EasyEDA'
                  → symbol  : {output}.kicad_sym
                  → footprint: {output}.pretty/{name}.kicad_mod
                  → 3D model : {output}.3dshapes/{name}.wrl/.step
                Defaults to LCSC_EASYEDA_LIB_PATH env var, then './EasyEDA'.
        symbol: Download and add symbol to .kicad_sym library. Default: True.
        footprint: Download and add footprint to .pretty directory. Default: True.
        model_3d: Download 3D model (.wrl/.step) to .3dshapes directory. Default: True.
        overwrite: Replace the component if it already exists. Default: True.
        model_3d_path: Path embedded in the footprint for 3D model references.
                       Supports KiCAD variables, e.g. '${KICAD_3RD_PARTY}/EasyEDA.3dshapes'.
                       Defaults to LCSC_EASYEDA_3D_PATH env var, then
                       '${KICAD_3RD_PARTY}/EasyEDA.3dshapes'.

    Returns:
        dict with success status, component name, and created file paths.
    """
    try:
        from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
        from easyeda2kicad.easyeda.easyeda_importer import (
            Easyeda3dModelImporter,
            EasyedaFootprintImporter,
            EasyedaSymbolImporter,
        )
        from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
        from easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
        from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad
        from easyeda2kicad.kicad.parameters_kicad_symbol import KicadVersion
        from easyeda2kicad.__main__ import (
            add_component_in_symbol_lib_file,
            fp_already_in_footprint_lib,
            id_already_in_symbol_lib,
            update_component_in_symbol_lib_file,
        )
    except ImportError as exc:
        return {"success": False, "error": f"easyeda2kicad not installed: {exc}"}

    # Resolve output base path
    lib_base = output or os.getenv("LCSC_EASYEDA_LIB_PATH") or "./EasyEDA"
    lib_base = lib_base.rstrip("/")

    # Resolve 3D model path embedded in footprints
    embedded_3d_path = (
        model_3d_path
        or os.getenv("LCSC_EASYEDA_3D_PATH")
        or "${KICAD_3RD_PARTY}/EasyEDA.3dshapes"
    )

    # Fetch component data from EasyEDA
    api = EasyedaApi()
    cad_data = api.get_cad_data_of_component(lcsc_id=lcsc_id)
    if not cad_data:
        return {"success": False, "error": f"No EasyEDA data found for {lcsc_id}"}

    result: dict = {"success": True, "lcsc_id": lcsc_id, "files": {}}

    # ---- Symbol ----
    if symbol:
        try:
            sym_importer = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data)
            ee_symbol = sym_importer.get_symbol()
            sym_lib_path = f"{lib_base}.kicad_sym"
            footprint_lib_name = Path(lib_base).name

            # Create an empty .kicad_sym library if it doesn't exist yet
            sym_lib_file = Path(sym_lib_path)
            sym_lib_file.parent.mkdir(parents=True, exist_ok=True)
            if not sym_lib_file.exists():
                sym_lib_file.write_text(
                    "(kicad_symbol_lib (version 20220914) (generator kicad_symbol_editor)\n)\n",
                    encoding="utf-8",
                )

            already_in_lib = id_already_in_symbol_lib(
                lib_path=sym_lib_path,
                component_name=ee_symbol.info.name,
                kicad_version=KicadVersion.v6,
            )

            exporter = ExporterSymbolKicad(
                symbol=ee_symbol, kicad_version=KicadVersion.v6
            )
            kicad_symbol_lib = exporter.export(footprint_lib_name=footprint_lib_name)

            if already_in_lib:
                if not overwrite:
                    result["files"]["symbol"] = {"skipped": True, "path": sym_lib_path}
                else:
                    update_component_in_symbol_lib_file(
                        lib_path=sym_lib_path,
                        component_name=ee_symbol.info.name,
                        component_content=kicad_symbol_lib,
                        kicad_version=KicadVersion.v6,
                    )
                    result["files"]["symbol"] = {"updated": True, "path": sym_lib_path, "name": ee_symbol.info.name}
            else:
                add_component_in_symbol_lib_file(
                    lib_path=sym_lib_path,
                    component_content=kicad_symbol_lib,
                    kicad_version=KicadVersion.v6,
                )
                result["files"]["symbol"] = {"created": True, "path": sym_lib_path, "name": ee_symbol.info.name}

        except Exception as exc:
            result["files"]["symbol"] = {"error": str(exc)}

    # ---- Footprint ----
    if footprint:
        try:
            fp_importer = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data)
            ee_footprint = fp_importer.get_footprint()
            fp_dir = f"{lib_base}.pretty"
            fp_filename = f"{ee_footprint.info.name}.kicad_mod"
            fp_full_path = f"{fp_dir}/{fp_filename}"

            already_in_lib = fp_already_in_footprint_lib(
                lib_path=fp_dir,
                package_name=ee_footprint.info.name,
            )

            if already_in_lib and not overwrite:
                result["files"]["footprint"] = {"skipped": True, "path": fp_full_path}
            else:
                Path(fp_dir).mkdir(parents=True, exist_ok=True)
                ki_footprint = ExporterFootprintKicad(footprint=ee_footprint)
                ki_footprint.export(
                    footprint_full_path=fp_full_path,
                    model_3d_path=embedded_3d_path,
                )
                action = "updated" if already_in_lib else "created"
                result["files"]["footprint"] = {action: True, "path": fp_full_path, "name": ee_footprint.info.name}

        except Exception as exc:
            result["files"]["footprint"] = {"error": str(exc)}

    # ---- 3D model ----
    if model_3d:
        try:
            model_importer = Easyeda3dModelImporter(
                easyeda_cp_cad_data=cad_data, download_raw_3d_model=True
            )
            model_exporter = Exporter3dModelKicad(model_3d=model_importer.output)
            Path(f"{lib_base}.3dshapes").mkdir(parents=True, exist_ok=True)
            model_exporter.export(lib_path=lib_base)

            shapes_dir = f"{lib_base}.3dshapes"
            files_created = []
            if model_exporter.output:
                files_created.append(f"{shapes_dir}/{model_exporter.output.name}.wrl")
            if model_exporter.output_step:
                files_created.append(f"{shapes_dir}/{model_exporter.output.name}.step")

            if files_created:
                result["files"]["model_3d"] = {"created": True, "paths": files_created}
            else:
                result["files"]["model_3d"] = {"skipped": True, "reason": "no 3D model available"}

        except Exception as exc:
            result["files"]["model_3d"] = {"error": str(exc)}

    return result


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
