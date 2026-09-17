"""Bounded, read-only boot decisions; an unavailable helper is not an offline link."""
import http.client
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:8765"
DECISION = Path("/run/cloudplay-startup/decision.json")
WAIT_SECONDS = 30


def status(url=URL):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/api/status", timeout=1) as response:
            state = json.load(response)
        if isinstance(state, dict) and isinstance(state.get("connected"), bool):
            return state
    except (OSError, ValueError, http.client.HTTPException):
        pass
    return None


def network_snapshot():
    # Bound the complete D-Bus connection/introspection/device enumeration, not
    # just individual method calls. The child only calls Network.snapshot().
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--network"],
            capture_output=True, text=True, timeout=2, check=True)
        state = json.loads(result.stdout)
        if isinstance(state, dict) and isinstance(state.get("connected"), bool):
            return state
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def observe():
    state = status()
    if state and state["connected"]:
        return state, {"connected": True}
    return state, network_snapshot()


def choose(state, network, expired):
    if (state and state["connected"]) or (network and network["connected"]):
        return "online"
    if not expired:
        return None
    # Never launch a dead localhost URL because the helper is starting, broken
    # or returning malformed data. Only a functioning, confirmed-offline helper
    # can request interactive setup; otherwise fall back to the actual service.
    if state and not state["connected"] and state.get("phase") == "setup":
        return "setup"
    return "online"


def cached_decision(path=DECISION):
    try:
        state = json.loads(path.read_text())
        age = time.monotonic() - state["at"]
        if 0 <= age <= 60 and state["mode"] in ("online", "setup"):
            return state["mode"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def wait_for_mode(stopping, use_cache=True, progress=lambda _: None):
    cached = cached_decision() if use_cache else None
    if cached == "online":
        return "online"
    deadline = time.monotonic() + (0 if cached else WAIT_SECONDS)
    progress("Connecting to network")
    while not stopping.is_set():
        state, network = observe()
        mode = choose(state, network, time.monotonic() >= deadline)
        if mode:
            if mode == "setup":
                progress("Wi-Fi setup required — Ethernet also works")
            elif (state and state["connected"]) or (network and network["connected"]):
                progress("Network connected — opening Cloudplay Home")
            else:
                progress("Opening Cloudplay Home")
            return mode
        stopping.wait(0.5)
    return None


def readonly_network():
    from network import Network
    net = Network()
    try:
        print(json.dumps(net.snapshot()))
    finally:
        net.bus.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["--network"]:
        raise SystemExit(2)
    try:
        readonly_network()
    except Exception:
        # The supervising caller treats failure as unknown, not disconnected.
        # Never echo exception messages or connection settings.
        raise SystemExit(1) from None
