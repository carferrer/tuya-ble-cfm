"""Bounded, credential-free diagnostics for DLOCK-X1 local opening attempts."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime
from time import monotonic


class UnlockAttempt:
    """Record metadata only; never retain commands, seeds or exception text."""

    def __init__(self, device):
        self.device = device
        self.started = monotonic()
        history = getattr(device, "_dlock_dp71_attempts", None)
        if history is None:
            history = device._dlock_dp71_attempts = deque(maxlen=20)
        number = getattr(device, "_dlock_attempt_number", 0) + 1
        device._dlock_attempt_number = number
        self.data = {
            "attempt": number,
            "started_at": datetime.now(UTC).isoformat(),
            "connected_at_start": bool(device.connected),
            "status": "pending",
            "transport_ack_received": False,
            "matching_response_received": False,
            "unmatched_reports": 0,
            "packets_written": 0,
            "timeline": [],
        }
        history.append(self.data)
        self.phase("queued")

    def phase(self, name):
        self.data["phase"] = name
        self.data["timeline"].append({
            "phase": name, "elapsed_ms": round((monotonic() - self.started) * 1000),
        })

    def __enter__(self):
        return self

    def activate(self):
        self.device._dlock_active_unlock_attempt = self

    def __exit__(self, kind, error, traceback):
        if self.data["status"] == "pending":
            if isinstance(error, asyncio.CancelledError):
                status = "cancelled"
            elif isinstance(error, TimeoutError):
                status = "timeout"
            elif self.data["phase"] == "configuration":
                status = "invalid_configuration"
            elif self.data["phase"] in ("connecting", "settling"):
                status = "connection_error"
            else:
                status = "transport_error" if error is not None else "confirmed"
            self.data["status"] = status
        self.data["finished_at"] = datetime.now(UTC).isoformat()
        self.data["elapsed_ms"] = round((monotonic() - self.started) * 1000)
        self.data["connected_at_end"] = bool(self.device.connected)
        if getattr(self.device, "_dlock_active_unlock_attempt", None) is self:
            self.device._dlock_active_unlock_attempt = None


def attempt_history(device):
    """Return an independent JSON-safe snapshot."""
    return deepcopy(list(getattr(device, "_dlock_dp71_attempts", ())))


def notify_local_open(device):
    """Notify only this unit's lock after a matching successful DP71 response."""
    for callback in tuple(getattr(device, "_dlock_open_listeners", ())):
        callback()
