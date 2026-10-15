# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## 0.3.5 - 2026-10-15

### Added

- Documentation for public use: README, design notes, contributing guide and security policy.
- MIT license, issue and pull request templates, and package metadata.

## 0.3.4 - 2026-10-15

### Fixed

- `mcp-oura logout` revokes the refresh token as well as the access token: Oura keeps a refresh
  token working after its access token is revoked. The saved credentials are kept until Oura
  confirms both, so logout can be run again after an outage.

## 0.3.3 - 2026-10-15

### Fixed

- `mcp-oura login` asks for the `ring_configuration` and `stress` scopes, so daily stress,
  resilience and ring configuration work with OAuth credentials.
- The server reports its name as `mcp-oura`, and messages use one set of terms: saved credentials,
  refresh the access token.

## 0.3.2 - 2026-10-14

### Changed

- Dependabot waits 14 days after a release before proposing it.

## 0.3.1 - 2026-10-14

### Added

- Continuous integration: lint, strict type checking, and tests with coverage on Python 3.12,
  3.13 and 3.14, plus weekly dependency updates.

## 0.3.0 - 2026-10-14

### Added

- `mcp-oura login`: OAuth sign-in in the browser (authorization code with PKCE over a loopback
  redirect), since Oura marks personal access tokens as deprecated.
- `mcp-oura logout`: revokes the access token with Oura and deletes the saved credentials.
- Automatic token refresh before expiry or after a 401, safe when several servers share one set of
  credentials.

## 0.2.1 - 2026-10-14

### Changed

- `OURA_TIMEZONE` defaults to the computer's timezone, then UTC.
- Error messages say where to get a token instead of naming one deployment's secret store.

## 0.2.0 - 2026-10-14

### Added

- VO2 max in `get_daily_metrics` and `get_trends`, with the number of readings behind each average.

## 0.1.0 - 2026-10-13

### Added

- Seven read-only tools over the Oura API v2 (`get_daily_summary`,
  `get_daily_metrics`, `get_trends`, `get_sleep_sessions`, `get_heart_rate`, `get_workouts`,
  `get_oura_data`), with paging, retries, and consistent date ranges across collections.
