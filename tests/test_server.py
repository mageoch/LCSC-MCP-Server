"""Tests for lcsc_mcp/server.py — MCP tools."""
import json
import sys
import time

import pytest

import lcsc_mcp.server as server
from lcsc_mcp import server as srv


# ---------------------------------------------------------------------------
# _db / _client factories
# ---------------------------------------------------------------------------

def test_db_default_path(monkeypatch):
    monkeypatch.delenv("LCSC_DB_PATH", raising=False)
    db = server._db()
    assert "lcsc_parts" in str(db.path)
    db.close()


def test_db_custom_path(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom.db")
    monkeypatch.setenv("LCSC_DB_PATH", custom)
    db = server._db()
    assert str(db.path) == custom
    db.close()


def test_client_missing_env(monkeypatch):
    for var in ("JLCPCB_APP_ID", "JLCPCB_API_KEY", "JLCPCB_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(EnvironmentError):
        server._client()


# ---------------------------------------------------------------------------
# _ensure_basic_library
# ---------------------------------------------------------------------------

def test_ensure_fresh(mocker):
    mock_db = mocker.MagicMock()
    mock_db.is_library_stale.return_value = False
    result = server._ensure_basic_library(mock_db)
    assert result is None
    mock_db.import_batch.assert_not_called()


def test_ensure_stale_age_none(mocker):
    """Stale with age=None (never fetched) → on_batch called → line 78 covered."""
    mock_db = mocker.MagicMock()
    mock_db.is_library_stale.return_value = True
    mock_db.library_age_hours.return_value = None

    mock_client = mocker.MagicMock()

    def fake_download_library(library_type=None, on_batch=None, on_progress=None):
        if on_batch:
            on_batch([{"lcscPart": "C1"}])

    mock_client.download_library.side_effect = fake_download_library
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server._ensure_basic_library(mock_db)
    assert result is None
    mock_db.import_batch.assert_called_once()


def test_ensure_stale_with_age(mocker):
    """Stale with age set → logs 'refreshing'."""
    mock_db = mocker.MagicMock()
    mock_db.is_library_stale.return_value = True
    mock_db.library_age_hours.return_value = 30.0

    mock_client = mocker.MagicMock()
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server._ensure_basic_library(mock_db)
    assert result is None


def test_ensure_stale_client_error(mocker):
    mock_db = mocker.MagicMock()
    mock_db.is_library_stale.return_value = True
    mock_db.library_age_hours.return_value = None
    mocker.patch("lcsc_mcp.server._client", side_effect=EnvironmentError("no creds"))

    result = server._ensure_basic_library(mock_db)
    assert result is not None
    assert "Auto-refresh failed" in result


def test_ensure_stale_download_error(mocker):
    mock_db = mocker.MagicMock()
    mock_db.is_library_stale.return_value = True
    mock_db.library_age_hours.return_value = 30.0

    mock_client = mocker.MagicMock()
    mock_client.download_library.side_effect = RuntimeError("API down")
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server._ensure_basic_library(mock_db)
    assert "Auto-refresh failed" in result


# ---------------------------------------------------------------------------
# download_database
# ---------------------------------------------------------------------------

def _make_mock_client_download(mocker, parts=None, last_key=None):
    """Returns a side_effect function that simulates client.download()."""
    def _download(on_batch, on_progress=None, checkpoint=None):
        if parts:
            on_batch(parts)
        if on_progress:
            on_progress(len(parts or []), "page 1")
        return len(parts or []), last_key
    return _download


def test_download_database_success(mocker, tmp_path):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {"total": 3, "basic": 1}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    mock_client.download.side_effect = _make_mock_client_download(mocker, [{"lcscPart": "C1"}])
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)
    mocker.patch("lcsc_mcp.server._CHECKPOINT", tmp_path / "ck.json")

    result = server.download_database()
    assert result["success"] is True
    mock_db.rebuild_fts.assert_called_once()


def test_download_database_force_clears(mocker, tmp_path):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._client", return_value=mocker.MagicMock(
        download=lambda **kw: (0, None)
    ))
    ck_path = tmp_path / "ck.json"
    ck_path.write_text('{"last_key":"k1","total":5}')
    mocker.patch("lcsc_mcp.server._CHECKPOINT", ck_path)

    server.download_database(force=True)
    mock_db.clear.assert_called_once()
    assert not ck_path.exists()


def test_download_database_force_no_checkpoint(mocker, tmp_path):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._client", return_value=mocker.MagicMock(
        download=lambda **kw: (0, None)
    ))
    mocker.patch("lcsc_mcp.server._CHECKPOINT", tmp_path / "nonexistent.json")

    server.download_database(force=True)
    mock_db.clear.assert_called_once()


def test_download_database_resume(mocker, tmp_path):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    received_checkpoint = {}

    def mock_download(on_batch, on_progress=None, checkpoint=None):
        received_checkpoint.update(checkpoint or {})
        return 5, None

    mock_client = mocker.MagicMock()
    mock_client.download.side_effect = mock_download
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    ck_path = tmp_path / "ck.json"
    ck_path.write_text('{"last_key":"resume_key","total":5}')
    mocker.patch("lcsc_mcp.server._CHECKPOINT", ck_path)

    server.download_database(force=False)
    assert received_checkpoint.get("last_key") == "resume_key"


def test_download_database_bad_checkpoint(mocker, tmp_path):
    """Malformed checkpoint JSON → checkpoint=None, download still proceeds."""
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._client", return_value=mocker.MagicMock(
        download=lambda on_batch, on_progress=None, checkpoint=None: (0, None)
    ))
    ck_path = tmp_path / "ck.json"
    ck_path.write_text("NOT_JSON{{{")
    mocker.patch("lcsc_mcp.server._CHECKPOINT", ck_path)

    result = server.download_database(force=False)
    assert result["success"] is True


def test_download_database_removes_checkpoint_on_success(mocker, tmp_path):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._client", return_value=mocker.MagicMock(
        download=lambda on_batch, on_progress=None, checkpoint=None: (0, None)
    ))
    ck_path = tmp_path / "ck.json"
    ck_path.write_text('{"last_key":null,"total":0}')
    mocker.patch("lcsc_mcp.server._CHECKPOINT", ck_path)

    server.download_database()
    assert not ck_path.exists()


def test_download_database_error(mocker, tmp_path):
    mocker.patch("lcsc_mcp.server._db", return_value=mocker.MagicMock())
    mock_client = mocker.MagicMock()
    mock_client.download.side_effect = RuntimeError("net error")
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)
    mocker.patch("lcsc_mcp.server._CHECKPOINT", tmp_path / "ck.json")

    result = server.download_database()
    assert result["success"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# download_library
# ---------------------------------------------------------------------------

def test_download_library_all(mocker):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {"total": 5}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mock_client = mocker.MagicMock()
    mock_client.download_library.return_value = None
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.download_library(library_type="all")
    assert result["success"] is True
    # api_type=None → set_metadata called
    mock_db.set_metadata.assert_called()


def test_download_library_basic(mocker):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mock_client = mocker.MagicMock()
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    server.download_library(library_type="basic")
    args, kwargs = mock_client.download_library.call_args
    assert kwargs.get("library_type") == "base"
    mock_db.set_metadata.assert_called()


def test_download_library_extended(mocker):
    """Extended → api_type='extend' → set_metadata NOT called."""
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mock_client = mocker.MagicMock()
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    server.download_library(library_type="extended")
    mock_db.set_metadata.assert_not_called()


def test_download_library_with_on_batch(mocker):
    """on_batch callback is called → line 202 covered."""
    mock_db = mocker.MagicMock()
    mock_db.import_batch.return_value = 2
    mock_db.stats.return_value = {"total": 2}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()

    def fake_download_library(library_type=None, on_batch=None, on_progress=None):
        if on_batch:
            on_batch([{"lcscPart": "C1"}, {"lcscPart": "C2"}])

    mock_client.download_library.side_effect = fake_download_library
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.download_library()
    assert result["success"] is True
    mock_db.import_batch.assert_called_once()


def test_download_library_error(mocker):
    mocker.patch("lcsc_mcp.server._db", return_value=mocker.MagicMock())
    mock_client = mocker.MagicMock()
    mock_client.download_library.side_effect = RuntimeError("API down")
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.download_library()
    assert result["success"] is False


# ---------------------------------------------------------------------------
# search_parts
# ---------------------------------------------------------------------------

def test_search_parts_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search.return_value = [{"lcsc": "C1"}]
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value=None)

    result = server.search_parts(query="resistor")
    assert result["success"] is True
    assert result["count"] == 1
    assert "warning" not in result


def test_search_parts_with_warning(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search.return_value = []
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value="results may be stale")

    result = server.search_parts()
    assert result["warning"] == "results may be stale"


# ---------------------------------------------------------------------------
# search_resistors
# ---------------------------------------------------------------------------

def test_search_resistors_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search_passive.return_value = [{"lcsc": "C25744"}]
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value=None)

    result = server.search_resistors(value="10k", package="0402", library_type="Basic")
    assert result["success"] is True
    assert result["count"] == 1
    assert "warning" not in result


def test_search_resistors_with_warning(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search_passive.return_value = []
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value="stale")

    result = server.search_resistors()
    assert "warning" in result


# ---------------------------------------------------------------------------
# search_capacitors
# ---------------------------------------------------------------------------

def test_search_capacitors_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search_passive.return_value = [{"lcsc": "C1525"}]
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value=None)

    result = server.search_capacitors(value="100nF", dielectric="X7R")
    assert result["success"] is True
    assert "warning" not in result


def test_search_capacitors_with_warning(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search_passive.return_value = []
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value="stale")

    result = server.search_capacitors()
    assert "warning" in result


# ---------------------------------------------------------------------------
# search_inductors
# ---------------------------------------------------------------------------

def test_search_inductors_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search_passive.return_value = [{"lcsc": "C1044"}]
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value=None)

    result = server.search_inductors(value="10nH")
    assert result["success"] is True
    assert "warning" not in result


def test_search_inductors_with_warning(mocker):
    mock_db = mocker.MagicMock()
    mock_db.search_passive.return_value = []
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)
    mocker.patch("lcsc_mcp.server._ensure_basic_library", return_value="stale")

    result = server.search_inductors()
    assert "warning" in result


# ---------------------------------------------------------------------------
# rebuild_component_specs
# ---------------------------------------------------------------------------

def test_rebuild_specs_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.rebuild_specs.return_value = 42
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    result = server.rebuild_component_specs()
    assert result == {"success": True, "passives_indexed": 42}


def test_rebuild_specs_error(mocker):
    mock_db = mocker.MagicMock()
    mock_db.rebuild_specs.side_effect = RuntimeError("DB locked")
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    result = server.rebuild_component_specs()
    assert result["success"] is False
    assert "DB locked" in result["error"]


# ---------------------------------------------------------------------------
# get_part
# ---------------------------------------------------------------------------

def test_get_part_local_fresh(mocker):
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = 1.0  # fresh
    mock_db.get.return_value = {"lcsc": "C25744"}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    result = server.get_part("C25744", live=False)
    assert result["success"] is True
    assert result["source"] == "local_db"


def test_get_part_local_fresh_no_part(mocker):
    """Age < TTL but get() returns None → falls through to API."""
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = 1.0
    mock_db.get.return_value = None
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    raw = {"lcscPart": "C25744", "description": "10k"}
    mock_client.get_part_detail.return_value = raw
    mock_db.get.side_effect = [None, {"lcsc": "C25744"}]  # first call None, second after upsert
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C25744", live=False)
    assert result["success"] is True
    assert result["source"] == "api"


def test_get_part_stale_api_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = 25.0  # stale
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    raw = {"lcscPart": "C25744"}
    mock_client.get_part_detail.return_value = raw
    mock_db.get.return_value = {"lcsc": "C25744"}
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C25744")
    assert result["source"] == "api"


def test_get_part_not_in_db_api_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = None  # not in DB
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    raw = {"lcscPart": "C25744"}
    mock_client.get_part_detail.return_value = raw
    mock_db.get.return_value = {"lcsc": "C25744"}
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C25744")
    assert result["source"] == "api"


def test_get_part_api_returns_none_fallback_stale(mocker):
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = None
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    mock_client.get_part_detail.return_value = None
    mock_db.get.return_value = {"lcsc": "C25744"}  # stale cache available
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C25744")
    assert result["source"] == "local_db_stale"


def test_get_part_api_returns_none_no_cache(mocker):
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = None
    mock_db.get.return_value = None
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    mock_client.get_part_detail.return_value = None
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C99999")
    assert result["success"] is False


def test_get_part_network_error_stale_cache(mocker):
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = None
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    mock_client.get_part_detail.side_effect = RuntimeError("network down")
    mock_db.get.return_value = {"lcsc": "C25744"}
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C25744")
    assert result["source"] == "local_db_stale"


def test_get_part_network_error_no_cache(mocker):
    mock_db = mocker.MagicMock()
    mock_db.part_age_hours.return_value = None
    mock_db.get.return_value = None
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    mock_client.get_part_detail.side_effect = RuntimeError("network down")
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C25744")
    assert result["success"] is False


def test_get_part_live_flag(mocker):
    """live=True skips local cache even if part is fresh."""
    mock_db = mocker.MagicMock()
    mock_db.get.return_value = {"lcsc": "C25744"}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    mock_client = mocker.MagicMock()
    mock_client.get_part_detail.return_value = {"lcscPart": "C25744"}
    mocker.patch("lcsc_mcp.server._client", return_value=mock_client)

    result = server.get_part("C25744", live=True)
    assert result["source"] == "api"
    mock_db.part_age_hours.assert_not_called()


# ---------------------------------------------------------------------------
# suggest_alternatives
# ---------------------------------------------------------------------------

def test_suggest_alternatives_found(mocker):
    mock_db = mocker.MagicMock()
    mock_db.get.return_value = {
        "lcsc": "C25744",
        "library_type": "Basic",
        "stock": 1000000,
        "price_breaks": [{"qty": 20, "price": 0.001}],
    }
    mock_db.suggest_alternatives.return_value = [{"lcsc": "C25105"}]
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    result = server.suggest_alternatives("C25744")
    assert result["success"] is True
    assert result["reference"]["price"] == pytest.approx(0.001)
    assert len(result["alternatives"]) == 1


def test_suggest_alternatives_no_price_breaks(mocker):
    mock_db = mocker.MagicMock()
    mock_db.get.return_value = {
        "lcsc": "C25744",
        "library_type": "Basic",
        "stock": 100,
        "price_breaks": [],
    }
    mock_db.suggest_alternatives.return_value = []
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    result = server.suggest_alternatives("C25744")
    assert result["reference"]["price"] is None


def test_suggest_alternatives_not_found(mocker):
    mock_db = mocker.MagicMock()
    mock_db.get.return_value = None
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    result = server.suggest_alternatives("C00000")
    assert result["success"] is False


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

def test_get_stats_success(mocker):
    mock_db = mocker.MagicMock()
    mock_db.stats.return_value = {"total": 100, "basic": 50}
    mocker.patch("lcsc_mcp.server._db", return_value=mock_db)

    result = server.get_stats()
    assert result["success"] is True
    assert result["total"] == 100


def test_get_stats_error(mocker):
    mocker.patch("lcsc_mcp.server._db", side_effect=RuntimeError("DB error"))
    result = server.get_stats()
    assert result["success"] is False


# ---------------------------------------------------------------------------
# download_kicad_component
# ---------------------------------------------------------------------------

@pytest.fixture
def easyeda_mocks(mocker, tmp_path):
    """Mock all easyeda2kicad imports used inside download_kicad_component."""
    # ---- EasyedaApi ----
    mock_cad_data = mocker.MagicMock()
    mock_api = mocker.MagicMock()
    mock_api.get_cad_data_of_component.return_value = mock_cad_data
    mocker.patch("easyeda2kicad.easyeda.easyeda_api.EasyedaApi", return_value=mock_api)

    # ---- Symbol ----
    mock_symbol = mocker.MagicMock()
    mock_symbol.info.name = "TEST_PART"
    mock_sym_importer = mocker.MagicMock()
    mock_sym_importer.get_symbol.return_value = mock_symbol
    mocker.patch("easyeda2kicad.easyeda.easyeda_importer.EasyedaSymbolImporter", return_value=mock_sym_importer)

    mock_sym_exporter = mocker.MagicMock()
    mock_sym_exporter.export.return_value = "(symbol content)"
    mocker.patch("easyeda2kicad.kicad.export_kicad_symbol.ExporterSymbolKicad", return_value=mock_sym_exporter)
    mocker.patch("easyeda2kicad.kicad.parameters_kicad_symbol.KicadVersion")

    mock_id_check = mocker.patch("easyeda2kicad.__main__.id_already_in_symbol_lib", return_value=False)
    mock_add = mocker.patch("easyeda2kicad.__main__.add_component_in_symbol_lib_file")
    mock_update = mocker.patch("easyeda2kicad.__main__.update_component_in_symbol_lib_file")

    # ---- Footprint ----
    mock_fp = mocker.MagicMock()
    mock_fp.info.name = "TEST_FP"
    mock_fp_importer = mocker.MagicMock()
    mock_fp_importer.get_footprint.return_value = mock_fp
    mocker.patch("easyeda2kicad.easyeda.easyeda_importer.EasyedaFootprintImporter", return_value=mock_fp_importer)

    mock_fp_exporter = mocker.MagicMock()
    mocker.patch("easyeda2kicad.kicad.export_kicad_footprint.ExporterFootprintKicad", return_value=mock_fp_exporter)
    mock_fp_check = mocker.patch("easyeda2kicad.__main__.fp_already_in_footprint_lib", return_value=False)

    # ---- 3D model ----
    mock_3d_output = mocker.MagicMock()
    mock_3d_output.name = "TEST_3D"
    mock_3d_importer = mocker.MagicMock()
    mock_3d_importer.output = mock_3d_output

    mock_3d_exporter = mocker.MagicMock()
    mock_3d_exporter.output = mock_3d_output
    mock_3d_exporter.output_step = mock_3d_output
    mocker.patch("easyeda2kicad.easyeda.easyeda_importer.Easyeda3dModelImporter", return_value=mock_3d_importer)
    mocker.patch("easyeda2kicad.kicad.export_kicad_3d_model.Exporter3dModelKicad", return_value=mock_3d_exporter)

    return {
        "cad_data": mock_cad_data,
        "api": mock_api,
        "symbol": mock_symbol,
        "id_check": mock_id_check,
        "add": mock_add,
        "update": mock_update,
        "fp_check": mock_fp_check,
        "fp_exporter": mock_fp_exporter,
        "3d_exporter": mock_3d_exporter,
        "tmp_path": tmp_path,
    }


def test_download_kicad_import_error(mocker):
    mocker.patch.dict(sys.modules, {"easyeda2kicad.easyeda.easyeda_api": None})
    result = server.download_kicad_component("C25744", output="/tmp/test")
    assert result["success"] is False
    assert "easyeda2kicad" in result["error"]


def test_download_kicad_no_data(easyeda_mocks):
    easyeda_mocks["api"].get_cad_data_of_component.return_value = None
    result = server.download_kicad_component(
        "C00000", output=str(easyeda_mocks["tmp_path"] / "EasyEDA")
    )
    assert result["success"] is False
    assert "No EasyEDA data" in result["error"]


def test_download_kicad_create_all(easyeda_mocks):
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base)
    assert result["success"] is True
    assert result["files"]["symbol"].get("created") is True
    assert result["files"]["footprint"].get("created") is True
    assert result["files"]["model_3d"].get("created") is True


def test_download_kicad_symbol_update(easyeda_mocks):
    easyeda_mocks["id_check"].return_value = True  # already in lib
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base, overwrite=True)
    assert result["files"]["symbol"].get("updated") is True
    easyeda_mocks["update"].assert_called_once()


def test_download_kicad_symbol_skip(easyeda_mocks):
    easyeda_mocks["id_check"].return_value = True
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base, overwrite=False)
    assert result["files"]["symbol"].get("skipped") is True


def test_download_kicad_footprint_update(easyeda_mocks):
    easyeda_mocks["fp_check"].return_value = True
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base, overwrite=True)
    assert result["files"]["footprint"].get("updated") is True


def test_download_kicad_footprint_skip(easyeda_mocks):
    easyeda_mocks["fp_check"].return_value = True
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base, overwrite=False)
    assert result["files"]["footprint"].get("skipped") is True


def test_download_kicad_3d_no_output(easyeda_mocks):
    easyeda_mocks["3d_exporter"].output = None
    easyeda_mocks["3d_exporter"].output_step = None
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base)
    assert result["files"]["model_3d"].get("skipped") is True


def test_download_kicad_3d_no_step(easyeda_mocks):
    easyeda_mocks["3d_exporter"].output_step = None
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base)
    assert result["files"]["model_3d"].get("created") is True


def test_download_kicad_symbol_error(easyeda_mocks, mocker):
    mocker.patch(
        "easyeda2kicad.easyeda.easyeda_importer.EasyedaSymbolImporter",
        side_effect=RuntimeError("sym error"),
    )
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base)
    assert "error" in result["files"]["symbol"]


def test_download_kicad_footprint_error(easyeda_mocks, mocker):
    mocker.patch(
        "easyeda2kicad.easyeda.easyeda_importer.EasyedaFootprintImporter",
        side_effect=RuntimeError("fp error"),
    )
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base)
    assert "error" in result["files"]["footprint"]


def test_download_kicad_3d_error(easyeda_mocks, mocker):
    mocker.patch(
        "easyeda2kicad.easyeda.easyeda_importer.Easyeda3dModelImporter",
        side_effect=RuntimeError("3d error"),
    )
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base)
    assert "error" in result["files"]["model_3d"]


def test_download_kicad_symbol_false(easyeda_mocks):
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base, symbol=False)
    assert "symbol" not in result["files"]


def test_download_kicad_footprint_false(easyeda_mocks):
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base, footprint=False)
    assert "footprint" not in result["files"]


def test_download_kicad_3d_false(easyeda_mocks):
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component("C25744", output=lib_base, model_3d=False)
    assert "model_3d" not in result["files"]


def test_download_kicad_default_output(easyeda_mocks, monkeypatch):
    """output=None → uses LCSC_EASYEDA_LIB_PATH env var."""
    monkeypatch.setenv("LCSC_EASYEDA_LIB_PATH", str(easyeda_mocks["tmp_path"] / "EasyEDA"))
    monkeypatch.delenv("LCSC_EASYEDA_3D_PATH", raising=False)
    result = server.download_kicad_component("C25744")
    assert result["success"] is True


def test_download_kicad_model_3d_path(easyeda_mocks):
    lib_base = str(easyeda_mocks["tmp_path"] / "EasyEDA")
    result = server.download_kicad_component(
        "C25744", output=lib_base, model_3d_path="${MY_LIB}/3dshapes"
    )
    assert result["success"] is True


def test_download_kicad_existing_sym_lib(easyeda_mocks, tmp_path):
    """When .kicad_sym already exists, the creation block is skipped."""
    lib_base = str(tmp_path / "EasyEDA")
    sym_lib = tmp_path / "EasyEDA.kicad_sym"
    sym_lib.write_text("(kicad_symbol_lib)", encoding="utf-8")
    result = server.download_kicad_component("C25744", output=lib_base)
    assert result["success"] is True
    assert "symbol" in result["files"]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def test_main(mocker):
    mock_run = mocker.patch.object(server.mcp, "run")
    server.main()
    mock_run.assert_called_once()
