"""HTTP client for the CardTrader Full API v2.

Reference: https://www.cardtrader.com/docs/api/full/reference
Only the endpoints MTG Ledger needs are wrapped. Responses are returned as
plain dicts/lists; mapping to Ledger models lives in `ledger.services`.
"""

import time
from decimal import Decimal

import httpx

BASE_URL = "https://api.cardtrader.com/api/v2"
MTG_GAME_ID = 1

# Ledger condition codes (Cardmarket-style grades) -> CardTrader "condition" property.
CONDITION_TO_CT = {
    "M": "Mint",
    "NM": "Near Mint",
    "EX": "Slightly Played",
    "GD": "Moderately Played",
    "LP": "Played",
    "PL": "Heavily Played",
    "PO": "Poor",
}
CONDITION_FROM_CT = {v: k for k, v in CONDITION_TO_CT.items()}

# Ledger language codes -> CardTrader "mtg_language" property.
LANGUAGE_TO_CT = {
    "EN": "en", "IT": "it", "DE": "de", "FR": "fr", "ES": "es",
    "PT": "pt", "JP": "jp", "KO": "ko", "RU": "ru", "ZHS": "zh-CN",
}


class CardTraderError(Exception):
    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class CardTraderClient:
    def __init__(self, token, *, base_url=BASE_URL, transport=None, timeout=30.0, sleep=time.sleep):
        if not token:
            raise CardTraderError("CardTrader API token is not configured.")
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            # products/export can take a while on large inventories (docs suggest 120-180s).
            timeout=httpx.Timeout(timeout, read=180.0),
            transport=transport,
        )
        self._sleep = sleep

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- plumbing ---------------------------------------------------------

    def _request(self, method, path, **kwargs):
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CardTraderError(f"CardTrader unreachable: {exc}") from exc
        if resp.status_code == 429:
            raise CardTraderError("CardTrader rate limit exceeded, retry in a few seconds.", 429)
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = None
        if resp.is_error:
            message = None
            if isinstance(data, dict):
                message = (data.get("extra") or {}).get("message") or data.get("error") or data.get("error_code")
            raise CardTraderError(
                f"CardTrader {method} {path} failed ({resp.status_code}): {message or resp.reason_phrase}",
                resp.status_code,
                data,
            )
        return data

    def _get(self, path, **params):
        return self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    # --- account / catalog -------------------------------------------------

    def info(self):
        return self._get("/info")

    def expansions(self):
        return self._get("/expansions")

    def blueprints(self, expansion_id):
        return self._get("/blueprints/export", expansion_id=expansion_id)

    def marketplace_products(self, blueprint_id, *, language=None, foil=None):
        """Cheapest 25 products for a blueprint: {"<blueprint_id>": [product, ...]}."""
        params = {"blueprint_id": blueprint_id, "language": language}
        if foil is not None:
            params["foil"] = "true" if foil else "false"
        data = self._get("/marketplace/products", **params) or {}
        return data.get(str(blueprint_id), [])

    # --- inventory -----------------------------------------------------------

    def my_products(self, **params):
        return self._get("/products/export", **params)

    def bulk_create(self, products):
        return self._run_job("/products/bulk_create", products)

    def bulk_update(self, products):
        return self._run_job("/products/bulk_update", products)

    def bulk_destroy(self, product_ids):
        return self._run_job("/products/bulk_destroy", [{"id": pid} for pid in product_ids])

    def delete_product(self, product_id):
        return self._request("DELETE", f"/products/{product_id}")

    def job(self, uuid):
        return self._get(f"/jobs/{uuid}")

    def _run_job(self, path, products, *, max_wait=60):
        """Start a bulk job and wait for it. Returns the job's `results` list (by job_index)."""
        if not products:
            return []
        uuid = self._request("POST", path, json={"products": products})["job"]
        waited = 0.0
        while True:
            self._sleep(1.0)  # /jobs is limited to 1 request per second
            waited += 1.0
            job = self.job(uuid)
            state = job.get("state")
            if state == "completed":
                return sorted(job.get("results") or [], key=lambda r: r.get("job_index", 0))
            if state == "unprocessable":
                raise CardTraderError(f"CardTrader job {uuid} failed", payload=job)
            if waited >= max_wait:
                raise CardTraderError(f"CardTrader job {uuid} still {state} after {max_wait}s", payload=job)

    # --- orders ----------------------------------------------------------

    def orders(self, *, page=1, limit=100, state=None, date_from=None, sort="date.desc"):
        return self._get(
            "/orders", page=page, limit=limit, state=state, order_as="seller", sort=sort,
            **{"from": date_from.isoformat() if date_from else None},
        )

    def all_orders(self, *, date_from=None, max_pages=20):
        for page in range(1, max_pages + 1):
            batch = self.orders(page=page, date_from=date_from) or []
            yield from batch
            if len(batch) < 100:
                return

    def order(self, order_id):
        return self._get(f"/orders/{order_id}")

    def set_tracking_code(self, order_id, tracking_code):
        return self._request("PUT", f"/orders/{order_id}/tracking_code", json={"tracking_code": tracking_code})

    def ship(self, order_id):
        return self._request("PUT", f"/orders/{order_id}/ship")


def money(value):
    """CardTrader Money ({"cents": 123, "currency": "EUR"}) or cents int -> Decimal."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("cents")
        if value is None:
            return None
    return (Decimal(int(value)) / 100).quantize(Decimal("0.01"))
