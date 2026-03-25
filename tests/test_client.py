"""Tests for lcsc_mcp/client.py."""
import base64
import hashlib
import hmac
import string
from unittest.mock import call

import pytest

from lcsc_mcp.client import (
    JLCPCBClient,
    _auth_header,
    _nonce,
    _require_env,
    _sign,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_require_env_present(monkeypatch):
    monkeypatch.setenv("_TEST_LCSC_VAR", "hello")
    assert _require_env("_TEST_LCSC_VAR") == "hello"


def test_require_env_missing(monkeypatch):
    monkeypatch.delenv("_TEST_LCSC_VAR", raising=False)
    with pytest.raises(EnvironmentError, match="_TEST_LCSC_VAR"):
        _require_env("_TEST_LCSC_VAR")


def test_nonce_length():
    assert len(_nonce()) == 32


def test_nonce_alphanumeric():
    valid = set(string.ascii_letters + string.digits)
    assert all(c in valid for c in _nonce())


def test_sign_deterministic():
    a = _sign("secret", "POST", "/path", 12345, "nonce", "body")
    b = _sign("secret", "POST", "/path", 12345, "nonce", "body")
    assert a == b


def test_sign_correct_hmac():
    secret, method, path, ts, nonce, body = "key", "POST", "/ep", 1, "n", "{}"
    msg = f"{method}\n{path}\n{ts}\n{nonce}\n{body}\n"
    expected = base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    assert _sign(secret, method, path, ts, nonce, body) == expected


def test_auth_header_format(monkeypatch):
    monkeypatch.setattr("lcsc_mcp.client._nonce", lambda: "TESTNONCE")
    monkeypatch.setattr("lcsc_mcp.client.time.time", lambda: 9999)
    h = _auth_header("APPID", "ACCESSKEY", "SECRET", "POST", "/ep", "{}")
    assert h.startswith('JOP appid="APPID"')
    assert 'accesskey="ACCESSKEY"' in h
    assert 'nonce="TESTNONCE"' in h
    assert 'timestamp="9999"' in h


# ---------------------------------------------------------------------------
# JLCPCBClient fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(env_vars):
    return JLCPCBClient()


def _mock_response(mocker, json_data):
    resp = mocker.MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

def test_init_missing_env(monkeypatch):
    for var in ("JLCPCB_APP_ID", "JLCPCB_API_KEY", "JLCPCB_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(EnvironmentError):
        JLCPCBClient()


# ---------------------------------------------------------------------------
# _post
# ---------------------------------------------------------------------------

def test_post_success(client, mocker):
    resp = _mock_response(mocker, {"code": 200, "data": {"key": "val"}})
    mocker.patch.object(client._session, "post", return_value=resp)
    assert client._post("/ep", {}) == {"key": "val"}


def test_post_empty_data(client, mocker):
    resp = _mock_response(mocker, {"code": 200, "data": None})
    mocker.patch.object(client._session, "post", return_value=resp)
    assert client._post("/ep", {}) == {}


def test_post_api_error_message(client, mocker):
    resp = _mock_response(mocker, {"code": 400, "message": "bad request"})
    mocker.patch.object(client._session, "post", return_value=resp)
    with pytest.raises(RuntimeError, match="bad request"):
        client._post("/ep", {})


def test_post_api_error_msg_fallback(client, mocker):
    resp = _mock_response(mocker, {"code": 500, "msg": "server error"})
    mocker.patch.object(client._session, "post", return_value=resp)
    with pytest.raises(RuntimeError, match="server error"):
        client._post("/ep", {})


def test_post_api_error_unknown(client, mocker):
    resp = _mock_response(mocker, {"code": 503})
    mocker.patch.object(client._session, "post", return_value=resp)
    with pytest.raises(RuntimeError, match="unknown error"):
        client._post("/ep", {})


# ---------------------------------------------------------------------------
# fetch_page
# ---------------------------------------------------------------------------

def test_fetch_page_no_key(client, mocker):
    mock_post = mocker.patch.object(client, "_post", return_value={})
    client.fetch_page()
    _, payload = mock_post.call_args[0]
    assert "lastKey" not in payload


def test_fetch_page_with_key(client, mocker):
    mock_post = mocker.patch.object(client, "_post", return_value={})
    client.fetch_page(last_key="k123")
    _, payload = mock_post.call_args[0]
    assert payload["lastKey"] == "k123"


# ---------------------------------------------------------------------------
# get_part_detail
# ---------------------------------------------------------------------------

def test_get_part_detail_found(client, mocker):
    mocker.patch.object(client, "_post", return_value={"lcscPart": "C25744"})
    assert client.get_part_detail("C25744")["lcscPart"] == "C25744"


def test_get_part_detail_list_response(client, mocker):
    mocker.patch.object(client, "_post", return_value=[{"lcscPart": "C25744"}])
    assert client.get_part_detail("C25744")["lcscPart"] == "C25744"


def test_get_part_detail_empty_list(client, mocker):
    mocker.patch.object(client, "_post", return_value=[])
    assert client.get_part_detail("C99999") is None


def test_get_part_detail_empty_dict(client, mocker):
    mocker.patch.object(client, "_post", return_value={})
    assert client.get_part_detail("C00000") is None


def test_get_part_detail_runtime_error(client, mocker):
    mocker.patch.object(client, "_post", side_effect=RuntimeError("not found"))
    assert client.get_part_detail("C00000") is None


# ---------------------------------------------------------------------------
# get_library_list
# ---------------------------------------------------------------------------

def test_get_library_list_no_filter(client, mocker):
    mock_post = mocker.patch.object(client, "_post", return_value={})
    client.get_library_list()
    _, payload = mock_post.call_args[0]
    assert "libraryType" not in payload
    assert "lastKey" not in payload


def test_get_library_list_with_type_and_key(client, mocker):
    mock_post = mocker.patch.object(client, "_post", return_value={})
    client.get_library_list(library_type="base", last_key="k1")
    _, payload = mock_post.call_args[0]
    assert payload["libraryType"] == "base"
    assert payload["lastKey"] == "k1"


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def test_download_basic(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    pages = [
        {"componentInfos": [{"lcscPart": "C1"}, {"lcscPart": "C2"}], "lastKey": "k1"},
        {"componentInfos": [{"lcscPart": "C3"}], "lastKey": None},
        {"componentInfos": []},
    ]
    mocker.patch.object(client, "fetch_page", side_effect=pages)

    batches = []
    total, last_key = client.download(on_batch=lambda p: batches.append(len(p)))
    assert total == 3
    assert last_key is None
    assert batches == [2, 1]


def test_download_resume(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    mock_fetch = mocker.patch.object(
        client, "fetch_page",
        return_value={"componentInfos": [], "lastKey": None},
    )
    client.download(
        on_batch=lambda p: None,
        checkpoint={"last_key": "resume_key", "total": 10},
    )
    mock_fetch.assert_called_with("resume_key")


def test_download_no_checkpoint(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    mock_fetch = mocker.patch.object(
        client, "fetch_page",
        return_value={"componentInfos": [], "lastKey": None},
    )
    client.download(on_batch=lambda p: None)
    mock_fetch.assert_called_with(None)


def test_download_progress_callback(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    pages = [
        {"componentInfos": [{"lcscPart": "C1"}], "lastKey": None},
        {"componentInfos": []},
    ]
    mocker.patch.object(client, "fetch_page", side_effect=pages)
    calls = []
    client.download(on_batch=lambda p: None, on_progress=lambda t, m: calls.append(t))
    assert calls == [1]


def test_download_no_progress_logs(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    pages = [
        {"componentInfos": [{"lcscPart": "C1"}], "lastKey": None},
        {"componentInfos": []},
    ]
    mocker.patch.object(client, "fetch_page", side_effect=pages)
    mock_log = mocker.patch("lcsc_mcp.client.logger")
    client.download(on_batch=lambda p: None)
    mock_log.info.assert_called()


def test_download_error_propagates(client, mocker):
    mocker.patch.object(client, "fetch_page", side_effect=RuntimeError("net error"))
    with pytest.raises(RuntimeError, match="net error"):
        client.download(on_batch=lambda p: None)


# ---------------------------------------------------------------------------
# download_library
# ---------------------------------------------------------------------------

def test_download_library_accumulates(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    pages = [
        {"componentInfos": [{"lcscPart": "C1"}, {"lcscPart": "C2"}], "lastKey": "k1"},
        {"componentInfos": [{"lcscPart": "C3"}], "lastKey": None},
        {"componentInfos": []},
    ]
    mocker.patch.object(client, "get_library_list", side_effect=pages)
    result = client.download_library()
    assert len(result) == 3


def test_download_library_bare_list_response(client, mocker):
    """API sometimes returns a bare list instead of {componentInfos: [...]}."""
    mocker.patch("lcsc_mcp.client.time.sleep")
    mocker.patch.object(client, "get_library_list", return_value=[{"lcscPart": "C1"}])
    result = client.download_library()
    assert len(result) == 1


def test_download_library_with_callback(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    pages = [
        {"componentInfos": [{"lcscPart": "C1"}], "lastKey": None},
        {"componentInfos": []},
    ]
    mocker.patch.object(client, "get_library_list", side_effect=pages)
    batches = []
    result = client.download_library(on_batch=lambda p: batches.append(p))
    assert result == []
    assert len(batches) == 1


def test_download_library_progress(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    pages = [
        {"componentInfos": [{"lcscPart": "C1"}], "lastKey": None},
        {"componentInfos": []},
    ]
    mocker.patch.object(client, "get_library_list", side_effect=pages)
    calls = []
    client.download_library(on_progress=lambda t, m: calls.append(t))
    assert calls == [1]


def test_download_library_empty_parts_with_last_key(client, mocker):
    """Empty componentInfos with a pending lastKey → break on 'if not parts' (line 240)."""
    mocker.patch("lcsc_mcp.client.time.sleep")
    pages = [
        {"componentInfos": [{"lcscPart": "C1"}], "lastKey": "k1"},
        {"componentInfos": [], "lastKey": "k2"},  # empty → break before last_key check
    ]
    mocker.patch.object(client, "get_library_list", side_effect=pages)
    result = client.download_library()
    assert len(result) == 1
