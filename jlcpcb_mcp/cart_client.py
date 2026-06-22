"""
JLCPCB shopping cart client — cookie-based session authentication.

Talks to cart.jlcpcb.com which proxies to JLCPCB's internal order platform.
Requires a JLCPCB_SESSION_COOKIE env var containing the user's browser cookies.

The minimum required cookie is JLCPCB_SESSION_ID. For write operations
(add/delete/edit), the XSRF-TOKEN cookie is also needed.

Set JLCPCB_SESSION_COOKIE to the full Cookie header string from your browser,
e.g.: "JLCPCB_SESSION_ID=abc123; XSRF-TOKEN=xyz789; ..."
"""

import logging
import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

CART_BASE_URL = "https://cart.jlcpcb.com"


def _require_session_cookie() -> str:
    value = os.getenv("JLCPCB_SESSION_COOKIE")
    if not value:
        raise EnvironmentError(
            "Missing JLCPCB_SESSION_COOKIE. Set it to your browser's Cookie header "
            "from cart.jlcpcb.com (must include JLCPCB_SESSION_ID)."
        )
    return value


def _extract_xsrf(cookie_str: str) -> str | None:
    for part in cookie_str.split(";"):
        part = part.strip()
        if part.startswith("XSRF-TOKEN="):
            return part.split("=", 1)[1]
    return None


class JLCPCBCartClient:
    """JLCPCB cart client using session cookies from the browser."""

    def __init__(self) -> None:
        self._cookie_str = _require_session_cookie()
        self._xsrf = _extract_xsrf(self._cookie_str)

        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4)
        self._session.mount("https://", adapter)

        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cookie": self._cookie_str,
            "User-Agent": "JLCPCB-MCP-Cart/0.1",
        })
        if self._xsrf:
            self._session.headers["X-XSRF-TOKEN"] = self._xsrf

    def _post(self, path: str, payload: dict | None = None) -> dict[str, Any]:
        url = f"{CART_BASE_URL}{path}"
        resp = self._session.post(url, json=payload or {})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 460:
            raise PermissionError(
                "JLCPCB session expired or invalid. Update JLCPCB_SESSION_COOKIE "
                "with fresh cookies from your browser."
            )
        return data

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = f"{CART_BASE_URL}{path}"
        resp = self._session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 460:
            raise PermissionError(
                "JLCPCB session expired or invalid. Update JLCPCB_SESSION_COOKIE "
                "with fresh cookies from your browser."
            )
        return data

    def show_cart(self) -> dict[str, Any]:
        return self._get("/shoppingCart/showCart")

    def cart_page(self, page_num: int = 1, page_size: int = 10,
                  business_type: str = "JLCPCB") -> dict[str, Any]:
        return self._post("/shoppingCart/page", {
            "pageNum": page_num,
            "pageSize": page_size,
            "businessProcessType": business_type,
        })

    def add_goods(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/shoppingCart/addGoods", payload)

    def edit_cart(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/shoppingCart/editShoppingCart", payload)

    def delete_items(self, cart_access_ids: list[str]) -> dict[str, Any]:
        return self._post("/shoppingCart/delShoppingCartItems", {
            "shoppingCartAccessIds": cart_access_ids,
        })

    def calculate_costs(self, cart_access_ids: list[str]) -> dict[str, Any]:
        return self._post("/shoppingCart/calculationBatchGoodsCosts", {
            "shoppingCartAccessIdList": cart_access_ids,
        })

    def cart_detail(self, cart_access_id: str) -> dict[str, Any]:
        return self._post("/shoppingCart/cartDetail", {
            "shoppingCartAccessId": cart_access_id,
        })

    def query_shipping(self, cart_access_ids: list[str]) -> dict[str, Any]:
        return self._post("/shoppingCart/queryOrderShippingVo", {
            "shoppingCartAccessIdList": cart_access_ids,
        })

    def search_smt_components(self, keyword: str, page: int = 1,
                               page_size: int = 20) -> dict[str, Any]:
        """Search SMT assembly components (no auth required)."""
        url = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
        resp = self._session.post(url, json={
            "keyword": keyword,
            "currentPage": page,
            "pageSize": page_size,
        })
        resp.raise_for_status()
        return resp.json()

    def get_smt_component_detail(self, component_code: str) -> dict[str, Any]:
        return self._get("/shoppingCart/smtGood/getComponentDetail", {
            "componentCode": component_code,
        })
