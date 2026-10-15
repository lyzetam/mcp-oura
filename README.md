# mcp-oura

[![CI](https://github.com/lyzetam/mcp-oura/actions/workflows/ci.yml/badge.svg)](https://github.com/lyzetam/mcp-oura/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives Claude, or any MCP
client, read-only access to your Oura Ring data through the
[Oura API v2](https://cloud.ouraring.com/v2/docs). It runs on your computer and talks only to
Oura's API.

> [!NOTE]
> mcp-oura is an independent, unofficial project. It is not made, endorsed or supported by
> Ōura Health Oy; "Oura" is their trademark, used here only to name the service this connects to.
> The numbers are Oura's estimates, and an AI model's reading of them can be wrong. Neither is
> medical advice.

Ask things like:

- "How did I sleep last night compared with my 30-day average?"
- "Has my VO2 max improved over the past year?"
- "What was my heart rate during yesterday's workout?"
- "Which weekdays do I sleep worst, by month, since January?"

## Tools

Every tool is read-only. Dates are `YYYY-MM-DD` in your local timezone. Oura files a night's sleep
under the morning you woke up, so "last night" is today's date. Tools that take a range default to
the 7 days ending today.

| Tool | Returns | Range |
|---|---|---|
| `get_daily_summary` | Everything for one day: sleep score and contributors, the night in detail, naps, readiness, activity, stress, SpO2, resilience, cardiovascular age, latest VO2 max, workouts | One day; by default the latest day with data in the past 14 days |
| `get_daily_metrics` | One row of headline numbers per day, VO2 max included | Up to 400 days |
| `get_trends` | Weekly, monthly, quarterly or yearly averages, with how many days and VO2 max readings each rests on | Any length |
| `get_sleep_sessions` | Each sleep period with stages, efficiency, latency, heart rate, HRV and breathing | Up to 400 days |
| `get_heart_rate` | Heart rate in time buckets: average, minimum, maximum and number of readings | Up to 30 days (Oura's limit) and 1,500 buckets |
| `get_workouts` | Workouts with activity, intensity, duration, calories and distance | Any length |
| `get_oura_data` | Raw records from 17 Oura collections, for anything the others leave out: tags, sessions, rest mode, bedtime guidance, ring details, personal info | Up to 400 days and 500 records |

<details>
<summary>Parameters</summary>

| Tool | Parameters |
|---|---|
| `get_daily_summary` | `day` (optional) |
| `get_daily_metrics`, `get_workouts` | `start_date`, `end_date` |
| `get_trends` | `start_date`, `end_date`, `period`: `week` (from Monday), `month` (default), `quarter` or `year` |
| `get_sleep_sessions` | `start_date`, `end_date`, `include_naps` (default `true`) |
| `get_heart_rate` | `start` (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`, required), `end` (exclusive; default one day after `start`), `bucket_minutes` (1 to 1,440; default 60), `source` (optional: `awake`, `rest`, `sleep`, `session` or `workout`) |
| `get_oura_data` | `endpoint` (one of the 17 collections the tool describes), `start_date`, `end_date` (for dated collections), `include_time_series` (default `false`: drops the large 5-minute and 30-second arrays) |

</details>

The server evens out the API's quirks, so every tool covers exactly the dates asked for.
[docs/design.md](docs/design.md) lists them.

## Requirements

- An Oura Ring and an active Oura Membership. Without a membership, Oura's API answers 403.
- macOS or Linux, with [uv](https://docs.astral.sh/uv/), which installs Python 3.12 or newer if
  needed.

## Setup

### 1. Install

```bash
uv tool install git+https://github.com/lyzetam/mcp-oura
```

This puts `mcp-oura` in `~/.local/bin`. If your shell can't find it, run `uv tool update-shell` and
open a new terminal. To work on the code instead, see [CONTRIBUTING.md](CONTRIBUTING.md).

### 2. Create an Oura API application

Oura grants API access through OAuth applications. Its API reference says personal access tokens
were retired in December 2025. You create your own application: only you use it, and its secret
stays on your computer.

1. Sign in at [developer.ouraring.com](https://developer.ouraring.com/applications) with your Oura
   account and choose **Create New**.
2. Fill in the form. Oura asks for a display name, a description, a contact email, and website,
   privacy policy and terms of service addresses; for a personal application, a link to this
   repository serves for all three addresses.
3. Add the redirect URI `http://localhost:8765/callback`, exactly as written.
4. Tick every scope except **Email**, accept the Oura API Agreement, and create the application.
5. Copy the client ID and client secret it shows you.

### 3. Sign in

```bash
mcp-oura login --client-id <client id>
```

It asks for the client secret without echoing it, then opens Oura in your browser. Approve access,
and the saved credentials refresh themselves from then on. `mcp-oura logout` revokes both tokens
with Oura and deletes the saved credentials.

`mcp-oura login` also reads `OURA_CLIENT_ID` and `OURA_CLIENT_SECRET`, but typing the secret at the
prompt keeps it out of your shell history. The sign-in address is also printed in the terminal.

<details>
<summary>Signing in on a remote machine</summary>

Oura sends your browser back to `localhost` on the computer the browser runs on. To sign in on a
remote machine, forward the port from your own computer first:

```bash
ssh -L 8765:localhost:8765 <host>
mcp-oura login --no-browser --client-id <client id>   # on the remote machine
```

Then open the printed address in your local browser.

</details>

<details>
<summary>Personal access tokens (legacy)</summary>

A personal access token issued before Oura retired them may still work. Skip steps 2 and 3 and
pass it in the client's configuration, for example
`claude mcp add --scope user oura -e OURA_ACCESS_TOKEN=<token> -- mcp-oura`, but expect Oura to
refuse it at some point.

</details>

### 4. Connect Claude

Claude Code:

```bash
claude mcp add --scope user oura -- mcp-oura
```

Claude Desktop: quit the app first, because a running Desktop can write its own copy of the
configuration back over your edit. Add the server to `claude_desktop_config.json` (Settings,
Developer, Edit Config) with the full path that `which mcp-oura` prints, then start the app:

```json
{
  "mcpServers": {
    "oura": { "command": "/Users/you/.local/bin/mcp-oura" }
  }
}
```

Other MCP clients start `mcp-oura` the same way: a stdio server with no arguments.

### 5. Check it works

`claude mcp list` should show `oura` as connected. Then ask Claude "How did I sleep last night?".

## Configuration

Every setting is optional. Claude Desktop does not see your shell's environment, and other clients
may not either, so set variables in the client's server configuration:
`claude mcp add --scope user oura -e OURA_TIMEZONE=Europe/London -- mcp-oura` for Claude Code, or an
`"env": { ... }` object next to `"command"` for Claude Desktop.

| Variable | Default | Purpose |
|---|---|---|
| `OURA_TIMEZONE` | your computer's timezone, else UTC | IANA name, such as `Europe/London`, for "today" and heart-rate times |
| `OURA_CREDENTIALS_FILE` | `$XDG_CONFIG_HOME/mcp-oura/credentials.json`, else `~/.config/mcp-oura/credentials.json` | Where `mcp-oura login` saves credentials and the server reads them |
| `OURA_ACCESS_TOKEN` | unset | A legacy personal access token, used instead of the saved credentials |
| `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET` | unset | Read by `mcp-oura login` |

## Updating and uninstalling

```bash
uv tool upgrade mcp-oura                 # update

mcp-oura logout                          # uninstall: revoke and delete the saved credentials,
uv tool uninstall mcp-oura
claude mcp remove --scope user oura      # remove it from Claude,
rm -r ~/.config/mcp-oura                 # and remove the lock file logout leaves
```

## Privacy and security

- The server sends your data only to the MCP client that started it, and keeps no copy. The client
  passes tool results to its language model: with Claude, they go to Anthropic as part of the
  conversation, under your Claude account's privacy settings.
- It writes two files, both in `~/.config/mcp-oura/` (created with mode `0700`).
  `credentials.json` holds your client ID, client secret, access token and refresh token. It has
  mode `0600` and is replaced atomically on each refresh. An empty `credentials.json.lock`
  coordinates refreshes between servers and stays after logout.
- Tokens and the client secret never appear in logs, error messages or tool results.
- Sign-in uses PKCE and a state check, and listens for Oura's redirect only on this computer's
  loopback addresses (`127.0.0.1` and `::1`).
- If the credentials file ever leaks, run `mcp-oura logout`, which revokes both tokens with Oura.
  The file also holds your application's client secret: reset it on the application's page at
  [developer.ouraring.com](https://developer.ouraring.com/applications) (Reset Secret), then run
  `mcp-oura login` again with the new secret.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Troubleshooting

If a client shows the server as failed, run `mcp-oura` in a terminal with the same environment
variables the client sets. It either prints the problem and exits, or waits silently, which means it
started; the first tool call is what tests the sign-in. Press Ctrl-D to stop it.

| Message or symptom | Cause and fix |
|---|---|
| `No Oura data for <date>` | The ring has not synced that day yet. Open the Oura app on your phone near the ring, then ask again |
| `No Oura data in the last 14 days` | The ring has not synced in two weeks, or the account has no ring data |
| Today's scores appear but heart rate stops days earlier | Oura's heart-rate series can lag the daily scores after a long gap between syncs, and fills in later |
| `not signed in: there are no saved credentials at ...` | Run `mcp-oura login`, or point `OURA_CREDENTIALS_FILE` at your credentials in the client's configuration |
| `The Oura API refused ... (HTTP 403)` | Your Oura Membership has expired, or you unticked a permission when approving. Renew, or run `mcp-oura login` again and allow every permission |
| `Oura refused to refresh the access token` | The application's access was removed, or the credentials expired. Run `mcp-oura login` again |
| `The Oura API rejected the access token (401). Check that OURA_ACCESS_TOKEN ...` | Oura no longer accepts your legacy personal access token. Unset it and run `mcp-oura login` |
| `mcp-oura login` times out | The redirect URI on your Oura application must be exactly `http://localhost:8765/callback`, and the browser must run on the same computer (or see "Signing in on a remote machine") |
| `Couldn't listen on port 8765` | Another program uses the port. Add a second redirect URI to your Oura application and pass it with `--redirect-uri` |
| Claude Desktop forgets the server | It was edited while Desktop was running. Quit Desktop, add it again, then start Desktop |

## Development

```bash
git clone https://github.com/lyzetam/mcp-oura && cd mcp-oura
uv sync
uv run pytest --cov     # runs against an in-memory fake of the Oura API; no account needed
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

[CONTRIBUTING.md](CONTRIBUTING.md) covers the workflow, and [docs/design.md](docs/design.md)
explains how the server is put together. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
