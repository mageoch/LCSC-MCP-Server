"""
JLCPCB official API client — HMAC-SHA256 authentication.

Env vars required:
  JLCPCB_APP_ID
  JLCPCB_API_KEY
  JLCPCB_API_SECRET
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

BASE_URL = "https://open.jlcpcb.com"
_BASE = "/overseas/openapi/component"

ENDPOINT_INFOS    = f"{_BASE}/getComponentInfos"        # bulk catalog (cursor-paginated)
ENDPOINT_DETAIL   = f"{_BASE}/getComponentDetailByCode"  # single part lookup by LCSC code
ENDPOINT_LIB_LIST = f"{_BASE}/getComponentLibraryList"   # Basic/Extended library list


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(
            f"Missing environment variable: {name}. "
            "Set JLCPCB_APP_ID, JLCPCB_API_KEY and JLCPCB_API_SECRET."
        )
    return value


def _nonce() -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(32))


def _sign(secret_key: str, method: str, path: str, timestamp: int, nonce: str, body: str) -> str:
    msg = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n"
    sig = hmac.new(
        secret_key.encode(),
        msg.encode(),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(sig).decode()


def _auth_header(app_id: str, access_key: str, secret_key: str, method: str, path: str, body: str) -> str:
    ts = int(time.time())
    n = _nonce()
    sig = _sign(secret_key, method, path, ts, n, body)
    return (
        f'JOP appid="{app_id}",accesskey="{access_key}",'
        f'nonce="{n}",timestamp="{ts}",signature="{sig}"'
    )


class JLCPCBClient:
    """Thread-safe JLCPCB API client with persistent connection pool."""

    def __init__(self) -> None:
        self.app_id = _require_env("JLCPCB_APP_ID")
        self.access_key = _require_env("JLCPCB_API_KEY")
        self.secret_key = _require_env("JLCPCB_API_SECRET")

        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4)
        self._session.mount("https://", adapter)

    def _post(self, endpoint: str, payload: dict, timeout: int = 60) -> dict:
        """Signed POST → returns response['data']."""
        body = json.dumps(payload, separators=(",", ":"))
        auth = _auth_header(self.app_id, self.access_key, self.secret_key, "POST", endpoint, body)
        resp = self._session.post(
            f"{BASE_URL}{endpoint}",
            headers={"Authorization": auth, "Content-Type": "application/json"},
            data=body.encode(),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            msg = data.get("message") or data.get("msg") or "unknown error"
            raise RuntimeError(f"JLCPCB API error {data.get('code')}: {msg}")
        return data.get("data") or {}

    def fetch_page(self, last_key: Optional[str] = None) -> dict:
        """Fetch one page of the full catalog (cursor-based). Returns the API 'data' dict."""
        payload: dict = {}
        if last_key:
            payload["lastKey"] = last_key
        return self._post(ENDPOINT_INFOS, payload)

    def get_part_detail(self, lcsc_code: str) -> Optional[dict]:
        """
        Fetch live detail for a single component by LCSC code (e.g. 'C25804').
        Returns the component dict or None if not found.
        """
        try:
            data = self._post(ENDPOINT_DETAIL, {"componentCode": lcsc_code})
            # API returns a single component object or a list depending on version
            if isinstance(data, list):
                return data[0] if data else None
            return data or None
        except RuntimeError:
            return None

    def get_library_list(
        self,
        library_type: Optional[str] = None,
        last_key: Optional[str] = None,
    ) -> dict:
        """
        Fetch one page of the Basic/Extended assembly library list.

        Args:
            library_type: 'base' for Basic, 'extend' for Extended, None for all.
            last_key: Pagination cursor from previous call.

        Returns:
            API 'data' dict with 'componentInfos' and optional 'lastKey'.
        """
        payload: dict = {}
        if library_type:
            payload["libraryType"] = library_type
        if last_key:
            payload["lastKey"] = last_key
        return self._post(ENDPOINT_LIB_LIST, payload)

    def download(
        self,
        on_batch: Callable[[list], None],
        on_progress: Optional[Callable[[int, str], None]] = None,
        checkpoint: Optional[dict] = None,
    ) -> tuple[int, Optional[str]]:
        """
        Stream the full catalog, calling on_batch(parts) for each page.

        Overlaps DB writes with network fetches via a background thread.

        Args:
            on_batch: Called with each page's parts list (may run in a worker thread).
            on_progress: Optional callback(total_fetched, message).
            checkpoint: Dict with {"last_key": str, "total": int} to resume.

        Returns:
            (total_fetched, final_last_key) — last_key is None on completion.
        """
        last_key: Optional[str] = checkpoint.get("last_key") if checkpoint else None
        total = checkpoint.get("total", 0) if checkpoint else 0
        page = 0

        with ThreadPoolExecutor(max_workers=1) as writer:
            pending: Optional[Future] = None

            def flush():
                if pending is not None:
                    pending.result()

            while True:
                page += 1
                try:
                    data = self.fetch_page(last_key)
                    parts = data.get("componentInfos", [])

                    flush()  # wait for previous write before we inspect results

                    if not parts:
                        break

                    next_key = data.get("lastKey")
                    pending = writer.submit(on_batch, parts)
                    total += len(parts)
                    last_key = next_key

                    if on_progress:
                        on_progress(total, f"Page {page}: {total} parts fetched")
                    else:
                        logger.info("Page %d: %d parts fetched", page, total)

                    if not last_key:
                        flush()
                        break

                    time.sleep(0.1)  # minimal rate limiting

                except Exception as exc:
                    logger.error("Download error at page %d: %s", page, exc)
                    raise

        return total, last_key

    def download_library(
        self,
        library_type: Optional[str] = None,
        on_batch: Optional[Callable[[list], None]] = None,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> list:
        """
        Download the full Basic/Extended library list (much smaller than full catalog).

        Args:
            library_type: 'base', 'extend', or None for both.
            on_batch: Streaming callback; if None, returns accumulated list.
            on_progress: Optional progress callback.

        Returns:
            All parts (empty list when on_batch is used).
        """
        all_parts: list = []
        last_key = None
        total = 0
        page = 0

        while True:
            page += 1
            data = self.get_library_list(library_type=library_type, last_key=last_key)
            parts = data.get("componentInfos", [])

            if not parts:
                break

            if on_batch:
                on_batch(parts)
            else:
                all_parts.extend(parts)

            total += len(parts)
            last_key = data.get("lastKey")

            if on_progress:
                on_progress(total, f"Library page {page}: {total} parts")

            if not last_key:
                break

            time.sleep(0.1)

        return all_parts
