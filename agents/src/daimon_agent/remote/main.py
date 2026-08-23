"""`daimon-remote` — run the gateway and pair a device with it."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from aiohttp import web

from .auth import CODE_TTL_S, CODE_TTL_TUNNEL_S
from .gateway import Gateway, create_gateway_app, default_state_dir
from .tunnel import Tunnel

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


def _print_banner(args, port: int, code: str | None, tunnel_url: str | None = None) -> None:
    print(f"\n  daimon remote — gateway on http://127.0.0.1:{port}", file=sys.stderr)

    if args.tunnel:
        if tunnel_url:
            print(f"  public tunnel:  {tunnel_url}", file=sys.stderr)
            print("  ⚠  that URL is reachable by anyone who has it.", file=sys.stderr)
        else:
            print("  tunnel unavailable — still reachable on http://127.0.0.1", file=sys.stderr)
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
        help="open a public cloudflared tunnel. The least safe mode: the URL is "
             "reachable by anyone who has it, so the token is the only gate. "
             "Shortens the pairing window accordingly.",
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
    tunnel = Tunnel() if args.tunnel else None

    async def open_tunnel(_app) -> None:
        if tunnel is not None:
            await tunnel.start(args.port)
            _print_banner(args, args.port, code, tunnel.url)

    async def close_tunnel(_app) -> None:
        if tunnel is not None:
            await tunnel.stop()

    async def listen_for_pair_requests(_app) -> None:
        """SIGUSR1 opens a new pairing window and prints the code.

        Pairing a second device used to mean restarting the gateway, which
        dropped every connected phone and tore down the tailnet mapping to mint
        eight characters that come from pure in-memory state. A signal costs
        none of that, adds no network surface, and needs no credential — only
        the process's parent can send it, which is exactly who is asking.

        The code goes to the log and nowhere else, the same way the one in the
        banner does: it lives in the gateway's memory for ten minutes, and the
        log is already how a human reads it.
        """

        def on_revoke_request() -> None:
            """SIGHUP revokes whatever the drop-file names.

            The app that supervises this process wants to show and un-pair
            devices without holding a gateway credential of its own, and
            without becoming a second writer of `tokens.json` — this file has
            exactly one writer by design, and that is worth keeping. So the
            supervisor writes a list of ids and asks; this does the work.
            """
            drop = gateway.tokens.path.parent / "revoke"
            try:
                wanted = [line.strip() for line in drop.read_text("utf-8").splitlines()]
            except OSError:
                return
            for device_id in filter(None, wanted):
                gateway.tokens.revoke(device_id)
            with contextlib.suppress(OSError):
                drop.unlink()

        def on_pair_request() -> None:
            fresh = gateway.pairing.start()
            ttl = int(gateway.pairing.ttl_s / 60)
            print(
                f"\n  pairing code:  {fresh}     (valid {ttl} minutes, one use)",
                file=sys.stderr,
                flush=True,
            )

        with contextlib.suppress(NotImplementedError, ValueError):
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGUSR1, on_pair_request)
            loop.add_signal_handler(signal.SIGHUP, on_revoke_request)

    app.on_startup.append(listen_for_pair_requests)

    if tunnel is not None:
        # The banner waits for the URL, so it can print it.
        app.on_startup.append(open_tunnel)
        app.on_cleanup.append(close_tunnel)
    else:
        _print_banner(args, args.port, code)

    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
