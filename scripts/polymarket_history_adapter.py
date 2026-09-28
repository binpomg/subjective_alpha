"""Small, read-only adapter for Polymarket Data API v2 price history.

The adapter deliberately deals only with the official ``/v2/prices-history``
route.  It does not discover markets, infer historical Gamma state, or fetch
any future labels.  A caller must provide a CLOB outcome ``token_id`` and a
single time-window form (``as_of``, ``interval`` or ``start``/``end``).

The API returns an observation bucket's *start* timestamp.  For a strict
point-in-time research cut, ``latest_completed_before`` therefore requires
``timestamp + resolution_seconds <= cutoff`` for aggregate points and skips
zero-resolution points by default.  Skipping zero-resolution points is the
safe default because a terminal settlement point is also represented with
``resolution_seconds=0``.  Callers that have independently verified an exact
tick may opt in explicitly.

This module uses only Python's standard library, making it suitable for the
sealed replay environment and easy to test without a live API call.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_BASE_URL = "https://data-api.polymarket.com"
DEFAULT_PATH = "/v2/prices-history"


class PolymarketAPIError(RuntimeError):
    """An HTTP, decoding, or contract error returned by the Data API."""


@dataclasses.dataclass(frozen=True)
class PricePoint:
    timestamp: int
    price: float
    resolution_seconds: int

    @property
    def point_end(self) -> int:
        """Exclusive end of the observation window (equal to timestamp for ticks)."""

        return self.timestamp + self.resolution_seconds

    @classmethod
    def from_json(cls, row: Mapping[str, Any]) -> "PricePoint":
        missing = [k for k in ("timestamp", "price", "resolution_seconds") if k not in row]
        if missing:
            raise PolymarketAPIError(f"price point missing fields: {missing}")
        try:
            timestamp = int(row["timestamp"])
            price = float(row["price"])
            resolution = int(row["resolution_seconds"])
        except (TypeError, ValueError) as exc:
            raise PolymarketAPIError(f"invalid price point: {row!r}") from exc
        if timestamp < 0 or resolution < 0 or not 0.0 <= price <= 1.0:
            raise PolymarketAPIError(f"price point outside API contract: {row!r}")
        return cls(timestamp, price, resolution)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "price": self.price,
            "resolution_seconds": self.resolution_seconds,
            "point_end": self.point_end,
        }


@dataclasses.dataclass(frozen=True)
class HistoryPage:
    points: tuple[PricePoint, ...]
    pagination: Mapping[str, Any]
    status: int
    url: str
    trace_id: str | None = None


def _retry_after(headers: Mapping[str, str], fallback: float) -> float:
    try:
        return max(0.0, float(headers.get("Retry-After", fallback)))
    except (TypeError, ValueError):
        return fallback


class PolymarketHistoryAPI:
    """Read-only client with cursor paging and bounded 429/503 retries."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 45.0,
        max_retries: int = 3,
        opener: Any = urllib.request.urlopen,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self._opener = opener
        self._sleep = sleep

    @staticmethod
    def _window_params(
        *,
        as_of: int | None = None,
        interval: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Any]:
        forms = int(as_of is not None) + int(interval is not None) + int(start is not None)
        if forms != 1:
            raise ValueError("provide exactly one of as_of, interval, or start/end")
        if end is not None and start is None:
            raise ValueError("end requires start")
        if start is not None and start <= 0:
            raise ValueError("start=0 is rejected by the API; use a positive epoch")
        if end is not None and end <= start:  # type: ignore[operator]
            raise ValueError("end must be greater than start")
        if as_of is not None and as_of <= 0:
            raise ValueError("as_of must be a positive epoch")
        if interval is not None and interval not in {"max", "all", "1m", "1w", "1d", "6h", "1h"}:
            raise ValueError("unsupported interval; use max, all, 1m, 1w, 1d, 6h, or 1h")
        if start is not None and end is not None:
            return {"start": int(start), "end": int(end)}
        return {"as_of": int(as_of)} if as_of is not None else {"interval": interval}

    def _get(self, params: Mapping[str, Any]) -> tuple[dict[str, Any], int, str, str | None]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}{DEFAULT_PATH}?{query}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "polymarket-pit-replay/0.1", "Accept": "application/json"},
            method="GET",
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._opener(req, timeout=self.timeout) as response:
                    status = int(getattr(response, "status", 200))
                    body = response.read()
                    headers = {str(k): str(v) for k, v in response.headers.items()}
                obj = json.loads(body.decode("utf-8"))
                if status >= 400:
                    raise PolymarketAPIError(
                        f"Polymarket API HTTP {status}: {obj.get('error', obj)!r}; "
                        f"trace_id={headers.get('x-trace-id')}"
                    )
                if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
                    raise PolymarketAPIError("prices-history response has no list data field")
                return obj, status, url, headers.get("x-trace-id")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (429, 503) or attempt >= self.max_retries:
                    payload = exc.read().decode("utf-8", errors="replace")
                    raise PolymarketAPIError(
                        f"Polymarket API HTTP {exc.code}: {payload[:500]}"
                    ) from exc
                self._sleep(_retry_after(exc.headers, 2.0**attempt))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise PolymarketAPIError(f"Polymarket API network failure: {exc}") from exc
                self._sleep(2.0**attempt)
            except json.JSONDecodeError as exc:
                raise PolymarketAPIError("Polymarket API returned non-JSON content") from exc
        raise PolymarketAPIError(f"Polymarket API request failed: {last_error}")

    def prices_history(
        self,
        token_id: str,
        *,
        as_of: int | None = None,
        interval: str | None = None,
        start: int | None = None,
        end: int | None = None,
        bucket_seconds: int | None = None,
        limit: int = 10_000,
        max_pages: int = 10_000,
    ) -> tuple[PricePoint, ...]:
        """Fetch and cursor-page one token's history in oldest-first order."""

        if not token_id or not str(token_id).strip():
            raise ValueError("token_id is required")
        if limit < 0 or limit > 10_000:
            raise ValueError("limit must be in [0, 10000]")
        # The API treats as_of as a standalone point-in-time form.  In
        # particular, bucket_seconds is a window modifier and is rejected
        # together with as_of (rather than changing the returned precision).
        if as_of is not None and bucket_seconds is not None:
            raise ValueError("bucket_seconds cannot be combined with as_of")
        if bucket_seconds is not None and not 60 <= int(bucket_seconds) <= 86_400:
            raise ValueError("bucket_seconds must be in [60, 86400]")
        params: dict[str, Any] = {"token_id": str(token_id), "limit": int(limit)}
        params.update(self._window_params(as_of=as_of, interval=interval, start=start, end=end))
        if bucket_seconds is not None:
            params["bucket_seconds"] = int(bucket_seconds)

        all_points: list[PricePoint] = []
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            obj, _, _, _ = self._get(params)
            all_points.extend(PricePoint.from_json(row) for row in obj["data"])
            pagination = obj.get("pagination") or {}
            next_cursor = pagination.get("next_cursor")
            has_more = bool(pagination.get("has_more"))
            if not has_more or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise PolymarketAPIError("pagination cursor repeated")
            seen_cursors.add(str(next_cursor))
            # Keep the original filters; only the opaque cursor advances.
            params["cursor"] = str(next_cursor)
        else:
            raise PolymarketAPIError(f"pagination exceeded max_pages={max_pages}")
        # The API documents oldest-first, but sorting and de-duplicating makes
        # replay deterministic if a page is repeated during a server refresh.
        unique = {(p.timestamp, p.price, p.resolution_seconds): p for p in all_points}
        return tuple(sorted(unique.values(), key=lambda p: (p.timestamp, p.resolution_seconds)))


def latest_completed_before(
    points: Iterable[PricePoint],
    cutoff_epoch: int,
    *,
    exclude_exact_ticks: bool = True,
) -> PricePoint | None:
    """Return the newest point whose observation window ended by ``cutoff_epoch``.

    ``resolution_seconds=0`` rows are excluded by default because the API can
    append a final on-chain settlement point using that representation.  An
    exact tick can be admitted only when the caller has separately verified it.
    """

    if cutoff_epoch <= 0:
        raise ValueError("cutoff_epoch must be positive")
    eligible = [
        p
        for p in points
        if p.timestamp <= cutoff_epoch
        and (not exclude_exact_ticks or p.resolution_seconds > 0)
        and p.point_end <= cutoff_epoch
    ]
    return max(eligible, key=lambda p: (p.timestamp, p.resolution_seconds), default=None)


def write_fetch_manifest(path: str | Path, *, token_id: str, params: Mapping[str, Any],
                         response_points: Sequence[PricePoint], retrieved_at_utc: str,
                         source_sha256: str | None = None) -> None:
    """Write a compact, auditable manifest without exposing future labels."""

    payload = {
        "endpoint": f"{DEFAULT_BASE_URL}{DEFAULT_PATH}",
        "token_id": str(token_id),
        "params": dict(params),
        "retrieved_at_utc": retrieved_at_utc,
        "count": len(response_points),
        "first_timestamp": response_points[0].timestamp if response_points else None,
        "last_timestamp": response_points[-1].timestamp if response_points else None,
        "resolution_seconds": sorted({p.resolution_seconds for p in response_points}),
        "source_sha256": source_sha256,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
