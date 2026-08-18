"""Cliente mínimo para la API de Siigo Nube (facturas de venta y compras)."""
import requests

SIIGO_BASE_URL = "https://api.siigo.com"


class SiigoAuthError(Exception):
    pass


class SiigoAPIError(Exception):
    pass


class SiigoClient:
    def __init__(self, username: str, access_key: str, partner_id: str):
        self.username = username
        self.access_key = access_key
        self.partner_id = partner_id
        self.token = None

    def authenticate(self):
        resp = requests.post(
            f"{SIIGO_BASE_URL}/auth",
            json={"username": self.username, "access_key": self.access_key},
            headers={"Content-Type": "application/json", "Partner-Id": self.partner_id},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise SiigoAuthError(f"Error {resp.status_code} autenticando con Siigo: {resp.text}")
        self.token = resp.json()["access_token"]

    def _headers(self):
        if not self.token:
            self.authenticate()
        return {"Authorization": self.token, "Partner-Id": self.partner_id}

    def _get_paginated(self, path: str, params: dict, on_page=None) -> list[dict]:
        results = []
        page = 1
        page_size = 100
        retried_auth = False
        while True:
            query = {**params, "page": page, "page_size": page_size}
            resp = requests.get(f"{SIIGO_BASE_URL}{path}", headers=self._headers(), params=query, timeout=30)
            if resp.status_code == 401 and not retried_auth:
                retried_auth = True
                self.authenticate()
                continue
            if resp.status_code != 200:
                raise SiigoAPIError(f"Error {resp.status_code} consultando {path}: {resp.text}")
            data = resp.json()
            batch = data.get("results", [])
            results.extend(batch)
            total = data.get("pagination", {}).get("total_results", len(results))
            if on_page:
                on_page(len(results), total)
            if not batch or len(results) >= total:
                break
            page += 1
        return results

    def get_sales_invoices(self, date_start: str, date_end: str, on_page=None) -> list[dict]:
        return self._get_paginated("/v1/invoices", {"date_start": date_start, "date_end": date_end}, on_page)

    def get_purchases(self, on_page=None) -> list[dict]:
        # /v1/purchases no soporta filtro por fecha en la API de Siigo — solo pagina, siempre trae todo.
        # El filtro por rango de fechas se aplica del lado del cliente (ver app.py).
        return self._get_paginated("/v1/purchases", {}, on_page)

    def get_purchase_support_documents(self, on_page=None) -> list[dict]:
        try:
            return self._get_paginated("/v1/purchase-support-documents", {}, on_page)
        except SiigoAPIError:
            return []

    def get_customer(self, customer_id: str) -> dict:
        retried_auth = False
        while True:
            resp = requests.get(f"{SIIGO_BASE_URL}/v1/customers/{customer_id}", headers=self._headers(), timeout=30)
            if resp.status_code == 401 and not retried_auth:
                retried_auth = True
                self.authenticate()
                continue
            if resp.status_code != 200:
                raise SiigoAPIError(f"Error {resp.status_code} consultando cliente {customer_id}: {resp.text}")
            return resp.json()


def normalize_document(doc: dict, tipo: str) -> dict:
    tercero = doc.get("customer") or doc.get("supplier") or {}
    provider_invoice = doc.get("provider_invoice") or {}
    documento_proveedor = " ".join(
        str(v) for v in (provider_invoice.get("prefix"), provider_invoice.get("number")) if v
    )
    return {
        "tipo": tipo,
        "id": doc.get("id"),
        "numero": doc.get("name") or str(doc.get("document", {}).get("id", "")),
        "fecha": doc.get("date"),
        "total": doc.get("total"),
        "tercero_id": tercero.get("id", ""),
        "tercero": tercero.get("identification", ""),
        "documento_proveedor": documento_proveedor,
    }


def nombre_tercero(data: dict) -> str:
    nombre = data.get("commercial_name") or " ".join(data.get("name") or [])
    return nombre.strip()
