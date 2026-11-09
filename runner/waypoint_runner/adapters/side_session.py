"""Adapter: run a node in a side session of an open Claude session, through the Waypoint mod.

Separate from the subagent adapter on purpose: a side session is its own session, not a subagent of the
caller. It uses the same request/receipt files (route ``side_session``). With no open Claude session the job
waits (``waiting-for-session``); a side session is never opened in a new chat by default, so the request is
only written once an open session exists. The receipt records which session took the job (``session_id`` in
``jobs/<job_id>/adapter.json``). The runner never calls a model; floors and toggles are honoured exactly as in
``session_subagent`` (dispatcher check plus a re-check before the request is written).
"""
from __future__ import annotations

from .session_subagent import WAITING, ModBridgeAdapter

__all__ = ["SideSessionAdapter", "WAITING"]


class SideSessionAdapter(ModBridgeAdapter):
    name = "side_session"
    route = "side_session"
