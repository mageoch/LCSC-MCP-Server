"""
Integration tests — call the live JLCPCB API.

Run with:
    uv run pytest --integration

Requires env vars: JLCPCB_APP_ID, JLCPCB_API_KEY, JLCPCB_API_SECRET.
Skipped automatically in normal test runs.

Design notes
------------
Tests use hard-coded LCSC codes for a handful of well-known Basic components.
This avoids downloading the full library (slow) while still exercising the
real API and the full import→search pipeline.

Known Basic 0402 resistors (UNI-ROYAL WGF series, E24 subset):
  C25744  10 kΩ    0402WGF1002TCE   lib=base
  C25752  12 kΩ    0402WGF1202TCE   lib=base
  C25741  100 kΩ   0402WGF1003TCE   lib=base
  C25764  200 kΩ   0402WGF2003TCE   lib=base

Note: 120 kΩ / 150 kΩ / 220 kΩ do NOT exist as 0402 Basic at JLCPCB —
those are Extended (lib=expand).  150 kΩ is Basic in 0805 (C17470).
"""

import pytest

from jlcpcb_mcp.client import JLCPCBClient
from jlcpcb_mcp.db import PartsDB

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Known LCSC codes — verified Basic 0402 chip resistors
# ---------------------------------------------------------------------------
KNOWN_BASIC_0402 = {
    "C25744": "10k",
    "C25752": "12k",
    "C25741": "100k",
    "C25764": "200k",
}

# A known Basic 0805 resistor (150 kΩ)
KNOWN_BASIC_0805_150K = "C17470"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_client():
    return JLCPCBClient()


@pytest.fixture(scope="module")
def basic_db(live_client):
    """
    In-memory DB populated with known Basic resistors fetched via
    getComponentDetailByCode.  Fast: one small API call for ~5 codes.
    """
    codes = list(KNOWN_BASIC_0402.keys()) + [KNOWN_BASIC_0805_150K]
    parts = live_client.get_parts_details(codes)
    assert parts, f"Detail endpoint returned no data for codes {codes}"
    db = PartsDB(":memory:")
    db.import_batch(parts)
    db.rebuild_fts()
    db.rebuild_specs()
    yield db
    db.close()


# ---------------------------------------------------------------------------
# JLCPCBClient — live API smoke tests
# ---------------------------------------------------------------------------

def test_client_get_part_detail_known_part(live_client):
    """C25744 (10 kΩ 0402 Basic) must be retrievable and marked Basic."""
    part = live_client.get_part_detail("C25744")
    assert part is not None
    assert part["lcscPart"] == "C25744"
    assert part["package"] == "0402"
    assert part["libraryType"] == "base"


def test_client_get_part_detail_returns_none_for_unknown(live_client):
    """A non-existent LCSC code must return None, not raise."""
    result = live_client.get_part_detail("C000000000")
    assert result is None


def test_client_get_parts_details_batch(live_client):
    """Batch lookup returns one normalized record per requested code."""
    codes = list(KNOWN_BASIC_0402)
    results = live_client.get_parts_details(codes)
    assert len(results) == len(codes)
    assert {r["lcscPart"] for r in results} >= set(codes)


def test_client_library_list_returns_stubs(live_client):
    """getComponentLibraryList returns a (stubs, next_key) tuple."""
    stubs, next_key = live_client.get_library_list(page_size=100)
    assert len(stubs) > 0
    assert all("componentCode" in s for s in stubs)
    # next_key may be None on the last page or a non-empty string when more pages remain.
    assert next_key is None or isinstance(next_key, str)


def test_client_library_list_pagination_advances(live_client):
    """The cursor returned by page N must yield a different first stub on page N+1."""
    page1, cursor = live_client.get_library_list(page_size=100)
    assert page1 and cursor, "first page must be non-empty and have a follow-up cursor"
    page2, _ = live_client.get_library_list(last_key=cursor, page_size=100)
    assert page2, "second page must not be empty"
    assert page1[0]["componentCode"] != page2[0]["componentCode"], (
        "cursor did not advance — page 2 starts on the same stub as page 1"
    )


def test_client_iter_library_stubs_total_size(live_client):
    """The full assembly library must contain at least 500 stubs."""
    total = 0
    for _ in live_client.iter_library_stubs():
        total += 1
        if total >= 500:
            break
    assert total >= 500, f"Only {total} library stubs — suspiciously low"


# ---------------------------------------------------------------------------
# DB import correctness
# ---------------------------------------------------------------------------

def test_known_parts_imported(basic_db):
    """All known codes must be importable and retrievable."""
    for code in KNOWN_BASIC_0402:
        part = basic_db.get(code)
        assert part is not None, f"Part {code} not found in DB after import"
        assert part["library_type"] == "Basic", (
            f"{code}: expected library_type='Basic', got {part['library_type']!r}"
        )
        assert part["package"] == "0402", (
            f"{code}: expected package='0402', got {part['package']!r}"
        )


def test_150k_0805_basic_imported(basic_db):
    """C17470 (150 kΩ 0805 Basic) must be importable."""
    part = basic_db.get(KNOWN_BASIC_0805_150K)
    assert part is not None, f"{KNOWN_BASIC_0805_150K} not found in DB"
    assert part["library_type"] == "Basic"
    assert "0805" in part["package"]


# ---------------------------------------------------------------------------
# Parametric search — end-to-end test of import → search pipeline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,code", list(KNOWN_BASIC_0402.items()))
def test_basic_0402_resistor_searchable(basic_db, value, code):
    """
    Each known Basic 0402 resistor must be findable by its nominal value.
    This exercises the full import → rebuild_specs → search_passive pipeline.
    """
    label = KNOWN_BASIC_0402[value]
    parts = basic_db.search_passive(
        component_type="resistor",
        value=label,
        package="0402",
        library_type="Basic",
        in_stock=True,
        limit=5,
    )
    assert parts, (
        f"search_passive returned no results for {label} 0402 Basic "
        f"(expected {value} to be in DB)"
    )
    lcsc_codes = [p["lcsc"] for p in parts]
    assert value in lcsc_codes, (
        f"{value} not in search results for '{label}': got {lcsc_codes}"
    )


def test_150k_searchable_in_0805(basic_db):
    """150 kΩ 0805 Basic must be findable by value search."""
    parts = basic_db.search_passive(
        component_type="resistor",
        value="150k",
        package="0805",
        library_type="Basic",
        in_stock=True,
        limit=5,
    )
    assert parts, "No 150kΩ 0805 Basic resistors found after import"
    assert any(p["lcsc"] == KNOWN_BASIC_0805_150K for p in parts), (
        f"{KNOWN_BASIC_0805_150K} not in search results: {[p['lcsc'] for p in parts]}"
    )


def test_120k_0402_is_extended_not_basic(live_client):
    """
    120 kΩ 0402 resistors are Extended at JLCPCB, not Basic.
    Confirms the user's original searches returning 0 Basic results were correct.
    """
    part = live_client.get_part_detail("C25750")  # 0402WGF1203TCE, 120kΩ
    assert part is not None, "C25750 (120kΩ 0402) not found via API"
    assert part["libraryType"] != "base", (
        f"C25750 is now Basic — update tests! libraryType={part['libraryType']!r}"
    )
