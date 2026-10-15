# Design notes

How mcp-oura is put together, which Oura API behaviours it absorbs, and why.

## Layers

```text
Claude (or any MCP client)
   │  stdio, JSON-RPC
   ▼
cli.py      `mcp-oura` runs the server; `mcp-oura login` and `logout` run login.py
   │
   ├──▶ login.py    browser sign-in and sign-out: authorization code + PKCE over a loopback redirect
   ▼
server.py   the seven MCP tools: argument parsing, range limits, readable errors
health.py   the answers: joins Oura collections into days, sessions, trends; converts units
client.py   the Oura API v2: paging, retries, end-date rules, one token refresh on 401
auth.py     the saved credentials: owner-only file, locked token refresh
   │  HTTPS
   ▼
api.ouraring.com
```

Only `server.py` touches MCP types. `health.py` sees `OuraClient`, never HTTP. `client.py` takes a
`TokenSource` (a fixed token, or the saved credentials) and knows nothing of MCP. Each is tested on
its own terms.

## Oura API behaviours the client absorbs

Each of these, apart from the 429 and 5xx handling, was observed against the live API.
`tests/fake_oura.py` reproduces the date, paging and heart-rate rules, and the tests feed the client
429s, 5xx responses and dropped connections, so a regression fails a test. Honouring `Retry-After`
is the one piece with no test.

| Behaviour | Handling |
|---|---|
| `end_date` is inclusive on `daily_sleep`, `daily_readiness`, `daily_stress`, `daily_spo2`, `daily_resilience`, `daily_cardiovascular_age` and `sleep_time`, but exclusive on `sleep`, `daily_activity`, `workout` and `vO2_max` | `OuraClient.days()` asks for one extra day and keeps records whose `day` (or `start_day`) falls in the requested range, so every collection answers for exactly `[start, end]` whichever rule it follows. The rule for `session`, `tag`, `enhanced_tag` and `rest_mode_period` has not been observed (the account used has no such records); the fake treats `session` and `tag` as exclusive, and the extra day covers either rule |
| Results are paged with `next_token` | `collection()` follows it to the end |
| `heartrate` takes `start_datetime`/`end_datetime`, refuses spans over 30 days with a 400, returns UTC timestamps, and pages at 1,000 readings | `get_heart_rate` checks the span in UTC before calling, as Oura counts it, and converts readings to local time |
| Durations are seconds, distances metres | `health.py` returns hours (sleep), minutes (activity, latency) and kilometres |
| A night's sleep belongs to the day you woke up | Tools and their descriptions say so, so "last night" maps to today's date |
| Sleep and workout times carry the local UTC offset at the time | Kept as wall-clock time where you were, offset dropped |
| VO2 max is measured on some days only | Daily rows carry it where measured; trends average it over measured days and report `vo2_max_readings` |
| 429 and 5xx responses, dropped connections | Up to 3 attempts with linear back-off, honouring `Retry-After` up to 30 s |

## Time

Oura's daily records carry a date but no timezone. The server takes one from `OURA_TIMEZONE`,
else the computer's own (via `tzlocal`), else UTC, and uses it for "today", default ranges and
heart-rate buckets. A bad name stops the server at start-up with a reason instead of failing every
call later.

## Signing in

Oura's API reference says personal access tokens were retired in December 2025, and OAuth
applications are the supported way in. The server reads the credentials saved by `mcp-oura login`,
or a legacy personal access token from `OURA_ACCESS_TOKEN` if one is set.

`login` runs the authorization-code flow with PKCE (S256) and a loopback redirect, as RFC 8252
recommends for native apps. For a `localhost` redirect it listens on both `127.0.0.1` and `::1`,
because browsers may try `::1` first and a port left free there could be taken by another program
to receive the code; if `::1` exists but cannot be bound, sign-in stops. A redirect to the callback
path is accepted only if it carries this sign-in's `state` (compared in constant time); one with
the wrong `state` gets a 400, any other request a 404, and the wait goes on. The listener logs
nothing, because the request line carries the authorization code. It asks for every scope the
tools read, which is every scope except `email`.

### How Oura's tokens behave

Observed against the live API on 2026-10-14 and 2026-10-15:

- An access token lasts 30 days.
- Each refresh returns a new refresh token. The old access token keeps working until it expires.
- A refresh token works once: sending it again gets `400 invalid_request`, and the newer tokens
  are unaffected.
- `POST /oauth/revoke?access_token=...` takes either token and revokes only that one: revoking the
  access token leaves the refresh token working, and a revoked refresh token then gets 400. A token
  Oura no longer accepts (revoked, expired or unknown) gets 401. A GET is refused with 405.

So `logout` revokes both tokens, counting a 401 as already revoked. Until Oura confirms both, it
keeps the credentials file, so it can simply be run again after an outage.

### Refreshing safely

Access tokens are refreshed five minutes before they expire, or when Oura answers 401, after which
the request is retried once. Because a refresh token works only once, it must never be sent twice:

- Within one server, tools fetch several collections in parallel. Concurrent callers share a single
  refresh task, shielded from cancellation, so a tool call abandoned mid-refresh cannot drop the new
  tokens after Oura has spent the old refresh token. (A file lock would not do this job: a
  `filelock` lock lets its own holder re-enter, and every task in one process is that holder.)
- Across servers (Claude Code and Claude Desktop each start their own), a refresh holds a file lock
  and re-reads the file first, so the second server reuses the first one's new tokens.
- `login` and `logout` take the same lock, so a refresh in flight cannot write old credentials back
  over a new sign-in, or live ones back after logout.

An outage at Oura's token endpoint (429, 5xx, no connection) says "try again" and keeps the current
token while it is still valid; only a refusal (400, 401) asks the user to sign in again. The
credentials are written to a temporary file with mode `0600`, fsynced, renamed into place and the
directory fsynced, so a crash during the write cannot lose the only valid refresh token.

## Errors

Mistakes the model can correct (a malformed date, a range over a limit) and Oura failures come
back as tool errors with a plain sentence saying what to change. Anything else is left to the MCP
SDK, which hides internals. Start-up problems (no saved credentials, a bad timezone) stop the server with a
message on stderr, which MCP clients show in their logs.

## Testing

`tests/fake_oura.py` is an in-memory Oura API served through `httpx.MockTransport`, including the
OAuth token and revoke endpoints, single-use codes and refresh tokens, PKCE verification and a
POST-only revoke. Its token endpoint can pause like a real round trip, so concurrent refreshes
overlap in tests as they do in use. Tests run the real client, the real tools and a real round trip
through the MCP SDK's in-memory client against it; the login tests run the real loopback listeners
on a free port. No test reaches the internet, needs an Oura account or opens a browser.

Each concurrency guard (the shared refresh task, its shield, the file lock, the re-read, and the
lock `login` and `logout` take) was checked by removing it: a test then fails.

## Known limitations

- CI runs the tests on Linux; macOS is tested by hand. Windows is untested: file permissions there
  do not follow the `0600` model.
- A server killed after Oura answers a refresh but before the new tokens are saved leaves a spent
  refresh token on disk; the next refresh then asks the user to sign in again.
- On the night clocks go back, the repeated hour's heart-rate readings share one bucket.
- `ring_battery_level` and webhook subscriptions are not exposed.
- Workouts carry only Oura's summary fields. Pace, splits and routes recorded by other devices are
  not in Oura's API.
