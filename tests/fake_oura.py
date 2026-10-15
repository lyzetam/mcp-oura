"""An in-memory stand-in for the Oura API v2, served through httpx.MockTransport.

It reproduces the behaviours of the real API that the client has to cope
with, observed against the live API on 2026-10-13 and 2026-10-15:
- daily_sleep, daily_readiness, daily_stress, daily_spo2, daily_resilience,
  daily_cardiovascular_age and sleep_time include `end_date`; sleep,
  daily_activity, workout and vO2_max exclude it. session and tag are
  treated as excluding it too, unobserved: the account had no such records.
- heartrate takes start_datetime/end_datetime, refuses ranges over 30 days
  with a 400, and timestamps are UTC with a `Z` suffix.
- Results come in pages; `next_token` asks for the next one.
"""

import asyncio
import base64
import hashlib
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl

import httpx

TOKEN = "test-token"
CLIENT_ID, CLIENT_SECRET = "client-id", "client-secret"

END_EXCLUSIVE = {"sleep", "daily_activity", "workout", "vO2_max", "session", "tag"}


def _nights():
    def period(pid, day, kind, start, end, total, deep, rem, light, awake, hrv, lowest):
        return {
            "id": pid,
            "day": day,
            "type": kind,
            "period": 0,
            "bedtime_start": start,
            "bedtime_end": end,
            "total_sleep_duration": total,
            "time_in_bed": total + awake,
            "deep_sleep_duration": deep,
            "rem_sleep_duration": rem,
            "light_sleep_duration": light,
            "awake_time": awake,
            "efficiency": 92,
            "latency": 600,
            "restless_periods": 210,
            "average_heart_rate": 55.25,
            "lowest_heart_rate": lowest,
            "average_hrv": hrv,
            "average_breath": 14.5,
            "heart_rate": {"interval": 300.0, "items": [60, 58, 55], "timestamp": start},
            "hrv": {"interval": 300.0, "items": [30, 32, 35], "timestamp": start},
            "movement_30_sec": "1112211",
            "sleep_phase_5_min": "4422314",
            "sleep_phase_30_sec": "44223",
            "readiness": {"score": 70},
        }

    return [
        period(
            "p10",
            "2026-10-10",
            "long_sleep",
            "2026-10-09T23:00:00.000-04:00",
            "2026-10-10T07:00:00.000-04:00",
            27000,
            5400,
            6300,
            15300,
            1800,
            40,
            48,
        ),
        period(
            "p11",
            "2026-10-11",
            "long_sleep",
            "2026-10-10T22:45:00.000-04:00",
            "2026-10-11T08:45:00.000-04:00",
            30600,
            6480,
            7920,
            16200,
            5400,
            44,
            47,
        ),
        period(
            "p12",
            "2026-10-12",
            "long_sleep",
            "2026-10-11T23:30:00.000-04:00",
            "2026-10-12T04:53:00.000-04:00",
            18000,
            3240,
            3960,
            10800,
            1380,
            32,
            51,
        ),
        period(
            "p12nap",
            "2026-10-12",
            "sleep",
            "2026-10-12T14:00:00.000-04:00",
            "2026-10-12T14:40:00.000-04:00",
            1800,
            0,
            360,
            1440,
            600,
            30,
            57,
        ),
        period(
            "p12del",
            "2026-10-12",
            "deleted",
            "2026-10-12T16:00:00.000-04:00",
            "2026-10-12T16:10:00.000-04:00",
            600,
            0,
            0,
            600,
            0,
            30,
            60,
        ),
    ]


def _heart_rate():
    readings = [
        {"timestamp": f"2026-10-12T03:{5 * n:02d}:00.000Z", "bpm": 50 + n, "source": "rest"}
        for n in range(12)
    ]
    readings.append({"timestamp": "2026-10-12T16:00:00.000Z", "bpm": 90, "source": "awake"})
    return readings


def seed():
    return {
        "personal_info": {
            "id": "u1",
            "age": 38,
            "weight": 80.0,
            "height": 1.8,
            "biological_sex": "male",
            "email": "someone@example.com",
        },
        "daily_sleep": [
            {
                "id": "s10",
                "day": "2026-10-10",
                "score": 80,
                "contributors": {"deep_sleep": 90, "total_sleep": 85},
            },
            {
                "id": "s11",
                "day": "2026-10-11",
                "score": 85,
                "contributors": {"deep_sleep": 95, "total_sleep": 90},
            },
            {
                "id": "s12",
                "day": "2026-10-12",
                "score": 70,
                "contributors": {"deep_sleep": 60, "total_sleep": 55, "timing": None},
            },
        ],
        "sleep": _nights(),
        "daily_readiness": [
            {
                "id": "r10",
                "day": "2026-10-10",
                "score": 78,
                "temperature_deviation": 0.1,
                "temperature_trend_deviation": 0.0,
                "contributors": {"hrv_balance": 75},
            },
            {
                "id": "r11",
                "day": "2026-10-11",
                "score": 82,
                "temperature_deviation": -0.2,
                "temperature_trend_deviation": 0.1,
                "contributors": {"hrv_balance": 78},
            },
            {
                "id": "r12",
                "day": "2026-10-12",
                "score": 75,
                "temperature_deviation": 0.4,
                "temperature_trend_deviation": 0.2,
                "contributors": {"hrv_balance": 55, "resting_heart_rate": 70, "previous_night": 58},
            },
        ],
        "daily_activity": [
            {
                "id": f"a{d[-2:]}",
                "day": d,
                "score": score,
                "steps": steps,
                "active_calories": active,
                "total_calories": 2300 + active,
                "target_calories": 500,
                "high_activity_time": 600,
                "medium_activity_time": 1200,
                "low_activity_time": 1800,
                "sedentary_time": 42000,
                "resting_time": 30000,
                "non_wear_time": 0,
                "equivalent_walking_distance": steps * 0.8,
                "inactivity_alerts": 1,
                "contributors": {"stay_active": 80},
                "met": {"interval": 60.0, "items": [1.0, 1.2]},
                "class_5_min": "0011",
            }
            for d, score, steps, active in [
                ("2026-10-10", 88, 10000, 500),
                ("2026-10-11", 90, 12000, 600),
                ("2026-10-12", 70, 4000, 200),
            ]
        ],
        "daily_stress": [
            {
                "id": "st11",
                "day": "2026-10-11",
                "stress_high": 1800,
                "recovery_high": 7200,
                "day_summary": "restored",
            },
            {
                "id": "st12",
                "day": "2026-10-12",
                "stress_high": 10800,
                "recovery_high": 900,
                "day_summary": "stressful",
            },
        ],
        "daily_spo2": [
            {
                "id": "o12",
                "day": "2026-10-12",
                "spo2_percentage": {"average": 96.5},
                "breathing_disturbance_index": 3,
            },
        ],
        "daily_resilience": [
            {
                "id": "re12",
                "day": "2026-10-12",
                "level": "adequate",
                "contributors": {"sleep_recovery": 50.1, "daytime_recovery": 40.0, "stress": 60.2},
            },
        ],
        "daily_cardiovascular_age": [
            {"id": "c12", "day": "2026-10-12", "vascular_age": 38, "pulse_wave_velocity": 7.1},
        ],
        "vO2_max": [
            {"id": "v1", "day": "2026-10-01", "vo2_max": 44},
            {"id": "v2", "day": "2026-11-01", "vo2_max": 45},
        ],
        "workout": [
            {
                "id": "w1",
                "day": "2026-10-11",
                "activity": "walking",
                "intensity": "moderate",
                "source": "autodetected",
                "label": None,
                "start_datetime": "2026-10-11T08:02:00.000-04:00",
                "end_datetime": "2026-10-11T08:47:00.000-04:00",
                "calories": 210.4,
                "distance": 4100.0,
            },
        ],
        "heartrate": _heart_rate(),
        "ring_configuration": [
            {"id": "ring1", "color": "black", "design": "heritage", "hardware_type": "gen3"}
        ],
    }


class FakeOura:
    def __init__(self, page_size: int = 1000):
        self.data = seed()
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.fail_next: list[int] = []
        self.access_tokens = {TOKEN}
        self.refresh_tokens: set[str] = set()
        self.token_requests = 0
        self.issued = 0
        self.app_revoked = False  # the user removed the app's access in Oura: every token is refused
        self.fail_token_requests: list[int] = []  # statuses the token endpoint answers with first
        self.codes: dict[str, str] = {}  # authorization code -> the PKCE challenge it was issued for

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def slow_token_transport(self) -> httpx.MockTransport:
        """Like `transport()`, but the token endpoint takes a moment, as a real round trip does,
        so renewals started together overlap instead of finishing one after another."""

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/oauth/"):
                await asyncio.sleep(0.02)
            return self.handle(request)

        return httpx.MockTransport(handle)

    def issue(self) -> dict:
        """A new token pair, as the OAuth token endpoint returns it."""
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return {"token_type": "bearer", "access_token": access, "refresh_token": refresh, "expires_in": 86400}

    def oauth(self, request: httpx.Request) -> httpx.Response:
        """POST /oauth/token and /oauth/revoke. Codes and refresh tokens are single-use, as Oura documents."""
        if request.url.path == "/oauth/revoke":
            if request.method != "POST":  # observed live: GET answers 405
                return httpx.Response(405, json={"detail": "Method Not Allowed"})
            # Observed live: the `access_token` parameter takes either token and revokes only that one;
            # a token Oura no longer accepts (revoked, expired, unknown) gets 401.
            token = request.url.params.get("access_token", "")
            if token not in self.access_tokens | self.refresh_tokens:
                return httpx.Response(401, json={"status": 401, "error": "invalid_token"})
            self.access_tokens.discard(token)
            self.refresh_tokens.discard(token)
            return httpx.Response(200, json={})
        self.token_requests += 1
        if self.fail_token_requests:
            return httpx.Response(self.fail_token_requests.pop(0), json={"error": "temporarily_unavailable"})
        form = dict(parse_qsl(request.content.decode()))
        if (form.get("client_id"), form.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in self.refresh_tokens:
            self.refresh_tokens.remove(form["refresh_token"])
            return httpx.Response(200, json=self.issue())
        if form.get("grant_type") == "authorization_code":
            challenge = self.codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "").encode()
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode()
            if challenge is not None and digest == challenge:
                return httpx.Response(200, json=self.issue())
        return httpx.Response(400, json={"error": "invalid_grant"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/oauth/"):
            return self.oauth(request)
        self.requests.append(request)
        if self.app_revoked or request.headers.get("authorization") not in {
            f"Bearer {t}" for t in self.access_tokens
        }:
            return httpx.Response(401, json={"detail": "Invalid token"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"detail": "try again"})

        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint not in self.data:
            return httpx.Response(404, json={"detail": "Not Found"})
        docs = self.data[endpoint]
        if isinstance(docs, dict):
            return httpx.Response(200, json=docs)

        q = request.url.params
        if endpoint == "heartrate":
            start = datetime.fromisoformat(q["start_datetime"])
            end = datetime.fromisoformat(q["end_datetime"])
            if end - start > timedelta(days=30):
                return httpx.Response(
                    400,
                    json={
                        "detail": "Timerange between start and endtime has to be "
                        "less than or equal to 30 days"
                    },
                )
            docs = [d for d in docs if start <= datetime.fromisoformat(d["timestamp"]) <= end]
        elif "start_date" in q:
            start, end = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])

            def day(d):
                return date.fromisoformat(d.get("day") or d["start_day"])

            if endpoint in END_EXCLUSIVE:
                docs = [d for d in docs if start <= day(d) < end]
            else:
                docs = [d for d in docs if start <= day(d) <= end]

        offset = int(q.get("next_token", 0))
        page = docs[offset : offset + self.page_size]
        more = offset + self.page_size < len(docs)
        return httpx.Response(
            200, json={"data": page, "next_token": str(offset + self.page_size) if more else None}
        )
