"""
JLCPCB shopping cart & assembly client — cookie-based session authentication.

Talks to cart.jlcpcb.com (shopping cart), jlcpcb.com (component search),
and jlcdfm.com (DFM/BOM analysis) for the full PCBA ordering workflow.

Requires a JLCPCB_SESSION_COOKIE env var containing the user's browser cookies.
The minimum required cookie is JLCPCB_SESSION_ID. For write operations
(add/delete/edit), the XSRF-TOKEN cookie is also needed.

Set JLCPCB_SESSION_COOKIE to the full Cookie header string from your browser,
e.g.: "JLCPCB_SESSION_ID=abc123; XSRF-TOKEN=xyz789; ..."
"""

import logging
import os
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

CART_BASE_URL = "https://cart.jlcpcb.com"
JLCPCB_API_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1"
DFM_BASE_URL = "https://jlcdfm.com/api/overseas-dfm-service"


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

    def _check_auth(self, data: dict) -> dict:
        if data.get("code") == 460:
            raise PermissionError(
                "JLCPCB session expired or invalid. Update JLCPCB_SESSION_COOKIE "
                "with fresh cookies from your browser."
            )
        return data

    def _post(self, path: str, payload: dict | None = None,
              base_url: str = CART_BASE_URL,
              params: dict | None = None) -> dict[str, Any]:
        url = f"{base_url}{path}"
        resp = self._session.post(url, json=payload or {}, params=params)
        resp.raise_for_status()
        return self._check_auth(resp.json())

    def _get(self, path: str, params: dict | None = None,
             base_url: str = CART_BASE_URL) -> dict[str, Any]:
        url = f"{base_url}{path}"
        resp = self._session.get(url, params=params)
        resp.raise_for_status()
        return self._check_auth(resp.json())

    def _post_file(self, path: str, file_path: str, params: dict | None = None,
                   base_url: str = DFM_BASE_URL) -> dict[str, Any]:
        url = f"{base_url}{path}"
        p = Path(file_path)
        headers = {k: v for k, v in self._session.headers.items()
                   if k.lower() != "content-type"}
        with open(p, "rb") as f:
            resp = self._session.post(
                url, files={"file": (p.name, f)}, params=params,
                headers=headers,
            )
        resp.raise_for_status()
        return self._check_auth(resp.json())

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

    def get_smt_component_suggest(self, keyword: str) -> dict[str, Any]:
        return self._get(
            "/shoppingCart/smtGood/getComponentSuggest",
            params={"keyword": keyword},
            base_url=JLCPCB_API_URL,
        )

    def get_hot_components(self) -> dict[str, Any]:
        return self._get(
            "/shoppingCart/smtGood/getHotComponent",
            base_url=JLCPCB_API_URL,
        )

    # --- Assembly config (no auth required) ---

    def get_smt_config_fee(self) -> dict[str, Any]:
        return self._get("/shoppingCart/getSmtConfigFee")

    def get_smt_panel_config(self) -> dict[str, Any]:
        return self._get("/shoppingCart/getSmtPanelConfig")

    def get_smt_order_config(self) -> dict[str, Any]:
        return self._get("/order/getSmtOrderConfig")

    def get_smt_service_list(self) -> dict[str, Any]:
        return self._get("/shoppingCart/smtGood/getServiceNameList")

    # --- BOM/CPL upload & DFM analysis (auth required) ---

    def upload_bom(self, file_path: str, dfm_record_key_id: str) -> dict[str, Any]:
        return self._post_file(
            "/smtDfm/uploadBomCpl",
            file_path,
            params={"fileType": "bom", "dfmRecordKeyId": dfm_record_key_id},
        )

    def upload_cpl(self, file_path: str, dfm_record_key_id: str) -> dict[str, Any]:
        return self._post_file(
            "/smtDfm/uploadBomCpl",
            file_path,
            params={"fileType": "cpl", "dfmRecordKeyId": dfm_record_key_id},
        )

    def trigger_bom_analysis(self, dfm_record_key_id: str) -> dict[str, Any]:
        return self._post(
            "/smtDfm/analyzeFile",
            params={"dfmRecordKeyId": dfm_record_key_id},
            base_url=DFM_BASE_URL,
        )

    def get_bom_analysis_status(self, dfm_record_key_id: str) -> dict[str, Any]:
        return self._post(
            "/smtDfm/getAnalyzeStatus",
            params={"dfmRecordKeyId": dfm_record_key_id},
            base_url=DFM_BASE_URL,
        )

    def get_bom_analysis_result(self, dfm_record_key_id: str) -> dict[str, Any]:
        return self._post(
            "/smtDfm/getAnalyzeResult",
            params={"dfmRecordKeyId": dfm_record_key_id},
            base_url=DFM_BASE_URL,
        )

    def get_dfm_info(self, dfm_record_key_id: str) -> dict[str, Any]:
        return self._get(
            "/smtDfm/getSmtDfmInfo",
            params={"dfmRecordKeyId": dfm_record_key_id},
            base_url=DFM_BASE_URL,
        )

    def list_matched_components(self, dfm_record_key_id: str) -> dict[str, Any]:
        return self._post(
            "/smtDfm/listSmtGoodsDetail",
            params={"dfmRecordKeyId": dfm_record_key_id},
            base_url=DFM_BASE_URL,
        )

    def replace_component(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            "/smtDfm/replaceComponent",
            payload=payload,
            base_url=DFM_BASE_URL,
        )

    def update_component_selection(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            "/smtDfm/updateSelectStatus",
            payload=payload,
            base_url=DFM_BASE_URL,
        )

    def get_unmatched_bom(self, smt_file_uuid: str) -> dict[str, Any]:
        return self._post("/shoppingCart/smtGood/getNoMatchBomList", {
            "smtFileUuid": smt_file_uuid,
        })
