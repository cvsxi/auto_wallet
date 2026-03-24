from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MONOBANK_BASE_URL = "https://api.monobank.ua"
STATEMENT_WINDOW_SECONDS = 2_682_000
REQUEST_INTERVAL_SECONDS = 61


class MonobankAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class MonobankClient:
    token: str
    _last_request_monotonic: float = 0.0
    _lock: threading.Lock | None = None

    def __post_init__(self) -> None:
        self._last_request_monotonic = 0.0
        self._lock = threading.Lock()

    def get_client_info(self) -> dict[str, Any]:
        return self._request_json("/personal/client-info")

    def get_statements(
        self,
        account_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        if end_at < start_at:
            raise ValueError("Кінцева дата не може бути раніше за початкову.")

        items: list[dict[str, Any]] = []
        cursor = start_at

        while cursor <= end_at:
            chunk_end = min(
                end_at,
                cursor + timedelta(seconds=STATEMENT_WINDOW_SECONDS - 1),
            )
            from_ts = int(cursor.timestamp())
            to_ts = int(chunk_end.timestamp())
            response = self._request_json(
                f"/personal/statement/{account_id}/{from_ts}/{to_ts}"
            )
            if not isinstance(response, list):
                raise MonobankAPIError("Monobank повернув неочікуваний формат виписки.")
            items.extend(response)
            cursor = chunk_end + timedelta(seconds=1)

        return items

    def _request_json(
        self,
        path: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if self._lock is None:
            self._lock = threading.Lock()

        with self._lock:
            self._respect_rate_limit()

            body: bytes | None = None
            headers = {
                "X-Token": self.token,
                "Accept": "application/json",
            }
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"

            request = Request(
                url=f"{MONOBANK_BASE_URL}{path}",
                data=body,
                headers=headers,
                method=method,
            )

            try:
                with urlopen(request, timeout=60) as response:
                    raw_body = response.read().decode("utf-8")
                    self._last_request_monotonic = time.monotonic()
                    return json.loads(raw_body) if raw_body else None
            except HTTPError as exc:
                message = exc.read().decode("utf-8", errors="replace")
                raise MonobankAPIError(
                    f"Monobank API error {exc.code}: {message}"
                ) from exc
            except URLError as exc:
                raise MonobankAPIError(f"Monobank API недоступний: {exc}") from exc

    def _respect_rate_limit(self) -> None:
        if self._last_request_monotonic == 0.0:
            return

        elapsed = time.monotonic() - self._last_request_monotonic
        if elapsed < REQUEST_INTERVAL_SECONDS:
            time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
