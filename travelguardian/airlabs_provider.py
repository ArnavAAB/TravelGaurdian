from __future__ import annotations

import dataclasses
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from travelguardian.providers import SimulatedProvider

AIRLABS_BASE_URL = "https://airlabs.co/api/v9"

FLIGHT_FIELDS = ",".join(
    [
        "flight_iata",
        "airline_iata",
        "dep_iata",
        "arr_iata",
        "dep_time",
        "dep_time_utc",
        "dep_estimated",
        "dep_estimated_utc",
        "arr_time",
        "arr_time_utc",
        "arr_estimated",
        "arr_estimated_utc",
        "dep_delayed",
        "arr_delayed",
        "delayed",
        "duration",
        "status",
        "dep_terminal",
        "dep_gate",
        "arr_terminal",
        "arr_gate",
        "lat",
        "lng",
        "alt",
        "speed",
    ]
)


class _TTLDiskCache:
    """Simple JSON-backed TTL cache shared across CLI runs and web reruns."""

    def __init__(self, path: Path, ttl_seconds: int = 300) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._mem: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._mem = raw
        except (OSError, json.JSONDecodeError):
            self._mem = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._mem, indent=2), encoding="utf-8")
        except OSError:
            pass

    def get(self, key: str) -> Any | None:
        entry = self._mem.get(key)
        if not entry:
            return None
        if time.time() > float(entry.get("expires_at", 0)):
            self._mem.pop(key, None)
            return None
        return entry.get("value")

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        self._mem[key] = {
            "expires_at": time.time() + ttl,
            "value": value,
        }
        self._save()

    def clear(self) -> None:
        self._mem.clear()
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _patch_record(record: Any, updates: dict[str, Any]) -> Any:
    """Update a Pydantic v2 model, dataclass, or dict without breaking its type."""
    if isinstance(record, dict):
        merged = dict(record)
        merged.update(updates)
        return merged
    if hasattr(record, "model_copy"):  # Pydantic v2
        allowed = set(getattr(type(record), "model_fields", {}).keys())
        filtered = {k: v for k, v in updates.items() if k in allowed}
        return record.model_copy(update=filtered)
    if dataclasses.is_dataclass(record):
        allowed = {f.name for f in dataclasses.fields(record)}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        return dataclasses.replace(record, **filtered)
    return record


class AirLabsProvider:
    """Live AirLabs v9 FlightProvider with TTL caching and SimulatedProvider fallback.

    Implements TravelGuardian's FlightProvider protocol:
        status, search, offer, rebook
    and proxies simulator helpers (now, set_disruption, clear_disruptions) so it can
    drop directly into cli.py and app.py in place of SimulatedProvider.
    """

    def __init__(
        self,
        scenario_path: Path,
        api_key: str | None = None,
        *,
        ttl_seconds: int = 300,
        timeout_seconds: float = 6.0,
        prefer_simulated_disruptions: bool = True,
        allow_live_date_mismatch: bool = False,
        cache_path: Path | None = None,
    ) -> None:
        self.fallback = SimulatedProvider(scenario_path)
        self.base = self.fallback
        self.api_key = (api_key or os.getenv("AIRLABS_API_KEY", "")).strip()
        self.timeout_seconds = timeout_seconds
        self.prefer_simulated_disruptions = prefer_simulated_disruptions
        self.allow_live_date_mismatch = allow_live_date_mismatch
        self.cache = _TTLDiskCache(
            cache_path or (scenario_path.parent / "airlabs_cache.json"),
            ttl_seconds=ttl_seconds,
        )
        self._live_offers: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Simulator / clock compatibility helpers (used by ToolBox & app.py)
    # ------------------------------------------------------------------
    def now(self) -> datetime:
        return self.fallback.now()

    def set_disruption(self, *args: Any, **kwargs: Any) -> Any:
        return self.fallback.set_disruption(*args, **kwargs)

    def clear_disruptions(self, *args: Any, **kwargs: Any) -> Any:
        self.cache.clear()
        return self.fallback.clear_disruptions(*args, **kwargs)

    # ------------------------------------------------------------------
    # Low-level AirLabs HTTP + cache helper
    # ------------------------------------------------------------------
    def _request(self, endpoint: str, params: dict[str, Any]) -> Any | None:
        if not self.api_key:
            return None

        cache_key = f"{endpoint}?{urllib.parse.urlencode(sorted(params.items()))}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        query = dict(params)
        query["api_key"] = self.api_key
        url = f"{AIRLABS_BASE_URL}/{endpoint}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "TravelGuardian/0.1"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

        # AirLabs returns {"error": {"message": ..., "code": ...}} on quota/auth issues
        if not isinstance(payload, dict) or "error" in payload:
            return None

        data = payload.get("response")
        if data is not None:
            self.cache.set(cache_key, data)
        return data

    # ------------------------------------------------------------------
    # FlightProvider protocol: status / search / offer / rebook
    # ------------------------------------------------------------------
    def status(self, flight: Any, *args: Any, **kwargs: Any) -> Any:
        """Fetch live flight status from AirLabs; fall back to SimulatedProvider.

        Accepts either a Flight model object or a flight_number string.
        """
        base = self.fallback.status(flight, *args, **kwargs)

        # Extract flight_number and departure date
        if hasattr(flight, "flight_number"):
            flight_number = flight.flight_number
            flight_departs = getattr(flight, "departs", None)
            requested_date = (
                flight_departs.date().isoformat()
                if hasattr(flight_departs, "date")
                else self._extract_date_arg(args, kwargs)
            )
        else:
            flight_number = str(flight)
            requested_date = self._extract_date_arg(args, kwargs)

        # Keep demo simulator buttons working even when AirLabs is enabled
        if self.prefer_simulated_disruptions:
            sim_status = str(_get_field(base, "status", "on_time")).lower()
            sim_delay = int(_get_field(base, "delay_minutes", 0) or 0)
            if sim_status != "on_time" or sim_delay > 0:
                return base

        normalized_iata = flight_number.replace(" ", "").upper()
        live = self._request(
            "flight",
            {"flight_iata": normalized_iata, "_fields": FLIGHT_FIELDS},
        )
        if not isinstance(live, dict) or not live:
            return base

        # Optional date guard: sample trips in Dec 2026 won't match today's live flight
        live_dep_time = str(live.get("dep_time") or "")
        if (
            requested_date
            and live_dep_time
            and not self.allow_live_date_mismatch
            and not live_dep_time.startswith(requested_date)
        ):
            return base

        raw_status = str(live.get("status") or "scheduled").lower()
        delay_minutes = int(
            live.get("arr_delayed")
            or live.get("dep_delayed")
            or live.get("delayed")
            or 0
        )

        if raw_status == "cancelled":
            mapped_status = "cancelled"
            reason = "Cancelled (AirLabs live feed)"
        elif delay_minutes > 0:
            mapped_status = "delayed"
            reason = f"AirLabs live update (+{delay_minutes}m, status: {raw_status})"
        else:
            mapped_status = "on_time"
            reason = None

        sched_dep = _get_field(base, "scheduled_departure")
        sched_arr = _get_field(base, "scheduled_arrival")
        delay_delta = timedelta(minutes=delay_minutes)

        updates: dict[str, Any] = {
            "status": mapped_status,
            "delay_minutes": delay_minutes,
            "estimated_departure": sched_dep + delay_delta if sched_dep else None,
            "estimated_arrival": sched_arr + delay_delta if sched_arr else None,
            "reason": reason,
        }

        # Pass optional gate/terminal/telemetry fields if status model defines them
        for src_key, dst_key in (
            ("dep_gate", "departure_gate"),
            ("dep_terminal", "departure_terminal"),
            ("arr_gate", "arrival_gate"),
            ("arr_terminal", "arrival_terminal"),
            ("lat", "lat"),
            ("lng", "lng"),
            ("alt", "altitude"),
            ("speed", "speed"),
        ):
            if live.get(src_key) is not None:
                updates[dst_key] = live[src_key]

        return _patch_record(base, updates)

    def search(
        self,
        origin: str,
        destination: str,
        earliest_departure: datetime | None = None,
        latest_arrival: datetime | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> list[Any]:
        """Search alternative flights.

        AirLabs /schedules covers flights within ~10 hours; falls back to
        SimulatedProvider for future dates or fare/offer metadata.
        """
        fallback_results = self.fallback.search(
            origin, destination, earliest_departure, latest_arrival, *args, **kwargs
        )

        requested_date = None
        if earliest_departure and hasattr(earliest_departure, "date"):
            requested_date = earliest_departure.date().isoformat()
        else:
            requested_date = self._extract_date_arg(args, kwargs)

        today_iso = datetime.now(timezone.utc).date().isoformat()
        if (
            not self.api_key
            or (requested_date and not self.allow_live_date_mismatch and requested_date != today_iso)
        ):
            return fallback_results

        schedules = self._request(
            "schedules",
            {
                "dep_iata": origin.upper(),
                "arr_iata": destination.upper(),
                "limit": 20,
                "_fields": FLIGHT_FIELDS,
            },
        )
        if not isinstance(schedules, list) or not schedules:
            return fallback_results

        if not fallback_results:
            return fallback_results

        template = fallback_results[0]
        live_offers: list[Any] = []
        for idx, row in enumerate(schedules[:5], start=1):
            flight_iata = row.get("flight_iata")
            if not flight_iata or str(row.get("status", "")).lower() == "cancelled":
                continue

            offer_id = f"AIRLABS-{flight_iata}-{idx}"
            updates: dict[str, Any] = {
                "id": offer_id,
                "flight_number": flight_iata,
                "airline": row.get("airline_iata") or flight_iata[:2],
                "origin": row.get("dep_iata") or origin.upper(),
                "destination": row.get("arr_iata") or destination.upper(),
                "seats_available": 5,
                "protected": False,
                "fare_difference": 0.0,
                "currency": "USD",
                "notes": "Live schedule from AirLabs",
            }
            dep_dt = self._parse_airlabs_dt(row.get("dep_estimated") or row.get("dep_time"))
            arr_dt = self._parse_airlabs_dt(row.get("arr_estimated") or row.get("arr_time"))

            # Preserve timezone info from template if present
            if dep_dt is not None:
                if hasattr(template, "departs") and template.departs.tzinfo:
                    dep_dt = dep_dt.replace(tzinfo=template.departs.tzinfo)
                updates["departs"] = dep_dt

            if arr_dt is not None:
                if hasattr(template, "arrives") and template.arrives.tzinfo:
                    arr_dt = arr_dt.replace(tzinfo=template.arrives.tzinfo)
                updates["arrives"] = arr_dt

            built = _patch_record(template, updates)
            self._live_offers[offer_id] = built
            live_offers.append(built)

        # /schedules only covers roughly the next 10 hours. For a trip on another day those flights
        # are outside the window being searched, so keep only offers that fit it.
        in_window = [
            o for o in live_offers
            if (earliest_departure is None or o.departs >= earliest_departure)
            and (latest_arrival is None or o.arrives <= latest_arrival)
        ]
        return in_window or fallback_results

    def offer(self, alternative_id: str, *args: Any, **kwargs: Any) -> Any:
        if alternative_id in self._live_offers:
            return self._live_offers[alternative_id]
        return self.fallback.offer(alternative_id, *args, **kwargs)

    def rebook(self, *args: Any, **kwargs: Any) -> Any:
        # AirLabs is a read-only flight data API; always execute simulated rebooking
        return self.fallback.rebook(*args, **kwargs)

    # ------------------------------------------------------------------
    # Internal parsing utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_date_arg(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
        for key in ("date", "departure_date", "on_date", "day", "earliest_departure"):
            val = kwargs.get(key)
            if val is not None:
                return val.isoformat()[:10] if hasattr(val, "isoformat") else str(val)[:10]
        for arg in args:
            if hasattr(arg, "isoformat"):
                return arg.isoformat()[:10]
            if isinstance(arg, str) and len(arg) >= 10 and arg[4:5] == "-" and arg[7:8] == "-":
                return arg[:10]
        return None

    @staticmethod
    def _parse_airlabs_dt(raw: Any) -> datetime | None:
        if not raw or not isinstance(raw, str):
            return None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
        return None
