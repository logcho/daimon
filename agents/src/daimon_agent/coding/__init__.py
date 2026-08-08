"""Coding agent — kernel-as-primary-tool, port of Prime Intellect's approach.

Shares one checkpointer, memory store, and event contract with the general agent.
The server routes to this graph when the request body includes `"agent": "coding"`.
"""
