"""The questions the tools answer, built from Oura API records.

Units: Oura reports durations in seconds and distances in metres; these
functions return hours (sleep), minutes (activity, stress, latency) and km.
Sleep and workout times keep the wall-clock time where you were and drop
the UTC offset. Heart-rate timestamps come back in UTC and are converted to
the configured timezone.
"""

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp_oura.client import OuraClient

TREND_PERIODS = ("week", "month", "quarter", "year")

TIME_SERIES_FIELDS = frozenset(
    {"heart_rate", "hrv", "met", "class_5_min", "movement_30_sec", "sleep_phase_5_min", "sleep_phase_30_sec"}
)

DOCUMENT_ENDPOINTS = ("personal_info",)
UNDATED_ENDPOINTS = ("ring_configuration",)
DATED_ENDPOINTS = (
    "daily_sleep",
    "sleep",
    "sleep_time",
    "daily_readiness",
    "daily_activity",
    "daily_stress",
    "daily_resilience",
    "daily_spo2",
    "daily_cardiovascular_age",
    "vO2_max",
    "workout",
    "session",
    "tag",
    "enhanced_tag",
    "rest_mode_period",
)
MAX_RAW_RECORDS = 500


def _round(value: Any, digits: int = 2) -> Any:
    return round(value, digits) if isinstance(value, float) else value


def _hours(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / 3600, 2)


def _minutes(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / 60, 1)


def _wall_clock(timestamp: str | None) -> str | None:
    """'2026-10-11T23:30:00.000-04:00' -> '2026-10-11T23:30:00'."""
    if timestamp is None:
        return None
    return datetime.fromisoformat(timestamp).replace(tzinfo=None).isoformat(timespec="seconds")


def _present(contributors: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (contributors or {}).items() if v is not None}


def _by_day(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["day"]: r for r in records}


def _main_sleeps(periods: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The longest long_sleep of each day."""
    main: dict[str, dict[str, Any]] = {}
    for p in periods:
        if p.get("type") != "long_sleep":
            continue
        best = main.get(p["day"])
        if best is None or (p.get("total_sleep_duration") or 0) > (best.get("total_sleep_duration") or 0):
            main[p["day"]] = p
    return main


def _session(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": p["day"],
        "type": p.get("type"),
        "bedtime_start": _wall_clock(p.get("bedtime_start")),
        "bedtime_end": _wall_clock(p.get("bedtime_end")),
        "total_sleep_hours": _hours(p.get("total_sleep_duration")),
        "time_in_bed_hours": _hours(p.get("time_in_bed")),
        "deep_hours": _hours(p.get("deep_sleep_duration")),
        "rem_hours": _hours(p.get("rem_sleep_duration")),
        "light_hours": _hours(p.get("light_sleep_duration")),
        "awake_hours": _hours(p.get("awake_time")),
        "efficiency_percent": p.get("efficiency"),
        "latency_minutes": _minutes(p.get("latency")),
        "restless_periods": p.get("restless_periods"),
        "heart_rate_avg": _round(p.get("average_heart_rate")),
        "lowest_heart_rate": p.get("lowest_heart_rate"),
        "hrv_avg": p.get("average_hrv"),
        "respiratory_rate": _round(p.get("average_breath")),
    }


async def latest_day(oura: OuraClient, today: date) -> date:
    start = today - timedelta(days=13)
    found = await asyncio.gather(
        *(oura.days(e, start, today) for e in ("daily_sleep", "daily_readiness", "daily_activity"))
    )
    days = [r["day"] for records in found for r in records]
    if not days:
        raise ValueError(
            "No Oura data in the last 14 days. The ring may not have synced through the Oura app."
        )
    return date.fromisoformat(max(days))


async def daily_summary(oura: OuraClient, day: date | None, *, today: date) -> dict[str, Any]:
    if day is None:
        day = await latest_day(oura, today)

    endpoints = (
        "daily_sleep",
        "sleep",
        "daily_readiness",
        "daily_activity",
        "daily_stress",
        "daily_spo2",
        "daily_resilience",
        "daily_cardiovascular_age",
        "workout",
    )
    *found, vo2 = await asyncio.gather(
        *(oura.days(e, day, day) for e in endpoints),
        oura.days("vO2_max", day - timedelta(days=365), day),
    )
    r = dict(zip(endpoints, found, strict=True))
    one = {e: (r[e][0] if r[e] else None) for e in endpoints if e not in ("sleep", "workout")}
    main = _main_sleeps(r["sleep"]).get(day.isoformat())

    if not (one["daily_sleep"] or main or one["daily_readiness"] or one["daily_activity"]):
        raise ValueError(
            f"No Oura data for {day}. If that is today or yesterday, the ring may not have synced "
            "through the Oura app yet."
        )

    sleep = None
    if one["daily_sleep"] or main:
        naps = [p for p in r["sleep"] if p is not main and p.get("type") != "deleted"]
        session = _session(main) if main else {}
        session.pop("date", None)
        session.pop("type", None)
        sleep = {
            "score": (one["daily_sleep"] or {}).get("score"),
            "contributors": _present((one["daily_sleep"] or {}).get("contributors")),
            **session,
            "naps": len(naps),
            "nap_hours": _hours(sum(p.get("total_sleep_duration") or 0 for p in naps)),
        }

    readiness = None
    if rd := one["daily_readiness"]:
        readiness = {
            "score": rd.get("score"),
            "temperature_deviation": _round(rd.get("temperature_deviation")),
            "temperature_trend_deviation": _round(rd.get("temperature_trend_deviation")),
            "contributors": _present(rd.get("contributors")),
        }

    activity = None
    if a := one["daily_activity"]:
        walking = a.get("equivalent_walking_distance")
        activity = {
            "score": a.get("score"),
            "steps": a.get("steps"),
            "active_calories": a.get("active_calories"),
            "total_calories": a.get("total_calories"),
            "target_calories": a.get("target_calories"),
            "high_activity_minutes": _minutes(a.get("high_activity_time")),
            "medium_activity_minutes": _minutes(a.get("medium_activity_time")),
            "low_activity_minutes": _minutes(a.get("low_activity_time")),
            "sedentary_minutes": _minutes(a.get("sedentary_time")),
            "resting_minutes": _minutes(a.get("resting_time")),
            "non_wear_minutes": _minutes(a.get("non_wear_time")),
            "walking_equivalent_km": None if walking is None else round(walking / 1000, 2),
            "inactivity_alerts": a.get("inactivity_alerts"),
            "contributors": _present(a.get("contributors")),
        }

    stress = None
    if st := one["daily_stress"]:
        stress = {
            "stress_high_minutes": _minutes(st.get("stress_high")),
            "recovery_high_minutes": _minutes(st.get("recovery_high")),
            "day_summary": st.get("day_summary"),
        }

    spo2 = None
    if o := one["daily_spo2"]:
        spo2 = {
            "average_percent": _round((o.get("spo2_percentage") or {}).get("average")),
            "breathing_disturbance_index": o.get("breathing_disturbance_index"),
        }

    resilience = None
    if res := one["daily_resilience"]:
        resilience = {"level": res.get("level"), "contributors": _present(res.get("contributors"))}

    latest_vo2 = max(vo2, key=lambda v: v["day"], default=None)
    summary = {
        "date": day.isoformat(),
        "sleep": sleep,
        "readiness": readiness,
        "activity": activity,
        "stress": stress,
        "spo2": spo2,
        "resilience": resilience,
        "cardiovascular_age": (one["daily_cardiovascular_age"] or {}).get("vascular_age"),
        "vo2_max": {"value": latest_vo2["vo2_max"], "measured_on": latest_vo2["day"]} if latest_vo2 else None,
        "workouts": [_workout(w) for w in sorted(r["workout"], key=lambda w: w.get("start_datetime") or "")],
    }
    summary["missing"] = [
        k
        for k in ("sleep", "readiness", "activity", "stress", "spo2", "resilience", "cardiovascular_age")
        if summary[k] is None
    ]
    return summary


TREND_METRICS = (
    "sleep_score",
    "readiness_score",
    "activity_score",
    "sleep_hours",
    "deep_hours",
    "rem_hours",
    "sleep_efficiency",
    "hrv_avg",
    "lowest_heart_rate",
    "temperature_deviation",
    "steps",
    "active_calories",
    "stress_high_minutes",
    "recovery_high_minutes",
    "spo2_avg",
    "vo2_max",
)


async def daily_metrics(oura: OuraClient, start: date, end: date) -> dict[str, Any]:
    endpoints = (
        "daily_sleep",
        "sleep",
        "daily_readiness",
        "daily_activity",
        "daily_stress",
        "daily_spo2",
        "daily_resilience",
        "vO2_max",
    )
    found = dict(
        zip(endpoints, await asyncio.gather(*(oura.days(e, start, end) for e in endpoints)), strict=True)
    )
    sleep_scores, readiness, activity = (
        _by_day(found[e]) for e in ("daily_sleep", "daily_readiness", "daily_activity")
    )
    stress, spo2, resilience, vo2 = (
        _by_day(found[e]) for e in ("daily_stress", "daily_spo2", "daily_resilience", "vO2_max")
    )
    main = _main_sleeps(found["sleep"])

    days = []
    for day in sorted(set(sleep_scores) | set(readiness) | set(activity) | set(main) | set(vo2)):
        ms, rd, a = main.get(day, {}), readiness.get(day, {}), activity.get(day, {})
        st, o = stress.get(day, {}), spo2.get(day, {})
        days.append(
            {
                "date": day,
                "sleep_score": sleep_scores.get(day, {}).get("score"),
                "readiness_score": rd.get("score"),
                "activity_score": a.get("score"),
                "sleep_hours": _hours(ms.get("total_sleep_duration")),
                "deep_hours": _hours(ms.get("deep_sleep_duration")),
                "rem_hours": _hours(ms.get("rem_sleep_duration")),
                "sleep_efficiency": ms.get("efficiency"),
                "hrv_avg": ms.get("average_hrv"),
                "lowest_heart_rate": ms.get("lowest_heart_rate"),
                "temperature_deviation": _round(rd.get("temperature_deviation")),
                "steps": a.get("steps"),
                "active_calories": a.get("active_calories"),
                "stress_high_minutes": _minutes(st.get("stress_high")),
                "recovery_high_minutes": _minutes(st.get("recovery_high")),
                "spo2_avg": _round((o.get("spo2_percentage") or {}).get("average")),
                "resilience_level": resilience.get(day, {}).get("level"),
                "vo2_max": vo2.get(day, {}).get("vo2_max"),
            }
        )
    return {"days": days}


def _period_start(day: date, period: str) -> date:
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "quarter":
        return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    return date(day.year, 1, 1)


async def trends(oura: OuraClient, start: date, end: date, period: str) -> dict[str, Any]:
    if period not in TREND_PERIODS:
        raise ValueError(f"period must be one of {', '.join(TREND_PERIODS)}; got {period!r}")
    groups: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for day in (await daily_metrics(oura, start, end))["days"]:
        groups[_period_start(date.fromisoformat(day["date"]), period)].append(day)

    rows = []
    for period_start in sorted(groups):
        members = groups[period_start]
        row: dict[str, Any] = {"period_start": period_start.isoformat(), "days": len(members)}
        for metric in TREND_METRICS:
            values = [m[metric] for m in members if m[metric] is not None]
            row[metric] = round(sum(values) / len(values), 2) if values else None
        row["vo2_max_readings"] = sum(m["vo2_max"] is not None for m in members)
        rows.append(row)
    return {"period": period, "rows": rows}


async def sleep_sessions(oura: OuraClient, start: date, end: date, include_naps: bool) -> dict[str, Any]:
    periods = [p for p in await oura.days("sleep", start, end) if p.get("type") != "deleted"]
    if not include_naps:
        periods = [p for p in periods if p.get("type") == "long_sleep"]
    periods.sort(key=lambda p: p.get("bedtime_start") or "")
    return {"sessions": [_session(p) for p in periods]}


async def heart_rate(
    oura: OuraClient, start: datetime, end: datetime, bucket_minutes: int, source: str | None
) -> dict[str, Any]:
    """`start` and `end` are naive local times in the client's timezone; `end` is exclusive."""
    tz = ZoneInfo(oura.timezone)

    def utc(local: datetime) -> str:
        return local.replace(tzinfo=tz).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    readings = await oura.collection("heartrate", {"start_datetime": utc(start), "end_datetime": utc(end)})
    bucket = timedelta(minutes=bucket_minutes)
    grouped: dict[datetime, list[int]] = defaultdict(list)
    for reading in readings:
        if source and reading.get("source") != source:
            continue
        local = datetime.fromisoformat(reading["timestamp"]).astimezone(tz).replace(tzinfo=None)
        if start <= local < end:
            grouped[start + ((local - start) // bucket) * bucket].append(reading["bpm"])

    buckets = [
        {
            "start": b.isoformat(),
            "avg": round(sum(v) / len(v), 1),
            "min": min(v),
            "max": max(v),
            "readings": len(v),
        }
        for b, v in sorted(grouped.items())
    ]
    return {
        "timezone": oura.timezone,
        "bucket_minutes": bucket_minutes,
        "readings": sum(b["readings"] for b in buckets),
        "buckets": buckets,
    }


def _workout(w: dict[str, Any]) -> dict[str, Any]:
    start, end = w.get("start_datetime"), w.get("end_datetime")
    duration = None
    if start and end:
        duration = round(
            (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60, 1
        )
    distance = w.get("distance")
    return {
        "date": w["day"],
        "activity": w.get("activity"),
        "intensity": w.get("intensity"),
        "source": w.get("source"),
        "label": w.get("label"),
        "start": _wall_clock(start),
        "end": _wall_clock(end),
        "duration_minutes": duration,
        "calories": _round(w.get("calories")),
        "distance_km": None if distance is None else round(distance / 1000, 2),
    }


async def workouts(oura: OuraClient, start: date, end: date) -> dict[str, Any]:
    records = sorted(await oura.days("workout", start, end), key=lambda w: w.get("start_datetime") or "")
    return {"workouts": [_workout(w) for w in records]}


async def oura_data(
    oura: OuraClient, endpoint: str, start: date | None, end: date | None, include_time_series: bool
) -> dict[str, Any]:
    if endpoint in DOCUMENT_ENDPOINTS:
        records = [await oura.document(endpoint)]
    elif endpoint in UNDATED_ENDPOINTS:
        records = await oura.collection(endpoint, {})
    elif endpoint in DATED_ENDPOINTS:
        if start is None or end is None:
            raise ValueError(f"{endpoint} needs start_date and end_date.")
        records = await oura.days(endpoint, start, end)
    else:
        known = ", ".join((*DOCUMENT_ENDPOINTS, *UNDATED_ENDPOINTS, *DATED_ENDPOINTS))
        raise ValueError(f"Unknown endpoint {endpoint!r}. Choose one of: {known}.")

    if not include_time_series:
        records = [{k: v for k, v in r.items() if k not in TIME_SERIES_FIELDS} for r in records]
    return {
        "endpoint": endpoint,
        "count": len(records),
        "truncated": len(records) > MAX_RAW_RECORDS,
        "records": records[:MAX_RAW_RECORDS],
    }
