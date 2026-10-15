"""The `mcp-oura` command. With no subcommand it runs the MCP server on stdio, which is what Claude
starts; `login` and `logout` manage the Oura sign-in the server uses."""

import argparse
import getpass
import os
import sys

from mcp_oura import __version__, auth, login, server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-oura",
        description="Read-only Oura Ring data for Claude over MCP. "
        "With no command, runs the server on stdio.",
    )
    parser.add_argument("--version", action="version", version=f"mcp-oura {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="[login | logout]")
    sign_in = commands.add_parser(
        "login",
        help="sign in with Oura in your browser",
        description="Sign in with Oura in your browser and save the credentials for the server. The client "
        "secret is read from $OURA_CLIENT_SECRET, else asked for without echoing it.",
    )
    sign_in.add_argument(
        "--client-id",
        default=os.environ.get("OURA_CLIENT_ID"),
        help="your Oura API application's client ID (default: $OURA_CLIENT_ID)",
    )
    sign_in.add_argument(
        "--redirect-uri",
        default=login.DEFAULT_REDIRECT_URI,
        help="a redirect URI registered on that application (default: %(default)s)",
    )
    sign_in.add_argument(
        "--no-browser", action="store_true", help="print the sign-in address instead of opening a browser"
    )
    commands.add_parser("logout", help="revoke and delete the saved credentials")
    args = parser.parse_args(argv)

    if args.command is None:
        server.main()
        return
    try:
        if args.command == "login":
            _sign_in(parser, args)
        else:
            print(login.logout(auth.credentials_path()))
    except login.LoginError as exc:
        sys.exit(f"mcp-oura {args.command}: {exc}")
    except KeyboardInterrupt:
        sys.exit(f"mcp-oura {args.command}: cancelled.")


def _sign_in(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.client_id:
        parser.error("login needs your Oura application's client ID: pass --client-id or set OURA_CLIENT_ID.")
    secret = os.environ.get("OURA_CLIENT_SECRET") or getpass.getpass("Oura client secret: ")
    path = auth.credentials_path()
    login.login(
        args.client_id,
        secret,
        path=path,
        redirect_uri=args.redirect_uri,
        open_browser=(lambda url: False) if args.no_browser else None,
    )
    print(f"Signed in. Saved to {auth.display(path)}. Claude uses it the next time it starts the server.")
