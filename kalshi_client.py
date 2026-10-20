
from __future__ import annotations

import base64
import datetime as dt
import os
import time
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiClient:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | None = None,
        env: str | None = None,
        rate_limit_per_sec: float = 8.0,
    ):
        self.key_id = key_id or os.environ.get("KALSHI_KEY_ID", "")
        key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        self.env = (env or os.environ.get("KALSHI_ENV", "demo")).lower()
        self.base = PROD_BASE if self.env == "prod" else DEMO_BASE

        if not self.key_id or not key_path:
            raise RuntimeError(
                "Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH environment variables."
            )

        with open(key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )
        if not isinstance(self.private_key, rsa.RSAPrivateKey):
            raise RuntimeError("Kalshi requires an RSA private key.")

        self.session = requests.Session()
        self._min_interval = 1.0 / rate_limit_per_sec
        self._last_call = 0.0

        if self.env == "prod":
            print("!! KALSHI_ENV=prod — this client will place REAL orders. !!")


    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _headers(self, method: str, path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.time()

    def request(
        self, method: str, endpoint: str, params: dict | None = None, json: dict | None = None
    ) -> Any:
        self._throttle()
        full_path = f"/trade-api/v2{endpoint}"
        url = f"{self.base}{endpoint}"
        resp = self.session.request(
            method.upper(),
            url,
            headers=self._headers(method, full_path),
            params=params,
            json=json,
            timeout=15,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} {method} {endpoint}: {resp.text[:400]}")
        return resp.json()


    def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str = "open",
        limit: int = 200,
        max_pages: int = 20,
    ) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": limit, "status": status}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if cursor:
                params["cursor"] = cursor
            data = self.request("GET", "/markets", params=params)
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        raw = self.request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        book = raw.get("orderbook_fp") or raw.get("orderbook") or raw

        def levels(*keys) -> list[list[float]]:
            for k in keys:
                v = book.get(k)
                if not v:
                    continue
                out = []
                for lvl in v:
                    try:
                        price, size = float(lvl[0]), float(lvl[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    out.append([round(price * 100, 4) if price <= 1.0 else price, size])
                return sorted(out, key=lambda x: -x[0])
            return []

        return {
            "yes": levels("yes_dollars", "yes"),
            "no": levels("no_dollars", "no"),
        }

    def get_events(
        self, series_ticker: str | None = None, status: str = "open", max_pages: int = 50
    ) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"status": status, "limit": 200}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            data = self.request("GET", "/events", params=params)
            out.extend(data.get("events", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_series_list(self, category: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        return self.request("GET", "/series", params=params).get("series", [])


    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        return self.request("GET", "/portfolio/positions")

    def get_fills(self, limit: int = 200) -> dict:
        return self.request("GET", "/portfolio/fills", params={"limit": limit})

    def get_orders(self, status: str = "resting") -> dict:
        return self.request("GET", "/portfolio/orders", params={"status": status})


    def place_limit_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        client_order_id: str | None = None,
        post_only: bool = True,
    ) -> dict:
        if side not in ("yes", "no"):
            raise ValueError("side must be 'yes' or 'no'")
        if not 1 <= price_cents <= 99:
            raise ValueError("price_cents must be 1-99")

        body: dict[str, Any] = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": price_cents,
            "client_order_id": client_order_id
            or f"{ticker}-{int(time.time()*1000)}",
        }
        if post_only:
            body["post_only"] = True
        return self.request("POST", "/portfolio/orders", json=body)

    def cancel_order(self, order_id: str) -> dict:
        return self.request("DELETE", f"/portfolio/orders/{order_id}")


def mid_price(market: dict) -> float | None:
    bid, ask = market.get("yes_bid"), market.get("yes_ask")
    if not bid or not ask or bid <= 0 or ask >= 100:
        return None
    return (bid + ask) / 200.0


def time_to_close(market: dict) -> float | None:
    ts = market.get("close_time")
    if not ts:
        return None
    close = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (close - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600.0


if __name__ == "__main__":
    c = KalshiClient()
    print(f"env={c.env} base={c.base}")
    print("balance:", c.get_balance())
    markets = c.get_markets(status="open", limit=10, max_pages=1)
    print(f"\n{len(markets)} open markets, first 5:")
    for m in markets[:5]:
        print(f"  {m['ticker']:<40} {m.get('yes_bid')}/{m.get('yes_ask')}  {m.get('title','')[:50]}")
