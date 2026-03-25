"""Tests for lcsc_mcp/db.py — SI parsers and PartsDB."""
import sqlite3
import time

import pytest
from freezegun import freeze_time

from lcsc_mcp.db import (
    PartsDB,
    _capacitance_farads,
    _component_type,
    _current_a,
    _dielectric,
    _extract_specs,
    _inductance_henries,
    _parse_price,
    _power_w,
    _resistance_ohms,
    _resistance_ohms_from_mfr,
    _tolerance_pct,
    _tolerance_pct_from_mfr,
    _voltage_v,
)


# ---------------------------------------------------------------------------
# _resistance_ohms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("10kΩ", 10_000.0),
    ("4.7MΩ", 4_700_000.0),
    ("100R", 100.0),
    ("0Ω", 0.0),
    ("100mΩ", 0.1),
    ("33K", 33_000.0),
    ("100ohm", 100.0),
    ("garbage", None),
    ("", None),
])
def test_resistance_ohms(desc, expected):
    result = _resistance_ohms(desc)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _capacitance_farads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("100nF", 100e-9),
    ("10µF", 10e-6),
    ("1pF", 1e-12),
    ("0.1uF", 0.1e-6),
    ("10mF", 10e-3),
    ("1MF", 1e-3),   # M = milli in capacitance context
    ("garbage", None),
    ("", None),
])
def test_capacitance_farads(desc, expected):
    result = _capacitance_farads(desc)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _inductance_henries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("10µH", 10e-6),
    ("100nH", 100e-9),
    ("4.7uH", 4.7e-6),
    ("1mH", 1e-3),
    ("garbage", None),
    ("", None),
])
def test_inductance_henries(desc, expected):
    result = _inductance_henries(desc)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _voltage_v
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("50V", 50.0),
    ("3.3V", 3.3),
    ("1kV", 1000.0),
    ("100VDC", 100.0),
    ("garbage", None),
])
def test_voltage_v(desc, expected):
    result = _voltage_v(desc)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _current_a
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("100mA", 0.1),
    ("1A", 1.0),
    ("2.5A", 2.5),
    ("garbage", None),
])
def test_current_a(desc, expected):
    result = _current_a(desc)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _power_w
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("1/4W", 0.25),
    ("1/10W", 0.1),
    ("0.125W", 0.125),
    ("100mW", 0.1),
    ("garbage", None),
])
def test_power_w(desc, expected):
    result = _power_w(desc)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _tolerance_pct
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("±1%", 1.0),
    ("±0.1%", 0.1),
    ("±5%", 5.0),
    ("no tolerance", None),
    ("", None),
])
def test_tolerance_pct(desc, expected):
    result = _tolerance_pct(desc)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _dielectric
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,expected", [
    ("X7R", "X7R"),
    ("C0G", "C0G"),
    ("NP0", "NP0"),
    ("X5R 6.3V", "X5R"),
    ("garbage", None),
    ("", None),
])
def test_dielectric(desc, expected):
    assert _dielectric(desc) == expected


# ---------------------------------------------------------------------------
# _resistance_ohms_from_mfr
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mfr,expected", [
    ("0402WGF1002TCE", 10_000.0),   # 4-digit EIA: 100 × 10² = 10 kΩ
    ("0402WGF330JTCE", 33.0),        # 3-digit EIA: 33 × 10⁰ = 33 Ω
    ("0603WAF4702T5E", 47_000.0),   # 4-digit: 470 × 10² = 47 kΩ
    ("UNKNOWN", None),
    ("", None),
])
def test_resistance_ohms_from_mfr(mfr, expected):
    result = _resistance_ohms_from_mfr(mfr)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _tolerance_pct_from_mfr
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mfr,expected", [
    ("0402WGF330FTCE", 1.0),    # F = ±1%
    ("0402WGF330JTCE", 5.0),    # J = ±5%
    ("0402WGF330KTCE", 10.0),   # K = ±10%
    ("0402WGF1002TCE", None),   # no tolerance letter
    ("UNKNOWN", None),
    ("", None),
])
def test_tolerance_pct_from_mfr(mfr, expected):
    result = _tolerance_pct_from_mfr(mfr)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _component_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cat,subcat,expected", [
    ("Resistors", "Chip Resistor - Surface Mount", "resistor"),
    ("Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT", "capacitor"),
    ("Inductors/Coils/Transformers", "Inductors (SMD)", "inductor"),
    ("Passive Components", "Ferrite Bead", "ferrite"),
    ("Integrated Circuits", "Microcontrollers", None),
    ("", "", None),
])
def test_component_type(cat, subcat, expected):
    assert _component_type(cat, subcat) == expected


# ---------------------------------------------------------------------------
# _extract_specs
# ---------------------------------------------------------------------------

def test_extract_specs_resistor():
    result = _extract_specs("C25744", "10kΩ ±1% 1/16W", "Resistors", "Chip Resistor - Surface Mount", "0402WGF1002TCE")
    assert result is not None
    lcsc, ctype, value_si, tol, voltage, current, power, dielectric = result
    assert ctype == "resistor"
    assert value_si == pytest.approx(10_000.0)
    assert tol == pytest.approx(1.0)
    assert power == pytest.approx(1 / 16)


def test_extract_specs_resistor_from_mfr_only():
    """When description has no resistance value, fall back to mfr_part EIA code."""
    result = _extract_specs("C25744", "", "Resistors", "Chip Resistor - Surface Mount", "0402WGF1002TCE")
    assert result is not None
    assert result[1] == "resistor"
    assert result[2] == pytest.approx(10_000.0)


def test_extract_specs_capacitor():
    result = _extract_specs("C1525", "100nF ±10% 25V X7R", "Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT")
    assert result is not None
    lcsc, ctype, value_si, tol, voltage, current, power, dielectric = result
    assert ctype == "capacitor"
    assert value_si == pytest.approx(100e-9)
    assert voltage == pytest.approx(25.0)
    assert dielectric == "X7R"


def test_extract_specs_inductor():
    result = _extract_specs("C1044", "10nH ±5% 100mA", "Inductors/Coils/Transformers", "Inductors (SMD)")
    assert result is not None
    assert result[1] == "inductor"
    assert result[2] == pytest.approx(10e-9)


def test_extract_specs_unknown_type():
    result = _extract_specs("C20734", "MCU", "Integrated Circuits", "Microcontrollers")
    assert result is None


# ---------------------------------------------------------------------------
# _parse_price
# ---------------------------------------------------------------------------

def test_parse_price_valid():
    breaks = _parse_price("20-100:0.001,100-1000:0.0008")
    assert len(breaks) == 2
    assert breaks[0] == {"qty": 20, "price": 0.001}
    assert breaks[1] == {"qty": 100, "price": 0.0008}


def test_parse_price_empty():
    assert _parse_price("") == []
    assert _parse_price(None) == []


def test_parse_price_malformed_entry():
    # Entry without ':' is skipped; bad price is skipped
    result = _parse_price("nocodon,10-20:bad,5-10:0.5")
    assert len(result) == 1
    assert result[0]["price"] == 0.5


def test_parse_price_sorted():
    result = _parse_price("100-1000:0.001,1-10:0.01,10-100:0.005")
    qtys = [b["qty"] for b in result]
    assert qtys == sorted(qtys)


# ---------------------------------------------------------------------------
# PartsDB — schema / init
# ---------------------------------------------------------------------------

def test_schema_tables_created(mem_db):
    tables = {
        row[0]
        for row in mem_db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','shadow')"
        ).fetchall()
    }
    assert "components" in tables
    assert "component_specs" in tables
    assert "metadata" in tables


def test_init_idempotent():
    db = PartsDB(":memory:")
    db._init_schema()  # second call must not raise
    db.close()


# ---------------------------------------------------------------------------
# import_batch
# ---------------------------------------------------------------------------

def test_import_batch_inserts(mem_db, sample_parts):
    count = mem_db.import_batch(sample_parts)
    # 5 parts - 1 excluded cable = 4
    assert count == 4
    total = mem_db._conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    assert total == 4


def test_import_batch_excluded_category(mem_db):
    cable = [{
        "lcscPart": "C99999",
        "firstCategory": "Wire/Cable/DataCable",
        "secondCategory": "Cable",
        "mfrPart": "X", "package": "", "solderJoint": 0, "manufacturer": "G",
        "libraryType": "base", "description": "cable", "datasheet": "",
        "stock": 1, "price": "",
    }]
    count = mem_db.import_batch(cable)
    assert count == 0


def test_import_batch_library_type_mapping(mem_db):
    parts = [
        {"lcscPart": "C1", "firstCategory": "ICs", "secondCategory": "", "mfrPart": "",
         "package": "", "solderJoint": 0, "manufacturer": "", "libraryType": "base",
         "description": "", "datasheet": "", "stock": 1, "price": ""},
        {"lcscPart": "C2", "firstCategory": "ICs", "secondCategory": "", "mfrPart": "",
         "package": "", "solderJoint": 0, "manufacturer": "", "libraryType": "preferred",
         "description": "", "datasheet": "", "stock": 1, "price": ""},
        {"lcscPart": "C3", "firstCategory": "ICs", "secondCategory": "", "mfrPart": "",
         "package": "", "solderJoint": 0, "manufacturer": "", "libraryType": "extend",
         "description": "", "datasheet": "", "stock": 1, "price": ""},
        {"lcscPart": "C4", "firstCategory": "ICs", "secondCategory": "", "mfrPart": "",
         "package": "", "solderJoint": 0, "manufacturer": "", "libraryType": "Preferred",
         "description": "", "datasheet": "", "stock": 1, "price": ""},
    ]
    mem_db.import_batch(parts)
    rows = {r[0]: r[1] for r in mem_db._conn.execute("SELECT lcsc, library_type FROM components").fetchall()}
    assert rows["C1"] == "Basic"
    assert rows["C2"] == "Preferred"
    assert rows["C3"] == "Extended"
    assert rows["C4"] == "Preferred"


def test_import_batch_extracts_specs(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    count = mem_db._conn.execute("SELECT COUNT(*) FROM component_specs").fetchone()[0]
    assert count > 0


def test_import_batch_empty(mem_db):
    assert mem_db.import_batch([]) == 0


# ---------------------------------------------------------------------------
# rebuild_fts / rebuild_specs / clear
# ---------------------------------------------------------------------------

def test_rebuild_fts(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_fts()
    results = mem_db.search(query="10kΩ")
    assert any(r["lcsc"] == "C25744" for r in results)


def test_rebuild_specs(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db._conn.execute("DELETE FROM component_specs")
    mem_db._conn.commit()
    count = mem_db.rebuild_specs()
    assert count > 0
    new_count = mem_db._conn.execute("SELECT COUNT(*) FROM component_specs").fetchone()[0]
    assert new_count == count


def test_rebuild_specs_no_passives(mem_db):
    """DB with only ICs → spec_rows is empty → executemany branch skipped."""
    parts = [{
        "lcscPart": "C20734", "firstCategory": "Integrated Circuits",
        "secondCategory": "Microcontrollers", "mfrPart": "STM32",
        "package": "LQFP-48", "solderJoint": 48, "manufacturer": "ST",
        "libraryType": "extend", "description": "32-bit MCU", "datasheet": "",
        "stock": 5000, "price": "1-10:2.50",
    }]
    mem_db.import_batch(parts)
    count = mem_db.rebuild_specs()
    assert count == 0


def test_clear(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.clear()
    total = mem_db._conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    assert total == 0


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

def test_metadata_roundtrip(mem_db):
    mem_db.set_metadata("test_key", "test_value")
    assert mem_db.get_metadata("test_key") == "test_value"


def test_metadata_missing_key(mem_db):
    assert mem_db.get_metadata("nonexistent") is None


# ---------------------------------------------------------------------------
# Time-based methods
# ---------------------------------------------------------------------------

def test_library_age_hours_never_set(mem_db):
    assert mem_db.library_age_hours() is None


def test_library_age_hours_set():
    db = PartsDB(":memory:")
    with freeze_time("2026-01-01 10:00:00"):
        db.set_metadata("basic_library_refreshed_at", str(time.time()))
    with freeze_time("2026-01-01 12:00:00"):
        age = db.library_age_hours()
    assert age == pytest.approx(2.0, abs=0.01)
    db.close()


def test_is_library_stale_never_fetched(mem_db):
    assert mem_db.is_library_stale() is True


def test_is_library_stale_fresh():
    db = PartsDB(":memory:")
    with freeze_time("2026-01-01 10:00:00"):
        db.set_metadata("basic_library_refreshed_at", str(time.time()))
        stale = db.is_library_stale(max_age_hours=24.0)
    assert stale is False
    db.close()


def test_is_library_stale_old():
    db = PartsDB(":memory:")
    with freeze_time("2026-01-01 10:00:00"):
        db.set_metadata("basic_library_refreshed_at", str(time.time()))
    with freeze_time("2026-01-02 11:00:00"):
        stale = db.is_library_stale(max_age_hours=24.0)
    assert stale is True
    db.close()


def test_part_age_hours_missing(mem_db):
    assert mem_db.part_age_hours("C99999") is None


def test_part_age_hours_set(mem_db, sample_parts):
    with freeze_time("2026-01-01 10:00:00"):
        mem_db.import_batch(sample_parts)
    with freeze_time("2026-01-01 13:00:00"):
        age = mem_db.part_age_hours("C25744")
    assert age == pytest.approx(3.0, abs=0.01)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_fts_match(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_fts()
    results = mem_db.search(query="10kΩ")
    assert any(r["lcsc"] == "C25744" for r in results)


def test_search_fts_no_match(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_fts()
    results = mem_db.search(query="nonexistent_xyz_part")
    assert results == []


def test_search_no_query(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search()
    assert len(results) > 0


def test_search_filters_library_type(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search(library_type="Basic")
    assert all(r["library_type"] == "Basic" for r in results)


def test_search_filter_library_type_all(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search(library_type="All")
    assert len(results) > 0


def test_search_filters_in_stock(mem_db):
    parts = [
        {"lcscPart": "C1", "firstCategory": "ICs", "secondCategory": "", "mfrPart": "",
         "package": "0402", "solderJoint": 2, "manufacturer": "", "libraryType": "base",
         "description": "in stock", "datasheet": "", "stock": 100, "price": ""},
        {"lcscPart": "C2", "firstCategory": "ICs", "secondCategory": "", "mfrPart": "",
         "package": "0402", "solderJoint": 2, "manufacturer": "", "libraryType": "base",
         "description": "out of stock", "datasheet": "", "stock": 0, "price": ""},
    ]
    mem_db.import_batch(parts)
    results = mem_db.search(in_stock=True)
    lcsc_codes = [r["lcsc"] for r in results]
    assert "C1" in lcsc_codes
    assert "C2" not in lcsc_codes


def test_search_with_all_filters(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search(
        category="Resistor",
        package="0402",
        manufacturer="UNI-ROYAL",
        limit=5,
    )
    assert isinstance(results, list)


def test_search_in_stock_false(mem_db, sample_parts):
    """in_stock=False → stock filter not applied (line 532->535 branch)."""
    mem_db.import_batch(sample_parts)
    results = mem_db.search(in_stock=False)
    assert isinstance(results, list)
    assert len(results) > 0


def test_search_error_returns_empty(mem_db, mocker):
    mock_conn = mocker.MagicMock()
    mock_conn.execute.side_effect = sqlite3.OperationalError("forced")
    mem_db._conn = mock_conn
    assert mem_db.search(query="test") == []


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_existing(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    part = mem_db.get("C25744")
    assert part is not None
    assert part["lcsc"] == "C25744"
    assert "price_breaks" in part


def test_get_missing(mem_db):
    assert mem_db.get("C00000") is None


# ---------------------------------------------------------------------------
# _row_to_dict
# ---------------------------------------------------------------------------

def test_row_to_dict_valid_price():
    row = {"price_json": '[{"qty":20,"price":0.001}]', "lcsc": "C1"}
    result = PartsDB._row_to_dict(row)
    assert result["price_breaks"] == [{"qty": 20, "price": 0.001}]


def test_row_to_dict_none_price():
    row = {"price_json": None, "lcsc": "C1"}
    result = PartsDB._row_to_dict(row)
    assert result["price_breaks"] == []


def test_row_to_dict_invalid_json():
    row = {"price_json": "not_valid_json", "lcsc": "C1"}
    result = PartsDB._row_to_dict(row)
    assert result["price_breaks"] == []


def test_row_to_dict_non_string_price():
    """Triggers TypeError in json.loads."""
    row = {"price_json": 42, "lcsc": "C1"}
    result = PartsDB._row_to_dict(row)
    assert result["price_breaks"] == []


# ---------------------------------------------------------------------------
# search_passive
# ---------------------------------------------------------------------------

def test_search_passive_by_value(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", value="10kΩ")
    assert any(r["lcsc"] == "C25744" for r in results)


def test_search_passive_zero_value(mem_db):
    """Zero-value resistor uses -1e-15 to 1e-15 range."""
    parts = [{
        "lcscPart": "C17168",
        "firstCategory": "Resistors",
        "secondCategory": "Chip Resistor - Surface Mount",
        "mfrPart": "0402WGF0000TCE",
        "package": "0402", "solderJoint": 2, "manufacturer": "UNI-ROYAL",
        "libraryType": "base", "description": "0Ω jumper", "datasheet": "",
        "stock": 1000000, "price": "20-100:0.001",
    }]
    mem_db.import_batch(parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", value="0Ω")
    assert isinstance(results, list)


def test_search_passive_numeric_range(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", value_min=9000.0, value_max=11000.0)
    assert any(r["lcsc"] == "C25744" for r in results)


def test_search_passive_unparseable_value_like_fallback(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search_passive("resistor", value="10kOhm_LIKE")
    assert isinstance(results, list)


def test_search_passive_bare_float_value(mem_db, sample_parts):
    """Bare float string falls back to float() conversion."""
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", value="10000")
    assert isinstance(results, list)


def test_search_passive_dielectric_filter(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search_passive("capacitor", dielectric="X7R")
    assert isinstance(results, list)


def test_search_passive_text_filters(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive(
        "resistor",
        tolerance="±1%",
        power_rating="1/16W",
        voltage_rating="50V",
        current_rating="100mA",
        dielectric="X7R",
    )
    assert isinstance(results, list)


def test_search_passive_voltage_min(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("capacitor", voltage_min_v=10.0)
    assert isinstance(results, list)


def test_search_passive_tolerance_max(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", tolerance_max_pct=1.0)
    assert isinstance(results, list)


def test_search_passive_power_min(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", power_min_w=0.05)
    assert isinstance(results, list)


def test_search_passive_current_min(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("inductor", current_min_a=0.05)
    assert isinstance(results, list)


def test_search_passive_in_stock_false(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search_passive("resistor", in_stock=False)
    assert isinstance(results, list)


def test_search_passive_package_filter(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search_passive("resistor", package="0402")
    assert isinstance(results, list)


def test_search_passive_library_type_filter(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    results = mem_db.search_passive("resistor", library_type="Basic")
    assert all(r["library_type"] == "Basic" for r in results)


def test_search_passive_ferrite(mem_db):
    parts = [{
        "lcscPart": "C1001",
        "firstCategory": "Inductors/Coils/Transformers",
        "secondCategory": "Ferrite Bead",
        "mfrPart": "BLM18AG601SN1D", "package": "0402", "solderJoint": 2,
        "manufacturer": "Murata", "libraryType": "base",
        "description": "600Ω@100MHz 100mA", "datasheet": "",
        "stock": 500000, "price": "20-100:0.01",
    }]
    mem_db.import_batch(parts)
    results = mem_db.search_passive("ferrite")
    assert isinstance(results, list)


def test_search_passive_no_results(mem_db):
    results = mem_db.search_passive("resistor", value="999GΩ")
    assert results == []


def test_search_passive_unknown_type_no_parser(mem_db):
    """Component type not in _parsers (e.g. 'crystal') → parser is None → fallback float."""
    results = mem_db.search_passive("crystal", value="10MHz")
    assert isinstance(results, list)


def test_search_passive_value_with_explicit_value_min(mem_db, sample_parts):
    """Explicit value_min + parsed value → if value_min is None: branch NOT taken."""
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", value="10kΩ", value_min=5000.0)
    assert isinstance(results, list)


def test_search_passive_value_with_explicit_value_max(mem_db, sample_parts):
    """Explicit value_max + parsed value → if value_max is None: branch NOT taken."""
    mem_db.import_batch(sample_parts)
    mem_db.rebuild_specs()
    results = mem_db.search_passive("resistor", value="10kΩ", value_max=15000.0)
    assert isinstance(results, list)


def test_search_passive_error_returns_empty(mem_db, mocker):
    mock_conn = mocker.MagicMock()
    mock_conn.execute.side_effect = sqlite3.OperationalError("forced")
    mem_db._conn = mock_conn
    assert mem_db.search_passive("resistor") == []


# ---------------------------------------------------------------------------
# suggest_alternatives
# ---------------------------------------------------------------------------

def test_suggest_alternatives_found(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    # Add another 0402 resistor to serve as alternative
    extra = [{
        "lcscPart": "C25105",
        "firstCategory": "Resistors",
        "secondCategory": "Chip Resistor - Surface Mount",
        "mfrPart": "0402WGF330JTCE",
        "package": "0402", "solderJoint": 2, "manufacturer": "UNI-ROYAL",
        "libraryType": "base", "description": "33Ω ±5% 1/16W", "datasheet": "",
        "stock": 500000, "price": "20-100:0.001",
    }]
    mem_db.import_batch(extra)
    alts = mem_db.suggest_alternatives("C25744")
    assert isinstance(alts, list)


def test_suggest_alternatives_unknown_part(mem_db):
    assert mem_db.suggest_alternatives("C00000") == []


def test_suggest_alternatives_error_returns_empty(mem_db, sample_parts, mocker):
    mem_db.import_batch(sample_parts)
    orig_execute = mem_db._conn.execute

    call_count = [0]

    def selective_raise(sql, *args):
        call_count[0] += 1
        if call_count[0] > 1:
            raise sqlite3.OperationalError("forced")
        return orig_execute(sql, *args)

    mock_conn = mocker.MagicMock()
    mock_conn.execute.side_effect = selective_raise
    mem_db._conn = mock_conn
    assert mem_db.suggest_alternatives("C25744") == []


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_empty_db(mem_db):
    s = mem_db.stats()
    assert s["total"] == 0
    assert s["db_size_mb"] == 0


def test_stats_with_data(mem_db, sample_parts):
    mem_db.import_batch(sample_parts)
    s = mem_db.stats()
    assert s["total"] == 4
    assert s["basic"] == 2
    assert s["extended"] == 2


def test_stats_with_real_file(tmp_path):
    """Tests the self.path.exists() → True branch for db_size_mb."""
    db_path = str(tmp_path / "test.db")
    db = PartsDB(db_path)
    s = db.stats()
    assert s["db_size_mb"] >= 0
    db.close()


def test_stats_library_age(tmp_path):
    """Tests the age is not None branch in stats."""
    db = PartsDB(str(tmp_path / "test.db"))
    with freeze_time("2026-01-01 10:00:00"):
        db.set_metadata("basic_library_refreshed_at", str(time.time()))
    with freeze_time("2026-01-01 11:00:00"):
        s = db.stats()
    assert s["basic_library_age_hours"] == pytest.approx(1.0, abs=0.01)
    db.close()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def test_close():
    db = PartsDB(":memory:")
    db.close()
    with pytest.raises(Exception):
        db._conn.execute("SELECT 1")
