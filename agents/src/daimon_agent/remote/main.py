"""`daimon-remote` — run the gateway and pair a device with it."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from aiohttp import web

from .auth import CODE_TTL_S, CODE_TTL_TUNNEL_S
from .gateway import Gateway, create_gateway_app, default_state_dir

DEFAULT_PORT = 4712


def _tailscale_hostname() -> str | None:
    """This machine's MagicDNS name, if Tailscale is up."""
    binary = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not Path(binary).exists():
        return None
    try:
        out = subprocess.run(
            [binary, "status", "--json"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        name = json.loads(out.stdout).get("Self", {}).get("DNSName", "")
    except ValueError:
        return None
    return name.rstrip(".") or None


def _print_banner(args, port: int, code: str | None) -> None:
    print(f"\n  daimon remote — gateway on http://127.0.0.1:{port}", file=sys.stderr)

    if args.tunnel:
        print("  reachable through the tunnel you started", file=sys.stderr)
    elif args.lan:
        print(f"  bound to {args.host} — reachable on your LAN", file=sys.stderr)
    else:
        host = _tailscale_hostname()
        if host:
            print(f"\n  put it on your tailnet:\n      tailscale serve --bg {port}", file=sys.stderr)
            print(f"\n  then open:  https://{host}/", file=sys.stderr)
        else:
            print(
                "\n  tailscale not found. Install it and run:\n"
                f"      tailscale serve --bg {port}\n"
                "  or use --lan to bind this port to your network instead.",
                file=sys.stderr,
            )

    if code:
        ttl = int((CODE_TTL_TUNNEL_S if args.tunnel else CODE_TTL_S) / 60)
        print(f"\n  pairing code:  {code}     (valid {ttl} minutes, one use)", file=sys.stderr)
    else:
        print("\n  paired devices only — run with --pair to add another", file=sys.stderr)

    if args.terminals:
        print(
            "\n  ⚠  terminals are exposed. A paired device gets a shell on this\n"
            "     machine, outside the agent's workspace sandbox.",
            file=sys.stderr,
        )
    print("", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daimon-remote",
        description="Reach your daimon sessions and terminals from another device.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--lan", action="store_true",
        help="bind 0.0.0.0 and reach it over your local network. Off by default: "
             "binding loopback is what makes Tailscale's identity headers trustworthy.",
    )
    parser.add_argument(
        "--tunnel", action="store_true",
        help="you are fronting this with a public tunnel. Shortens the pairing "
             "window, since the endpoint is world-reachable.",
    )
    parser.add_argument(
        "--terminals", action="store_true",
        help="let paired devices open and drive shells. A shell runs outside the "
             "workspace sandbox everything else here respects, so this is opt-in.",
    )
    parser.add_argument("--pair", action="store_true", help="print a pairing code and accept one new device")
    parser.add_argument("--login", action="append", default=[], metavar="EMAIL",
                        help="restrict Tailscale access to these logins (repeatable)")
    parser.add_argument("--devices", action="store_true", help="list paired devices and exit")
    parser.add_argument("--revoke", metavar="ID", help="revoke a paired device and exit")
    parser.add_argument("--state-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.host = "0.0.0.0" if args.lan else "127.0.0.1"

    state_dir = args.state_dir or default_state_dir()
    gateway = Gateway(
        state_dir=state_dir,
        bind_host=args.host,
        tailscale=not (args.lan or args.tunnel),
        allowed_logins=set(args.login),
        allow_terminals=args.terminals,
    )

    if args.devices:
        rows = gateway.tokens.devices()
        if not rows:
            print("no paired devices", file=sys.stderr)
        for device in rows:
            print(f"{device.id}  {device.label}")
        return 0

    if args.revoke:
        ok = gateway.tokens.revoke(args.revoke)
        print("revoked" if ok else "no such device", file=sys.stderr)
        return 0 if ok else 1

    # Pair on first run without being asked: a gateway with no devices and no
    # code is a locked door with no key, which is not a useful default.
    should_pair = args.pair or not gateway.tokens.devices()
    if should_pair and args.tunnel and not args.lan:
        gateway.pairing.ttl_s = CODE_TTL_TUNNEL_S
    code = gateway.pairing.start() if should_pair else None

    app = create_gateway_app(gateway)
    _print_banner(args, args.port, code)
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
