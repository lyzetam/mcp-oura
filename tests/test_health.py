from datetime import date, datetime

import pytest

from mcp_oura import health

D = date.fromisoformat
TODAY = D("2026-10-13")


async def test_daily_summary_collects_every_category_for_the_day(oura):
    s = await health.daily_summary(oura, D("2026-10-12"), today=TODAY)

    assert s["date"] == "2026-10-12"
    assert s["sleep"]["score"] == 70
    assert s["sleep"]["contributors"] == {"deep_sleep": 60, "total_sleep": 55}
    assert s["sleep"]["total_sleep_hours"] == 5.0  # the main sleep, not the nap
    assert s["sleep"]["deep_hours"] == 0.9
    assert s["sleep"]["latency_minutes"] == 10.0
    assert s["sleep"]["bedtime_start"] == "2026-10-11T23:30:00"  # wall-clock where you slept
    assert s["sleep"]["hrv_avg"] == 32
    assert s["sleep"]["lowest_heart_rate"] == 51
    assert s["sleep"]["naps"] == 1  # the deleted period does not count
    assert s["sleep"]["nap_hours"] == 0.5
    assert s["readiness"]["score"] == 75
    assert s["readiness"]["temperature_deviation"] == 0.4
    assert s["readiness"]["contributors"] == {
        "hrv_balance": 55,
        "resting_heart_rate": 70,
        "previous_night": 58,
    }
    assert s["activity"]["score"] == 70
    assert s["activity"]["steps"] == 4000
    assert s["activity"]["high_activity_minutes"] == 10.0
    assert s["activity"]["sedentary_minutes"] == 700.0
    assert s["activity"]["walking_equivalent_km"] == 3.2
    assert s["stress"] == {
        "stress_high_minutes": 180.0,
        "recovery_high_minutes": 15.0,
        "day_summary": "stressful",
    }
    assert s["spo2"] == {"average_percent": 96.5, "breathing_disturbance_index": 3}
    assert s["resilience"]["level"] == "adequate"
    assert s["cardiovascular_age"] == 38
    # The most recent VO2 max on or before the day, never a later one.
    assert s["vo2_max"] == {"value": 44, "measured_on": "2026-10-01"}
    assert s["workouts"] == []
    assert s["missing"] == []


async def test_daily_summary_defaults_to_the_latest_day_with_data(oura):
    s = await health.daily_summary(oura, None, today=TODAY)
    assert s["date"] == "2026-10-12"


async def test_daily_summary_names_the_categories_with_no_data(oura):
    s = await health.daily_summary(oura, D("2026-10-10"), today=TODAY)
    assert s["stress"] is None
    assert s["missing"] == ["stress", "spo2", "resilience", "cardiovascular_age"]


async def test_daily_summary_for_a_day_without_data_says_so(oura):
    with pytest.raises(ValueError, match="No Oura data for 2026-09-01"):
        await health.daily_summary(oura, D("2026-09-01"), today=TODAY)


async def test_daily_metrics_one_row_per_day_with_data(oura):
    out = await health.daily_metrics(oura, D("2026-10-01"), D("2026-10-12"))
    days = out["days"]

    # 2026-10-01 has only a VO2 max reading; it still gets a row.
    assert [d["date"] for d in days] == ["2026-10-01", "2026-10-10", "2026-10-11", "2026-10-12"]
    last = days[-1]
    assert last["sleep_score"] == 70
    assert last["readiness_score"] == 75
    assert last["activity_score"] == 70
    assert last["sleep_hours"] == 5.0
    assert last["hrv_avg"] == 32
    assert last["lowest_heart_rate"] == 51
    assert last["temperature_deviation"] == 0.4
    assert last["steps"] == 4000
    assert last["stress_high_minutes"] == 180.0
    assert last["spo2_avg"] == 96.5
    assert last["resilience_level"] == "adequate"
    assert last["vo2_max"] is None  # not measured that day
    assert days[1]["stress_high_minutes"] is None


async def test_daily_metrics_carry_vo2_max_on_the_days_it_was_measured(oura):
    days = (await health.daily_metrics(oura, D("2026-10-01"), D("2026-11-01")))["days"]
    assert {d["date"]: d["vo2_max"] for d in days if d["vo2_max"] is not None} == {
        "2026-10-01": 44,
        "2026-11-01": 45,
    }


async def test_a_single_day_range_includes_sleep_and_activity(oura):
    days = (await health.daily_metrics(oura, D("2026-10-12"), D("2026-10-12")))["days"]
    assert days[0]["sleep_hours"] == 5.0
    assert days[0]["steps"] == 4000


async def test_trends_average_by_week(oura):
    rows = (await health.trends(oura, D("2026-10-10"), D("2026-10-12"), "week"))["rows"]

    assert [r["period_start"] for r in rows] == ["2026-10-05", "2026-10-12"]
    assert rows[0]["days"] == 2
    assert rows[0]["sleep_score"] == 82.5
    assert rows[0]["readiness_score"] == 80.0
    assert rows[0]["steps"] == 11000.0
    assert rows[0]["stress_high_minutes"] == 30.0  # only one of the two days has stress data
    assert rows[1]["days"] == 1
    assert rows[1]["sleep_score"] == 70.0


async def test_trends_average_by_month(oura):
    rows = (await health.trends(oura, D("2026-10-01"), D("2026-10-31"), "month"))["rows"]
    assert len(rows) == 1
    assert rows[0]["period_start"] == "2026-10-01"
    assert rows[0]["days"] == 4
    assert rows[0]["sleep_score"] == 78.33  # the VO2-only day has no sleep score to average


async def test_trends_average_vo2_max_over_the_days_it_was_measured(oura):
    rows = (await health.trends(oura, D("2026-10-01"), D("2026-11-30"), "month"))["rows"]
    assert [(r["period_start"], r["vo2_max"]) for r in rows] == [("2026-10-01", 44.0), ("2026-11-01", 45.0)]
    # VO2 max is measured on few days, so each period says how many readings its average rests on.
    assert [(r["days"], r["vo2_max_readings"]) for r in rows] == [(4, 1), (1, 1)]


async def test_trends_without_vo2_max_readings_count_zero(oura):
    rows = (await health.trends(oura, D("2026-10-10"), D("2026-10-12"), "week"))["rows"]
    assert [(r["vo2_max"], r["vo2_max_readings"]) for r in rows] == [(None, 0), (None, 0)]


async def test_trends_rejects_an_unknown_period(oura):
    with pytest.raises(ValueError, match="period"):
        await health.trends(oura, D("2026-10-01"), D("2026-10-31"), "fortnight")


async def test_sleep_sessions_without_the_raw_time_series(oura):
    sessions = (await health.sleep_sessions(oura, D("2026-10-12"), D("2026-10-12"), include_naps=True))[
        "sessions"
    ]
    assert [s["type"] for s in sessions] == ["long_sleep", "sleep"]
    assert sessions[0]["bedtime_end"] == "2026-10-12T04:53:00"
    assert "heart_rate" not in sessions[0]

    sessions = (await health.sleep_sessions(oura, D("2026-10-12"), D("2026-10-12"), include_naps=False))[
        "sessions"
    ]
    assert [s["type"] for s in sessions] == ["long_sleep"]


async def test_heart_rate_is_asked_for_in_utc_and_bucketed_in_local_time(oura, fake):
    out = await health.heart_rate(
        oura, datetime(2026, 10, 11, 23, 0), datetime(2026, 10, 12, 0, 0), bucket_minutes=30, source=None
    )

    sent = fake.requests[0].url.params
    assert sent["start_datetime"] == "2026-10-12T03:00:00Z"
    assert sent["end_datetime"] == "2026-10-12T04:00:00Z"
    assert out["timezone"] == "America/New_York"
    assert out["readings"] == 12  # three pages of five
    assert out["buckets"] == [
        {"start": "2026-10-11T23:00:00", "avg": 52.5, "min": 50, "max": 55, "readings": 6},
        {"start": "2026-10-11T23:30:00", "avg": 58.5, "min": 56, "max": 61, "readings": 6},
    ]


async def test_heart_rate_daily_buckets_follow_local_midnight(oura):
    out = await health.heart_rate(
        oura, datetime(2026, 10, 12), datetime(2026, 10, 13), bucket_minutes=1440, source=None
    )
    # 03:xx UTC is the evening of Oct 11 in New York, so only the 16:00 UTC reading is on Oct 12.
    assert out["buckets"] == [
        {"start": "2026-10-12T00:00:00", "avg": 90.0, "min": 90, "max": 90, "readings": 1}
    ]


async def test_heart_rate_filters_by_source(oura):
    out = await health.heart_rate(
        oura, datetime(2026, 10, 11, 23, 0), datetime(2026, 10, 12, 0, 0), bucket_minutes=60, source="awake"
    )
    assert out["buckets"] == []


async def test_workouts_in_range(oura):
    out = await health.workouts(oura, D("2026-10-11"), D("2026-10-11"))
    assert out["workouts"] == [
        {
            "date": "2026-10-11",
            "activity": "walking",
            "intensity": "moderate",
            "source": "autodetected",
            "label": None,
            "start": "2026-10-11T08:02:00",
            "end": "2026-10-11T08:47:00",
            "duration_minutes": 45.0,
            "calories": 210.4,
            "distance_km": 4.1,
        }
    ]


async def test_oura_data_returns_raw_records_without_time_series_by_default(oura):
    out = await health.oura_data(oura, "sleep", D("2026-10-12"), D("2026-10-12"), include_time_series=False)
    assert out["count"] == 3
    assert "heart_rate" not in out["records"][0]
    assert out["records"][0]["readiness"] == {"score": 70}

    out = await health.oura_data(oura, "sleep", D("2026-10-12"), D("2026-10-12"), include_time_series=True)
    assert out["records"][0]["heart_rate"]["items"] == [60, 58, 55]


async def test_oura_data_reads_undated_endpoints(oura):
    info = await health.oura_data(oura, "personal_info", None, None, include_time_series=False)
    assert info["records"] == [
        {
            "id": "u1",
            "age": 38,
            "weight": 80.0,
            "height": 1.8,
            "biological_sex": "male",
            "email": "someone@example.com",
        }
    ]
    rings = await health.oura_data(oura, "ring_configuration", None, None, include_time_series=False)
    assert rings["records"][0]["hardware_type"] == "gen3"


async def test_oura_data_refuses_unknown_endpoints(oura):
    with pytest.raises(ValueError, match="daily_sleep"):
        await health.oura_data(oura, "everything", None, None, include_time_series=False)


async def test_heart_rate_buckets_start_at_the_requested_time(oura):
    out = await health.heart_rate(
        oura, datetime(2026, 10, 11, 23, 10), datetime(2026, 10, 12, 0, 0), bucket_minutes=30, source=None
    )
    assert [(b["start"], b["readings"]) for b in out["buckets"]] == [
        ("2026-10-11T23:10:00", 6),
        ("2026-10-11T23:40:00", 4),
    ]


async def test_a_second_long_sleep_counts_with_the_naps(oura, fake):
    fake.data["sleep"].append(
        {
            "id": "p12b",
            "day": "2026-10-12",
            "type": "long_sleep",
            "total_sleep_duration": 7200,
            "bedtime_start": "2026-10-12T05:30:00.000-04:00",
            "bedtime_end": "2026-10-12T07:40:00.000-04:00",
        }
    )
    s = await health.daily_summary(oura, D("2026-10-12"), today=TODAY)
    assert s["sleep"]["total_sleep_hours"] == 5.0
    assert s["sleep"]["naps"] == 2
    assert s["sleep"]["nap_hours"] == 2.5


async def test_records_dated_by_start_day_stay_inside_the_range(oura, fake):
    fake.data["enhanced_tag"] = [{"id": "t1", "start_day": "2026-10-13", "tag_type_code": "caffeine"}]
    assert (await health.oura_data(oura, "enhanced_tag", D("2026-10-12"), D("2026-10-12"), False))[
        "count"
    ] == 0
    assert (await health.oura_data(oura, "enhanced_tag", D("2026-10-13"), D("2026-10-13"), False))[
        "count"
    ] == 1
