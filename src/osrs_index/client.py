"""Client for the OSRS Wiki real-time prices API.

Endpoint behaviour documented here was verified against the live API on
2026-07-27. See docs/api-notes.md for the full findings, including the
v1/v2 parameter divergence that this module deliberately pins around.

Standard library only, on purpose: a data collector that anyone can run
with `python3 -m osrs_index` and no install step is worth more than one
that needs a virtualenv.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger(__name__)

V1_BASE = "https://prices.runescape.wiki/api/v1/osrs"
V2_BASE = "https://prices.runescape.wiki/api/v2/osrs"

#: Aggregate windows the API actually serves. `/4h`, `/1d` and `/7d` return 404;
#: `/6h` works but is absent from parts of the wiki documentation.
Timestep = Literal["5m", "1h", "6h", "24h"]
AGGREGATE_STEPS: tuple[Timestep, ...] = ("5m", "1h", "6h", "24h")

#: Accepted by v1 /timeseries as `timestep=`. v2 uses `lookback=` with a
#: different vocabulary; see TimeseriesLookback.
TimeseriesStep = Literal["5m", "1h", "6h", "24h"]
TimeseriesLookback = Literal["6h", "24h", "7d", "30d", "6m", "1y"]


class ApiError(RuntimeError):
    """Raised when the upstream API cannot be reached or returns garbage."""


class UserAgentError(ValueError):
    """Raised when the configured User-Agent would violate the API's usage policy."""


#: User-Agent substrings the wiki explicitly calls out as blocked. Note that
#: as of 2026-07-27 none of these are *technically* enforced -- requests with
#: no UA at all, and with `python-requests/2.31.0`, both returned HTTP 200.
#: This is a stated policy, not a control. Respect it anyway: the API is a
#: volunteer-run service and the cost of being blocked is the whole product.
_BANNED_UA_FRAGMENTS = (
    "python-requests",
    "python-urllib",
    "apache-httpclient",
    "restsharp",
    "java/",
    "curl/",
    "go-http-client",
    "okhttp",
)


def validate_user_agent(ua: str) -> str:
    """Reject User-Agent strings that the API asks us not to send.

    A descriptive UA has to identify the project *and* give a contact route,
    so that the wiki admins can reach the operator instead of null-routing
    them. We enforce the contact requirement locally because nobody upstream
    will.
    """
    if not ua or not ua.strip():
        raise UserAgentError("User-Agent must be set. See config.py.")
    lowered = ua.lower()
    for fragment in _BANNED_UA_FRAGMENTS:
        if fragment in lowered:
            raise UserAgentError(
                f"User-Agent contains {fragment!r}, which the API asks clients not to send. "
                "Use something like 'osrs-assets/0.1 - contact @you on Discord'."
            )
    if "@" not in ua and "http" not in lowered:
        raise UserAgentError(
            "User-Agent must include a contact route (an @handle or a URL) so the "
            "wiki can reach you before blocking you."
        )
    return ua


@dataclass(frozen=True)
class ClientConfig:
    user_agent: str
    base: str = V1_BASE
    timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 1.5
    #: Floor between requests. The API states no explicit rate limit but warns
    #: against "multiple large queries per second for a sustained period".
    #: One request every 250ms is three orders of magnitude below that.
    min_interval: float = 0.25


class PricesClient:
    """Thin, retrying, polite client over the real-time prices API."""

    def __init__(self, config: ClientConfig) -> None:
        validate_user_agent(config.user_agent)
        self.config = config
        self._last_request_at = 0.0

    # ------------------------------------------------------------------ http

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.config.min_interval:
            time.sleep(self.config.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.config.base}{path}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url = f"{url}?{query}"

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            if attempt:
                delay = self.config.backoff_base**attempt
                log.warning("retrying %s in %.1fs (attempt %d)", path, delay, attempt + 1)
                time.sleep(delay)
            self._throttle()
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx other than 429 will not fix themselves by retrying.
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise ApiError(f"GET {url} -> HTTP {exc.code}: {exc.read()[:200]!r}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc

        raise ApiError(f"GET {url} failed after {self.config.max_retries} attempts") from last_error

    # --------------------------------------------------------------- endpoints

    def mapping(self) -> list[dict[str, Any]]:
        """Static item reference: id, name, buy limit, alch values, members flag.

        Returns a bare list, not a `{"data": ...}` envelope. ~4600 items.
        Items with no `limit` key exist (mostly set-items); treat as unlimited
        at your peril -- in practice they are untradeable-in-bulk anyway.
        """
        payload = self._get("/mapping")
        if not isinstance(payload, list):
            raise ApiError("/mapping did not return a list")
        return payload

    def latest(self, item_id: int | None = None) -> dict[str, dict[str, Any]]:
        """Most recent instant-buy (`high`) and instant-sell (`low`) print.

        WARNING: these are two independent event streams with independent
        timestamps. They are NOT a bid/ask quote and NOT an order book mid.
        Median staleness of the older leg was 37 minutes at the time of
        measurement, p90 14.8 hours. `high < low` on ~15.6% of items.

        Do not value anything with this. It is here for microstructure
        research and anomaly detection only. See nav.py.
        """
        return self._get("/latest", {"id": item_id})["data"]

    def aggregate(self, step: Timestep, timestamp: int | None = None) -> dict[str, dict[str, Any]]:
        """Volume-weighted averages over a fixed window.

        Fields per item: avgHighPrice, highPriceVolume, avgLowPrice, lowPriceVolume.
        Any of the price fields may be null when that side did not trade.
        `timestamp` selects a historical bucket (must be aligned to the step).
        """
        if step not in AGGREGATE_STEPS:
            raise ValueError(f"unsupported step {step!r}; API serves {AGGREGATE_STEPS}")
        return self._get(f"/{step}", {"timestamp": timestamp})["data"]

    def timeseries(self, item_id: int, step: TimeseriesStep = "24h") -> list[dict[str, Any]]:
        """Up to 365 buckets of history for one item.

        This is the v1 signature, which takes `timestep=`. The v2 endpoint at
        the same path takes `lookback=` instead and rejects `timestep` with
        `{"error": "lookback must be a valid value"}`. That divergence is the
        single most common way to break a collector when someone "upgrades"
        the base URL, which is why the version is pinned in ClientConfig and
        never inferred.
        """
        if self.config.base != V1_BASE:
            raise ApiError(
                "timeseries() implements the v1 contract (timestep=). "
                f"Client is pinned to {self.config.base}. Use timeseries_v2() instead."
            )
        return self._get("/timeseries", {"id": item_id, "timestep": step})["data"]

    def timeseries_v2(self, item_id: int, lookback: TimeseriesLookback = "1y") -> dict[str, Any]:
        """v2 history. Returns an envelope with itemId/startTimestamp/endTimestamp/timestep.

        The wiki notes the returned timestep "is NOT guaranteed by this API and
        may change with no prior warning" -- so read it from the response rather
        than assuming it.
        """
        client = PricesClient(
            ClientConfig(
                user_agent=self.config.user_agent,
                base=V2_BASE,
                timeout=self.config.timeout,
                max_retries=self.config.max_retries,
            )
        )
        return client._get("/timeseries", {"id": item_id, "lookback": lookback})
