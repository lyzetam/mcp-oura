# Security policy

## Supported versions

Fixes land on `main` and in the next version. Only the newest version is supported.

## Reporting a vulnerability

Please report privately through
[GitHub's private vulnerability reporting](https://github.com/lyzetam/mcp-oura/security/advisories/new),
not in a public issue. Include what you found, how to reproduce it, and the version
(`mcp-oura --version`). You can expect an acknowledgement within a week.

## What this project protects

mcp-oura holds credentials that read someone's health data, so reports in these areas matter most:

- The saved credentials (`credentials.json`): file permissions, atomic replacement, and the
  cross-process refresh lock.
- The `mcp-oura login` flow: PKCE, the `state` check, and the loopback listeners on `127.0.0.1`
  and `::1`.
- Tokens or the client secret appearing in logs, error messages, or tool output.
- Any way for tool arguments to reach a host other than `api.ouraring.com`.

The server is read-only by design: no tool writes to Oura. A tool that could change Oura data
would itself be a security bug.
