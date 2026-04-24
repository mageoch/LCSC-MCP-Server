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
# _normalize_detail
# ---------------------------------------------------------------------------

_SAMPLE_DETAIL = {
    "componentCode": "C25744",
    "firstTypeName": "Resistors",
    "secondTypeName": "Chip Resistor - Surface Mount",
    "componentModel": "0402WGF1002TCE",
    "componentSpecification": "0402",
    "solderJointCount": 2,
    "libraryType": "base",
    "description": "10kΩ ±1% 1/16W",
    "datasheetUrl": "https://example.com/ds.pdf",
    "stockCount": 1000000,
    "priceRanges": [
        {"startQuantity": 1, "endQuantity": 999, "unitPrice": 0.001},
        {"startQuantity": 1000, "endQuantity": -1, "unitPrice": 0.0008},
    ],
}


def test_normalize_detail_fields():
    n = JLCPCBClient._normalize_detail(_SAMPLE_DETAIL)
    assert n["lcscPart"] == "C25744"
    assert n["firstCategory"] == "Resistors"
    assert n["mfrPart"] == "0402WGF1002TCE"
    assert n["package"] == "0402"
    assert n["stock"] == 1000000
    assert n["datasheet"] == "https://example.com/ds.pdf"


def test_normalize_detail_price_string():
    n = JLCPCBClient._normalize_detail(_SAMPLE_DETAIL)
    # priceRanges → "1-999:0.001,1000-1000:0.0008"
    assert "1-999:0.001" in n["price"]
    assert "1000-1000:0.0008" in n["price"]


def test_normalize_detail_datasheet_empty():
    """Missing datasheetUrl → empty string."""
    raw = {**_SAMPLE_DETAIL, "datasheetUrl": ""}
    n = JLCPCBClient._normalize_detail(raw)
    assert n["datasheet"] == ""


def test_normalize_detail_no_price_ranges():
    raw = {**_SAMPLE_DETAIL, "priceRanges": []}
    n = JLCPCBClient._normalize_detail(raw)
    assert n["price"] == ""


# ---------------------------------------------------------------------------
# get_part_detail
# ---------------------------------------------------------------------------

def test_get_part_detail_found(client, mocker):
    mocker.patch.object(client, "_post", return_value={"componentDetailResponseVOList": [_SAMPLE_DETAIL]})
    result = client.get_part_detail("C25744")
    assert result["lcscPart"] == "C25744"


def test_get_part_detail_uses_correct_param(client, mocker):
    """API call uses 'componentCodes' list, not 'componentCode' string."""
    mock_post = mocker.patch.object(client, "_post", return_value=[_SAMPLE_DETAIL])
    client.get_part_detail("C25744")
    _, payload = mock_post.call_args[0]
    assert payload == {"componentCodes": ["C25744"]}


def test_get_part_detail_empty_list(client, mocker):
    mocker.patch.object(client, "_post", return_value={"componentDetailResponseVOList": []})
    assert client.get_part_detail("C99999") is None


def test_get_part_detail_non_list_response(client, mocker):
    """Unexpected non-list response → None."""
    mocker.patch.object(client, "_post", return_value={})
    assert client.get_part_detail("C00000") is None


def test_get_part_detail_runtime_error(client, mocker):
    mocker.patch.object(client, "_post", side_effect=RuntimeError("API error 401: unauthorized"))
    with pytest.raises(RuntimeError, match="401"):
        client.get_part_detail("C00000")


# ---------------------------------------------------------------------------
# get_library_list
# ---------------------------------------------------------------------------

def test_get_library_list_no_filter(client, mocker):
    mock_post = mocker.patch.object(client, "_post", return_value={})
    client.get_library_list()
    _, payload = mock_post.call_args[0]
    assert "libraryType" not in payload
    assert "lastKey" not in payload


def test_get_library_list_with_last_key(client, mocker):
    mock_post = mocker.patch.object(client, "_post", return_value={})
    client.get_library_list(last_key="cursor123", page_size=500)
    _, payload = mock_post.call_args[0]
    assert payload["lastKey"] == "cursor123"
    assert payload["pageSize"] == 500
    assert "libraryType" not in payload
    assert "currentPage" not in payload


def test_get_library_list_non_dict_response(client, mocker):
    """Non-dict response (e.g. None) → returns ([], None)."""
    mocker.patch.object(client, "_post", return_value=None)
    assert client.get_library_list() == ([], None)


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

def _make_stub(code: str) -> dict:
    return {"componentCode": code, "componentModel": code, "componentSpecification": "SMD"}


def _make_detail(code: str) -> dict:
    return {"componentCode": code, "componentModel": code, "componentSpecification": "SMD",
            "firstTypeName": "Resistors", "secondTypeName": "Chip", "libraryType": "base",
            "description": "", "datasheetUrl": "", "solderJointCount": 2,
            "priceRanges": [{"startQuantity": 1, "endQuantity": 9, "unitPrice": 0.01}],
            "stockCount": 100}


def test_download_library_accumulates(client, mocker):
    """Non-null lastKey triggers page 2; null lastKey ends loop."""
    mocker.patch("lcsc_mcp.client.time.sleep")
    page1_stubs = [_make_stub(f"C{i}") for i in range(1000)]
    page2_stubs = [_make_stub("C9999")]
    mocker.patch.object(client, "get_library_list", side_effect=[
        (page1_stubs, "cursor_key"),
        (page2_stubs, None),
    ])
    mocker.patch.object(client, "_post", side_effect=[
        {"componentDetailResponseVOList": [_make_detail(s["componentCode"]) for s in page1_stubs]},
        {"componentDetailResponseVOList": [_make_detail(s["componentCode"]) for s in page2_stubs]},
    ])
    result = client.download_library()
    assert len(result) == 1001


def test_download_library_with_callback(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    stubs = [_make_stub("C1")]
    mocker.patch.object(client, "get_library_list", side_effect=[(stubs, None)])
    mocker.patch.object(client, "_post", return_value={"componentDetailResponseVOList": [_make_detail("C1")]})
    batches = []
    result = client.download_library(on_batch=lambda p: batches.append(p))
    assert result == []
    assert len(batches) == 1


def test_download_library_progress(client, mocker):
    mocker.patch("lcsc_mcp.client.time.sleep")
    stubs = [_make_stub("C1")]
    mocker.patch.object(client, "get_library_list", side_effect=[(stubs, None)])
    mocker.patch.object(client, "_post", return_value={"componentDetailResponseVOList": [_make_detail("C1")]})
    calls = []
    client.download_library(on_progress=lambda t, m: calls.append(t))
    assert calls == [1]


def test_download_library_empty_first_page(client, mocker):
    """Empty first page → returns nothing without calling detail endpoint."""
    mocker.patch("lcsc_mcp.client.time.sleep")
    mocker.patch.object(client, "get_library_list", return_value=([], None))
    post_mock = mocker.patch.object(client, "_post")
    result = client.download_library()
    assert result == []
    post_mock.assert_not_called()


def test_download_library_non_list_detail_response(client, mocker):
    """_post returns dict without known key for detail endpoint → parts = [], no import."""
    mocker.patch("lcsc_mcp.client.time.sleep")
    stubs = [_make_stub("C1")]
    mocker.patch.object(client, "get_library_list", side_effect=[(stubs, None)])
    mocker.patch.object(client, "_post", return_value={"unexpected": "dict"})
    result = client.download_library()
    assert result == []


def test_download_library_detail_enrichment(client, mocker):
    """Verifies codes are passed to ENDPOINT_DETAIL and results are normalized."""
    mocker.patch("lcsc_mcp.client.time.sleep")
    stubs = [_make_stub("C580905"), _make_stub("C110499")]
    mocker.patch.object(client, "get_library_list", side_effect=[(stubs, None)])
    detail_raw = [_make_detail("C580905"), _make_detail("C110499")]
    post_mock = mocker.patch.object(client, "_post",
                                    return_value={"componentDetailResponseVOList": detail_raw})
    result = client.download_library()
    assert len(result) == 2
    codes_sent = post_mock.call_args[0][1]["componentCodes"]
    assert set(codes_sent) == {"C580905", "C110499"}
