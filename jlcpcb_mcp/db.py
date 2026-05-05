"""
SQLite parts database manager.

Schema is compatible with the KiCAD-MCP-Server database so the file
can be shared between both servers.
"""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Passive component value parsers
# ---------------------------------------------------------------------------

_SI: dict[str, float] = {
    'p': 1e-12, 'n': 1e-9,
    'u': 1e-6,  'µ': 1e-6, 'μ': 1e-6,
    'm': 1e-3,
    'k': 1e3,   'K': 1e3,
    'M': 1e6,
    'G': 1e9,
}


def _fv(s: str) -> float:
    return float(s.replace(',', '.'))


def _resistance_ohms(desc: str) -> Optional[float]:
    """Parse resistance in Ohms. Handles 10kΩ, 4.7MΩ, 100R, 0Ω, 10K, 100mΩ."""
    m = re.search(
        r'(?<!\w)(\d+(?:[.,]\d+)?)\s*([pnuµμmkKMG]?)\s*(?:Ω|ohm|R)(?=\s|±|%|/|$)',
        desc, re.IGNORECASE,
    )
    if m:
        return _fv(m.group(1)) * _SI.get(m.group(2), 1.0)
    # Fallback: bare "10K" / "4.7M" without unit symbol (resistor context only)
    m = re.search(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*([kKMG])(?=\s|±|%|/|$)', desc)
    if m:
        return _fv(m.group(1)) * _SI.get(m.group(2), 1.0)
    return None


def _capacitance_farads(desc: str) -> Optional[float]:
    """Parse capacitance in Farads. Handles 100nF, 10µF, 0.1uF, 1pF, 10mF."""
    m = re.search(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*([pnuµμmM]?)\s*F(?=\s|±|%|/|$)', desc)
    if not m:
        return None
    prefix = m.group(2)
    # In capacitance, M = milli (not Mega)
    mult = 1e-3 if prefix == 'M' else _SI.get(prefix, 1.0)
    return _fv(m.group(1)) * mult


def _inductance_henries(desc: str) -> Optional[float]:
    """Parse inductance in Henries. Handles 10µH, 100nH, 4.7uH, 1mH."""
    m = re.search(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*([pnuµμmM]?)\s*H(?=\s|±|%|/|$)', desc)
    if not m:
        return None
    prefix = m.group(2)
    mult = 1e-3 if prefix == 'M' else _SI.get(prefix, 1.0)
    return _fv(m.group(1)) * mult


def _voltage_v(desc: str) -> Optional[float]:
    """Parse voltage rating in Volts. Handles 50V, 3.3V, 1kV."""
    m = re.search(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*([kK]?)\s*V(?:DC|AC)?(?=\s|±|%|/|$)', desc)
    if not m:
        return None
    return _fv(m.group(1)) * (_SI.get(m.group(2), 1.0))


def _current_a(desc: str) -> Optional[float]:
    """Parse current rating in Amperes. Handles 100mA, 1A, 2.5A."""
    m = re.search(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*([mMkK]?)\s*A(?=\s|±|%|/|$)', desc)
    if not m:
        return None
    return _fv(m.group(1)) * _SI.get(m.group(2), 1.0)


def _power_w(desc: str) -> Optional[float]:
    """Parse power rating in Watts. Handles 1/10W, 1/4W, 0.1W, 100mW."""
    m = re.search(r'(\d+)/(\d+)\s*W(?=\s|±|%|$)', desc)
    if m:
        return float(m.group(1)) / float(m.group(2))
    m = re.search(r'(?<!\w)(\d+(?:[.,]\d+)?)\s*([mMkK]?)\s*W(?=\s|±|%|/|$)', desc)
    if not m:
        return None
    return _fv(m.group(1)) * _SI.get(m.group(2), 1.0)


def _tolerance_pct(desc: str) -> Optional[float]:
    """Parse tolerance in percent. Handles ±1%, ±0.5%."""
    m = re.search(r'[±]\s*(\d+(?:[.,]\d+)?)\s*%', desc)
    return _fv(m.group(1)) if m else None


_RE_DIELECTRIC = re.compile(
    r'\b(C0G|NP0|X[4-8][RVSU]|Y[345][VU]|Z[456]U|N150|N470|N750|U2J)\b',
    re.IGNORECASE,
)


def _dielectric(desc: str) -> Optional[str]:
    """Extract ceramic capacitor dielectric type."""
    m = _RE_DIELECTRIC.search(desc)
    return m.group(1).upper() if m else None


# EIA tolerance letter → percent
_EIA_TOL: dict[str, float] = {
    'B': 0.1, 'C': 0.25, 'D': 0.5, 'F': 1.0,
    'G': 2.0, 'J': 5.0, 'K': 10.0, 'M': 20.0,
}

# Matches UNI-ROYAL / YAGEO part numbers with embedded EIA resistance code:
#   0402WGF1002TCE  → group(1)="1002" (4-digit EIA), group(2)=None
#   0402WGF330JTCE  → group(1)="330",  group(2)="J"  (3-digit + tolerance)
#   0603WAF4702T5E  → group(1)="4702" (4-digit EIA)
_RE_EIA_RES = re.compile(
    r'(?:WGF|WAF|WGJ|WGI|WMF|WMJ)(\d{3,4})([BCDFGJKM])?T',
    re.IGNORECASE,
)


def _resistance_ohms_from_mfr(mfr_part: str) -> Optional[float]:
    """Parse resistance in Ohms from an EIA-coded manufacturer part number.

    Handles UNI-ROYAL / YAGEO 0402WGF / 0603WAF series:
    - 4-digit EIA: '1002' → 100 × 10² = 10 kΩ
    - 3-digit + tolerance letter: '330J' → 33 × 10⁰ = 33 Ω
    """
    if not mfr_part:
        return None
    m = _RE_EIA_RES.search(mfr_part)
    if not m:
        return None
    code, tol_letter = m.group(1), m.group(2)
    if len(code) == 4:
        # 4-digit EIA: first 3 significant, last is multiplier exponent
        mantissa = int(code[:3])
        exp = int(code[3])
    else:
        # 3-digit EIA: first 2 significant, last is multiplier exponent
        mantissa = int(code[:2])
        exp = int(code[2])
    return mantissa * (10 ** exp)


def _tolerance_pct_from_mfr(mfr_part: str) -> Optional[float]:
    """Extract tolerance from EIA tolerance letter in manufacturer part number."""
    if not mfr_part:
        return None
    m = _RE_EIA_RES.search(mfr_part)
    if not m or not m.group(2):
        return None
    return _EIA_TOL.get(m.group(2).upper())


def _component_type(category: str, subcategory: str) -> Optional[str]:
    """Detect passive component type from category strings."""
    combined = f"{category or ''} {subcategory or ''}".lower()
    if 'resistor' in combined:
        return 'resistor'
    if 'capacitor' in combined:
        return 'capacitor'
    if 'inductor' in combined or 'coil' in combined or 'choke' in combined:
        return 'inductor'
    if 'ferrite' in combined:
        return 'ferrite'
    return None


def _extract_specs(
    lcsc: str,
    description: str,
    category: str,
    subcategory: str,
    mfr_part: str = '',
) -> Optional[tuple]:
    """
    Extract parametric specs from a component row.
    Returns a tuple matching the component_specs INSERT columns, or None.

    When description is empty (common for JLCPCB Basic parts), falls back to
    parsing the EIA resistance code embedded in mfr_part (e.g. 0402WGF1002TCE).
    """
    ctype = _component_type(category, subcategory)
    if ctype is None:
        return None

    desc = description or ''

    if ctype == 'resistor':
        value_si = _resistance_ohms(desc)
        if value_si is None and mfr_part:
            value_si = _resistance_ohms_from_mfr(mfr_part)
    elif ctype == 'capacitor':
        value_si = _capacitance_farads(desc)
    else:  # inductor / ferrite
        value_si = _inductance_henries(desc)

    tol = _tolerance_pct(desc)
    if tol is None and mfr_part and ctype == 'resistor':
        tol = _tolerance_pct_from_mfr(mfr_part)

    return (
        lcsc,
        ctype,
        value_si,
        tol,
        _voltage_v(desc),
        _current_a(desc),
        _power_w(desc),
        _dielectric(desc),
    )

logger = logging.getLogger(__name__)

# Categories that are not electronic components.
EXCLUDED_CATEGORIES: set[str] = {
    "Wire/Cable/DataCable",
    "Wires And Cables",
    "Electronic Tools/Instruments/Consumables",
    "Consumables",
    "Consumables And Auxiliary Materials",
    "Instrumentation/Meter",
    "Hardware/Fasteners/Sealing",
    "Hardware Fasteners",
    "Hardware Fasteners/Seals",
    "Hardwares & Others",
    "Hardwares / Sealings / Machinings",
    "Office Supplies",
    "Solders / Accessories / Batteries",
    "Educational Kits",
}

_DEFAULT_DB = Path(__file__).parent.parent / "data" / "lcsc_parts.db"


def _build_fts_query(query: str) -> str:
    """Convert a free-text query into FTS5 syntax that's robust to punctuation.

    - Alphanumeric tokens of length >= 2 get a prefix wildcard (``LDO`` → ``LDO*``).
    - Tokens with non-alphanumeric chars (``3.3V``, ``SOT-23``) become quoted
      phrases so the FTS5 tokenizer's punctuation-splitting yields an
      adjacent-phrase match instead of a syntax error.
    - Single-char alphanumeric tokens are left bare.

    Empty input returns an empty string (caller should fall back to non-FTS).
    """
    tokens: list[str] = []
    for raw in query.split():
        t = raw.strip("\"'")
        if not t:
            continue
        if any(not c.isalnum() for c in t):
            # Quote phrase; escape inner double quotes per FTS5 rules.
            tokens.append('"' + t.replace('"', '""') + '"')
        elif len(t) >= 2:
            tokens.append(t + "*")
        else:
            tokens.append(t)
    return " ".join(tokens)


def _parse_price(price_str: str) -> list[dict]:
    """Parse 'qty_min-qty_max:price,...' into [{"qty": N, "price": F}, ...]."""
    breaks = []
    for entry in (price_str or "").split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        qty_range, price_part = entry.split(":", 1)
        qty = qty_range.split("-")[0]
        try:
            breaks.append({"qty": int(qty), "price": float(price_part)})
        except ValueError:
            continue
    breaks.sort(key=lambda x: x["qty"])
    return breaks


class PartsDB:
    """Thread-safe SQLite parts database."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path) if path else _DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        logger.info("Parts DB opened at %s", self.path)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS components (
                lcsc          TEXT PRIMARY KEY,
                category      TEXT,
                subcategory   TEXT,
                mfr_part      TEXT,
                package       TEXT,
                solder_joints INTEGER,
                manufacturer  TEXT,
                library_type  TEXT,
                description   TEXT,
                datasheet     TEXT,
                stock         INTEGER,
                price_json    TEXT,
                last_updated  INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_category    ON components(category, subcategory)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_package     ON components(package)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_lib_type    ON components(library_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_mfr_part    ON components(mfr_part)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_stock       ON components(stock)")
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS components_fts USING fts5(
                lcsc, description, mfr_part, manufacturer,
                content=components
            )
        """)
        # FTS5 sync triggers. With content='components' the FTS index stores
        # rowids referring to the components table; without these triggers,
        # any INSERT/UPDATE/DELETE on components leaves the inverted index
        # stale and a JOIN against it can raise 'database disk image is
        # malformed'. Combined with UPSERT in import_batch (which preserves
        # rowids), this keeps the FTS table coherent with no manual rebuild.
        c.executescript("""
            CREATE TRIGGER IF NOT EXISTS components_fts_ai
            AFTER INSERT ON components BEGIN
                INSERT INTO components_fts(rowid, lcsc, description, mfr_part, manufacturer)
                VALUES (new.rowid, new.lcsc, new.description, new.mfr_part, new.manufacturer);
            END;
            CREATE TRIGGER IF NOT EXISTS components_fts_ad
            AFTER DELETE ON components BEGIN
                INSERT INTO components_fts(components_fts, rowid, lcsc, description, mfr_part, manufacturer)
                VALUES ('delete', old.rowid, old.lcsc, old.description, old.mfr_part, old.manufacturer);
            END;
            CREATE TRIGGER IF NOT EXISTS components_fts_au
            AFTER UPDATE ON components BEGIN
                INSERT INTO components_fts(components_fts, rowid, lcsc, description, mfr_part, manufacturer)
                VALUES ('delete', old.rowid, old.lcsc, old.description, old.mfr_part, old.manufacturer);
                INSERT INTO components_fts(rowid, lcsc, description, mfr_part, manufacturer)
                VALUES (new.rowid, new.lcsc, new.description, new.mfr_part, new.manufacturer);
            END;
        """)
        # Parametric specs for passives (resistors, capacitors, inductors…)
        c.execute("""
            CREATE TABLE IF NOT EXISTS component_specs (
                lcsc           TEXT PRIMARY KEY,
                component_type TEXT,
                value_si       REAL,      -- Ohms / Farads / Henries
                tolerance_pct  REAL,      -- percent, e.g. 1.0 for ±1 %
                voltage_v      REAL,      -- Volts
                current_a      REAL,      -- Amperes
                power_w        REAL,      -- Watts
                dielectric     TEXT       -- X7R, C0G, NP0…
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_specs_type_val
            ON component_specs(component_type, value_si)
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.commit()

    # ------------------------------------------------------------------
    # Import (streaming)
    # ------------------------------------------------------------------

    def import_batch(self, parts: list[dict]) -> int:
        """
        Upsert a batch from the official JLCPCB API.
        Excludes non-electronic categories.

        Uses ON CONFLICT DO UPDATE so rowids are preserved on existing rows;
        combined with the FTS5 sync triggers installed in _init_schema, the
        FTS index stays consistent without a manual rebuild_fts() call.

        Returns number of rows inserted or updated.
        """
        now = int(datetime.now().timestamp())
        rows = []

        for p in parts:
            if p.get("firstCategory") in EXCLUDED_CATEGORIES:
                continue

            lib = p.get("libraryType", "")
            if lib == "base":
                library_type = "Basic"
            elif lib in ("preferred", "Preferred"):
                library_type = "Preferred"
            else:
                library_type = "Extended"

            rows.append((
                p.get("lcscPart"),
                p.get("firstCategory"),
                p.get("secondCategory"),
                p.get("mfrPart"),
                p.get("package"),
                p.get("solderJoint"),
                p.get("manufacturer"),
                library_type,
                p.get("description"),
                p.get("datasheet"),
                int(p.get("stock", 0) or 0),
                json.dumps(_parse_price(p.get("price", ""))),
                now,
            ))

        if not rows:
            return 0

        self._conn.executemany("""
            INSERT INTO components (
                lcsc, category, subcategory, mfr_part, package,
                solder_joints, manufacturer, library_type, description,
                datasheet, stock, price_json, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lcsc) DO UPDATE SET
                category      = excluded.category,
                subcategory   = excluded.subcategory,
                mfr_part      = excluded.mfr_part,
                package       = excluded.package,
                solder_joints = excluded.solder_joints,
                manufacturer  = excluded.manufacturer,
                library_type  = excluded.library_type,
                description   = excluded.description,
                datasheet     = excluded.datasheet,
                stock         = excluded.stock,
                price_json    = excluded.price_json,
                last_updated  = excluded.last_updated
        """, rows)

        spec_rows = []
        for r in rows:
            s = _extract_specs(r[0], r[8], r[1], r[2], r[3] or '')  # lcsc, desc, cat, subcat, mfr_part
            if s:
                spec_rows.append(s)
        if spec_rows:
            # component_specs has no FTS dependency, so INSERT OR REPLACE is fine
            self._conn.executemany("""
                INSERT OR REPLACE INTO component_specs
                    (lcsc, component_type, value_si, tolerance_pct,
                     voltage_v, current_a, power_w, dielectric)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, spec_rows)

        self._conn.commit()
        return len(rows)

    def rebuild_fts(self) -> None:
        """Rebuild the full-text search index. Call once after bulk import."""
        self._conn.execute("INSERT INTO components_fts(components_fts) VALUES('rebuild')")
        self._conn.commit()
        logger.info("FTS index rebuilt")

    def rebuild_specs(self) -> int:
        """
        (Re)extract parametric specs for all passive components in the DB.

        Useful after a database upgrade or to retroactively populate specs
        for parts downloaded before this feature existed.

        Returns the number of passive components processed.
        """
        rows = self._conn.execute(
            "SELECT lcsc, description, category, subcategory, mfr_part FROM components"
        ).fetchall()

        spec_rows = []
        for row in rows:
            s = _extract_specs(row[0], row[1], row[2], row[3], row[4] or '')
            if s:
                spec_rows.append(s)

        self._conn.execute("DELETE FROM component_specs")
        if spec_rows:
            self._conn.executemany("""
                INSERT OR REPLACE INTO component_specs
                    (lcsc, component_type, value_si, tolerance_pct,
                     voltage_v, current_a, power_w, dielectric)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, spec_rows)
        self._conn.commit()
        logger.info("Specs rebuilt: %d passive components indexed", len(spec_rows))
        return len(spec_rows)

    # ------------------------------------------------------------------
    # Membership-diff helpers
    # ------------------------------------------------------------------

    def get_all_lcsc_codes(self) -> set[str]:
        """Return the set of all LCSC codes currently stored."""
        rows = self._conn.execute("SELECT lcsc FROM components").fetchall()
        return {r[0] for r in rows}

    def delete_codes_not_in(self, valid_codes: Iterable[str]) -> int:
        """
        Delete every row whose lcsc is NOT in valid_codes. Used by the membership
        refresh to purge parts that are no longer part of the assembly library
        (and to clean up entries left over from earlier full-catalog imports).

        Returns the number of component rows deleted. Does NOT rebuild FTS — the
        caller is expected to do that once after all writes are done.
        """
        codes_list = [(c,) for c in valid_codes if c]
        if not codes_list:
            return 0
        # Use a temp table so we don't hit SQLite's parameter limit (~999) when
        # diffing tens of thousands of codes.
        self._conn.execute("DROP TABLE IF EXISTS _valid_codes")
        self._conn.execute("CREATE TEMP TABLE _valid_codes(lcsc TEXT PRIMARY KEY)")
        self._conn.executemany("INSERT OR IGNORE INTO _valid_codes(lcsc) VALUES(?)", codes_list)
        cur = self._conn.execute(
            "DELETE FROM components WHERE lcsc NOT IN (SELECT lcsc FROM _valid_codes)"
        )
        deleted = cur.rowcount
        self._conn.execute(
            "DELETE FROM component_specs WHERE lcsc NOT IN (SELECT lcsc FROM _valid_codes)"
        )
        self._conn.execute("DROP TABLE _valid_codes")
        self._conn.commit()
        return deleted

    def stale_codes(self, codes: Iterable[str], ttl_hours: float) -> list[str]:
        """
        Return the subset of `codes` whose row is missing or whose `last_updated`
        is older than `ttl_hours`. Used to decide which result rows need a live
        refresh after a search.
        """
        codes = [c for c in codes if c]
        if not codes:
            return []
        cutoff = time.time() - ttl_hours * 3600.0
        placeholders = ",".join("?" * len(codes))
        rows = self._conn.execute(
            f"SELECT lcsc FROM components WHERE lcsc IN ({placeholders}) "
            f"AND last_updated IS NOT NULL AND last_updated >= ?",
            codes + [cutoff],
        ).fetchall()
        fresh = {r[0] for r in rows}
        return [c for c in codes if c not in fresh]

    # ------------------------------------------------------------------
    # Cache metadata
    # ------------------------------------------------------------------

    def get_metadata(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES(?,?)", (key, value)
        )
        self._conn.commit()

    def library_age_hours(self) -> Optional[float]:
        """Return how many hours ago the Basic library was last refreshed, or None."""
        ts = self.get_metadata("basic_library_refreshed_at")
        if not ts:
            return None
        return (time.time() - float(ts)) / 3600.0

    def is_library_stale(self, max_age_hours: float = 24.0) -> bool:
        """Return True if the Basic library has never been fetched or is older than max_age_hours."""
        age = self.library_age_hours()
        return age is None or age > max_age_hours

    def part_age_hours(self, lcsc: str) -> Optional[float]:
        """Return how many hours ago a specific part was last updated in the DB, or None."""
        row = self._conn.execute(
            "SELECT last_updated FROM components WHERE lcsc=?", (lcsc,)
        ).fetchone()
        if not row or not row[0]:
            return None
        return (time.time() - float(row[0])) / 3600.0

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def search(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        package: Optional[str] = None,
        library_type: Optional[str] = None,
        manufacturer: Optional[str] = None,
        in_stock: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        """Full-text + parametric search."""
        if query:
            fts_query = _build_fts_query(query)
            parts = ["""
                SELECT c.* FROM components c
                JOIN components_fts ON components_fts.lcsc = c.lcsc
                WHERE components_fts MATCH ?
            """]
            params: list = [fts_query]
        else:
            parts = ["SELECT c.* FROM components c WHERE 1=1"]
            params = []

        if category:
            parts.append("AND (c.category LIKE ? OR c.subcategory LIKE ?)")
            params += [f"%{category}%", f"%{category}%"]

        if package:
            parts.append("AND c.package LIKE ?")
            params.append(f"%{package}%")

        if library_type and library_type != "All":
            parts.append("AND c.library_type = ?")
            params.append(library_type)

        if manufacturer:
            parts.append("AND c.manufacturer LIKE ?")
            params.append(f"%{manufacturer}%")

        if in_stock:
            parts.append("AND c.stock > 0")

        parts.append("LIMIT ?")
        params.append(limit)

        # Let sqlite3.DatabaseError (incl. 'database disk image is malformed')
        # propagate so corruption surfaces in the caller's response instead of
        # silently returning [].
        rows = self._conn.execute(" ".join(parts), params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, lcsc: str) -> Optional[dict]:
        """Get a single part by LCSC number."""
        row = self._conn.execute(
            "SELECT * FROM components WHERE lcsc = ?", (lcsc,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # Category mappings for passives
    # ------------------------------------------------------------------

    _PASSIVE_CATEGORIES: dict[str, list[str]] = {
        "resistor":    ["Resistor", "Res"],
        "capacitor":   ["Capacitor", "Cap"],
        "inductor":    ["Inductor", "Coil", "Choke"],
        "ferrite":     ["Ferrite Bead", "Ferrite Core", "Bead"],
    }

    def search_passive(
        self,
        component_type: str,
        # --- text filters (LIKE on description) ---
        value: Optional[str] = None,
        tolerance: Optional[str] = None,
        power_rating: Optional[str] = None,
        voltage_rating: Optional[str] = None,
        dielectric: Optional[str] = None,
        current_rating: Optional[str] = None,
        # --- numeric range filters (use component_specs table) ---
        value_min: Optional[float] = None,
        value_max: Optional[float] = None,
        tolerance_max_pct: Optional[float] = None,
        voltage_min_v: Optional[float] = None,
        power_min_w: Optional[float] = None,
        current_min_a: Optional[float] = None,
        # --- common filters ---
        package: Optional[str] = None,
        library_type: Optional[str] = None,
        in_stock: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        """
        Parametric search for passive components.

        ``value`` accepts any textual representation (e.g. '4.7nF', '33pF',
        '10kΩ', '100R', '4.7uH').  It is first parsed numerically; if
        successful a ±0.5 % range query is added against the component_specs
        table (fast, indexed).  If parsing fails, a LIKE fallback is applied
        against the description column.

        All other text filters (tolerance, voltage_rating…) use LIKE on the
        description column — FTS is intentionally avoided here because decimal
        points and special characters (±, Ω, /) break FTS5 syntax.

        Numeric range filters (value_min/max, voltage_min_v…) can be combined
        freely with text filters and require the component_specs table
        (populate it with rebuild_specs() / rebuild_component_specs tool).
        """
        # ------------------------------------------------------------------
        # Step 1 — try to parse value as a numeric quantity
        # ------------------------------------------------------------------
        _parsers = {
            'resistor':  _resistance_ohms,
            'capacitor': _capacitance_farads,
            'inductor':  _inductance_henries,
            'ferrite':   _inductance_henries,
        }
        parsed_value: Optional[float] = None
        if value:
            parser = _parsers.get(component_type.lower())
            if parser:
                parsed_value = parser(value)
            # Fallback: bare number → treat as base unit (Ohms for resistors,
            # Farads for capacitors, Henries for inductors)
            if parsed_value is None:
                try:
                    parsed_value = float(value.replace(',', '.'))
                except (ValueError, AttributeError):
                    pass

        # Auto-set value_min/max when value is parseable (±0.5 % window)
        _TOL = 0.005
        if parsed_value is not None:
            if value_min is None:
                value_min = parsed_value * (1 - _TOL) if parsed_value != 0 else -1e-15
            if value_max is None:
                value_max = parsed_value * (1 + _TOL) if parsed_value != 0 else 1e-15

        use_numeric = any(v is not None for v in [
            value_min, value_max, tolerance_max_pct,
            voltage_min_v, power_min_w, current_min_a,
        ])

        # ------------------------------------------------------------------
        # FROM clause
        # ------------------------------------------------------------------
        from_parts = ["FROM components c"]
        if use_numeric:
            from_parts.append("JOIN component_specs s ON s.lcsc = c.lcsc")

        # ------------------------------------------------------------------
        # WHERE conditions
        # ------------------------------------------------------------------
        where_parts: list[str] = []
        params: list = []

        # Category guard
        cat_keywords = self._PASSIVE_CATEGORIES.get(component_type.lower(), [component_type])
        cat_cond = " OR ".join(
            "c.category LIKE ? OR c.subcategory LIKE ?" for _ in cat_keywords
        )
        where_parts.append(f"({cat_cond})")
        for kw in cat_keywords:
            params += [f"%{kw}%", f"%{kw}%"]

        # value: LIKE fallback only when numeric parsing failed
        if value and parsed_value is None:
            where_parts.append("c.description LIKE ?")
            params.append(f"%{value}%")

        # Other text filters — LIKE on description (safe with decimal points,
        # ± sign, Ω, / etc. that break FTS5 syntax)
        for txt in [tolerance, power_rating, voltage_rating, current_rating, dielectric]:
            if txt:
                where_parts.append("c.description LIKE ?")
                params.append(f"%{txt}%")

        if package:
            where_parts.append("c.package LIKE ?")
            params.append(f"%{package}%")

        if library_type and library_type != "All":
            where_parts.append("c.library_type = ?")
            params.append(library_type)

        if in_stock:
            where_parts.append("c.stock > 0")

        # Numeric specs filters
        if use_numeric:
            where_parts.append("s.component_type = ?")
            params.append(component_type.lower())

            if value_min is not None:
                where_parts.append("s.value_si >= ?")
                params.append(value_min)
            if value_max is not None:
                where_parts.append("s.value_si <= ?")
                params.append(value_max)
            if tolerance_max_pct is not None:
                where_parts.append("s.tolerance_pct IS NOT NULL AND s.tolerance_pct <= ?")
                params.append(tolerance_max_pct)
            if voltage_min_v is not None:
                where_parts.append("s.voltage_v IS NOT NULL AND s.voltage_v >= ?")
                params.append(voltage_min_v)
            if power_min_w is not None:
                where_parts.append("s.power_w IS NOT NULL AND s.power_w >= ?")
                params.append(power_min_w)
            if current_min_a is not None:
                where_parts.append("s.current_a IS NOT NULL AND s.current_a >= ?")
                params.append(current_min_a)

        where_clause = "WHERE " + " AND ".join(where_parts)
        order_clause = (
            "ORDER BY CASE c.library_type WHEN 'Basic' THEN 0 WHEN 'Preferred' THEN 1 ELSE 2 END,"
            " CAST(json_extract(c.price_json, '$[0].price') AS REAL)"
        )
        sql = f"SELECT c.* {' '.join(from_parts)} {where_clause} {order_clause} LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def suggest_alternatives(
        self,
        lcsc: str,
        limit: int = 5,
        value_tolerance_pct: float = 5.0,
    ) -> list[dict]:
        """Find alternative parts: same category + package, sorted Basic → cheap → in-stock.

        For passives with a known parametric value (resistance/capacitance/
        inductance), alternatives are additionally constrained to ±value_tolerance_pct
        of the reference value — so a 10kΩ resistor doesn't get 0Ω, 33kΩ, etc.
        proposed as drop-ins.
        """
        ref = self.get(lcsc)
        if not ref:
            return []

        category = ref.get("subcategory") or ref.get("category")
        package = ref.get("package")

        # Pull reference parametric value if present (passives only).
        ref_specs = self._conn.execute(
            "SELECT component_type, value_si, dielectric FROM component_specs WHERE lcsc = ?",
            (lcsc,),
        ).fetchone()

        ref_type = ref_specs["component_type"] if ref_specs else None
        ref_value = ref_specs["value_si"] if ref_specs else None
        ref_dielectric = ref_specs["dielectric"] if ref_specs else None

        sql_parts = ["""
            SELECT c.* FROM components c
        """]
        params: list = []

        if ref_value is not None and ref_value > 0:
            sql_parts.append("JOIN component_specs s ON s.lcsc = c.lcsc")
        sql_parts.append("""
            WHERE c.lcsc != ?
              AND (c.category LIKE ? OR c.subcategory LIKE ?)
              AND c.package LIKE ?
              AND c.stock > 0
        """)
        params += [lcsc, f"%{category}%", f"%{category}%", f"%{package}%"]

        if ref_value is not None and ref_value > 0:
            tol = value_tolerance_pct / 100.0
            sql_parts.append("AND s.component_type = ?")
            params.append(ref_type)
            sql_parts.append("AND s.value_si BETWEEN ? AND ?")
            params += [ref_value * (1 - tol), ref_value * (1 + tol)]
            # Match dielectric for ceramic capacitors when known — X7R is not
            # a drop-in for C0G even at the same nominal capacitance.
            if ref_type == "capacitor" and ref_dielectric:
                sql_parts.append("AND (s.dielectric = ? OR s.dielectric IS NULL)")
                params.append(ref_dielectric)

        sql_parts.append("""
            ORDER BY
                CASE c.library_type WHEN 'Basic' THEN 0 WHEN 'Preferred' THEN 1 ELSE 2 END,
                CAST(json_extract(c.price_json, '$[0].price') AS REAL),
                -c.stock
            LIMIT ?
        """)
        params.append(limit)

        rows = self._conn.execute("\n".join(sql_parts), params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats(self) -> dict:
        """Return database statistics."""
        def _count(sql, *args):
            return self._conn.execute(sql, args).fetchone()[0]

        age = self.library_age_hours()
        return {
            "total":     _count("SELECT COUNT(*) FROM components"),
            "basic":     _count("SELECT COUNT(*) FROM components WHERE library_type='Basic'"),
            "preferred": _count("SELECT COUNT(*) FROM components WHERE library_type='Preferred'"),
            "extended":  _count("SELECT COUNT(*) FROM components WHERE library_type='Extended'"),
            "in_stock":  _count("SELECT COUNT(*) FROM components WHERE stock > 0"),
            "db_path":   str(self.path),
            "db_size_mb": round(self.path.stat().st_size / 1_048_576, 1) if self.path.exists() else 0,
            "basic_library_age_hours": round(age, 1) if age is not None else None,
            "basic_library_stale": self.is_library_stale(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["price_breaks"] = json.loads(d.get("price_json") or "[]")
        except (ValueError, TypeError):
            d["price_breaks"] = []
        return d

    def close(self) -> None:
        self._conn.close()
