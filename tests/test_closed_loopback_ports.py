"""--aliasmode-closed-loopback-ports: a listed loopback port answers a page as a free port does."""

from __future__ import annotations

import collections
import secrets
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading

import harness

ROUNDS = 20
METHODS = ("fetch", "websocket", "turn")
#: Two medians differ by no more than this many milliseconds, or this share of the larger one.
MEDIAN_SLACK_MS = 5
MEDIAN_SLACK_SHARE = 0.3


class _Listener:
    """A service on a loopback port that counts the connections it accepts."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(64)
        self.port = self._socket.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            self.accepted += 1
            connection.close()

    def close(self) -> None:
        self._socket.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _run(server, binary, url, token, args, timeout):
    """The bare binary on `url`, no automation, as harness.run_probe launches it."""
    user_data_dir = tempfile.mkdtemp(prefix="apostate-loopback-")
    command = [binary, f"--user-data-dir={user_data_dir}", "--no-first-run", "--no-default-browser-check",
               "--headless=new", f"--fingerprint-locale={harness.LOCALE}", f"--fingerprint-timezone={harness.TIMEZONE}",
               *args]
    if harness._sandbox_unavailable():
        command.append("--no-sandbox")
    command.append(url)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return server.wait(token, timeout)
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)


def test_listed_port_answers_as_a_free_port(server, binary, record):
    listener = _Listener()
    free = _free_port()
    token = secrets.token_hex(8)
    url = f"{server.origin}/loopback.html?token={token}&listed={listener.port}&free={free}&rounds={ROUNDS}"
    args = [
        f"--aliasmode-closed-loopback-ports={listener.port}",
        # The page then counts as public, as a page from the internet does.
        f"--ip-address-space-overrides=127.0.0.1:{server.port}=public",
    ]
    try:
        values = _run(server, binary, url, token, args, timeout=900)
    finally:
        listener.close()
    record.update(listed=listener.port, free=free, accepted=listener.accepted)
    assert "error" not in values, values.get("error")
    for method in METHODS:
        listed = values[method]["listed"]
        free_side = values[method]["free"]
        outcomes = {side: collections.Counter(probe["outcome"] for probe in probes)
                    for side, probes in (("listed", listed), ("free", free_side))}
        medians = {side: statistics.median(probe["ms"] for probe in probes)
                   for side, probes in (("listed", listed), ("free", free_side))}
        record[method] = {"outcomes": {side: dict(counts) for side, counts in outcomes.items()}, "median_ms": medians}
        assert outcomes["listed"] == outcomes["free"], (method, outcomes)
        slack = max(MEDIAN_SLACK_MS, MEDIAN_SLACK_SHARE * max(medians.values()))
        assert abs(medians["listed"] - medians["free"]) <= slack, (method, medians)
    assert listener.accepted == 0
