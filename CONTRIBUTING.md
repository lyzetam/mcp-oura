# Contributing

Thanks for helping. Bug reports, fixes and new tools are all welcome.

## Set up

```bash
git clone https://github.com/lyzetam/mcp-oura && cd mcp-oura
uv sync
```

## Check your change

```bash
uv run pytest --cov                               # tests, with the 85% coverage floor CI enforces
uv run ruff check . && uv run ruff format --check .
uv run mypy                                       # strict type checking of src/
```

CI runs these on every push to `main` and every pull request: lint and type checks once, and the
tests on Python 3.12, 3.13 and 3.14, all on Linux.

The tests run against `tests/fake_oura.py`, an in-memory Oura API, so they need no account and no
network. When you rely on a new Oura behaviour, observe it against the real API first, then teach
the fake to reproduce it, so a test fails if the handling ever regresses. Never put a real token,
or anyone's real health data, in a test or fixture.

To try a change against your own data, sign in once with
`uv run mcp-oura login --client-id <client id>`, then point an MCP client at `.venv/bin/mcp-oura`,
for example `claude mcp add oura-dev -- "$PWD/.venv/bin/mcp-oura"`.

## Guidelines

- **Read-only.** Tools only read from Oura. A change that writes to an Oura account will not be
  accepted.
- **Tests first.** Add a failing test that shows the bug or the new behaviour, then make it pass.
  Test behaviour through the public functions and tools, not private helpers.
- **Errors the model can act on.** Tool errors should say, in one plain sentence, what to change.
- **Small pull requests.** One concern each, with a description of why and how you tested it.
- **Commit messages** follow [Conventional Commits](https://www.conventionalcommits.org):
  `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- **Changelog.** Add a line under "Unreleased" in [CHANGELOG.md](CHANGELOG.md) for anything a user
  would notice.

[docs/design.md](docs/design.md) explains how the pieces fit together and which Oura API quirks
the client already handles.
