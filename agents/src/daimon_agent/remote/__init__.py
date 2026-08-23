"""The remote gateway: the one process on this machine that faces the network.

The agent servers stay exactly as they were — bound to 127.0.0.1, trusting
every caller, no auth. That is deliberate and it is the main reason this is a
separate package rather than more routes on the agent server: adding a network
listener there would put arbitrary shell execution (`POST /task`, and now
terminals) one routing mistake away from the LAN. Here, the agent server has
no network-facing route to leak, because it has no network listener at all.

The gateway binds loopback too. `tailscale serve` proxies to it, which is what
provides TLS, a stable hostname, and — because the backend is loopback-only —
trustworthy `Tailscale-User-Login` identity headers.
"""
