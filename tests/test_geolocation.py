"""--fingerprint-geolocation: after the stock prompt a page reads the switch's position, never the host's."""

from __future__ import annotations

import contextlib

import pytest

import harness

SCRIPT = (harness.PAGES / "geolocation.js").read_text()

LATITUDE, LONGITUDE, ACCURACY = 48.85837, 2.294481, 35.0
UNSET = ("altitude", "altitudeAccuracy", "heading", "speed")
FIRST_FIX_MS = 400


@contextlib.contextmanager
def _page(server, binary, pytestconfig, value):
    import apostate

    browser = apostate.launch(
        fingerprint="42", geoip=False, locale=harness.LOCALE, timezone=harness.TIMEZONE,
        headless=not pytestconfig.getoption("--headed"), binary_path=binary,
        args=[f"--fingerprint-geolocation={value}"],
    )
    try:
        page = browser.new_page()
        page.goto(f"{server.origin}/geolocation.html")
        yield page.context, page
    finally:
        browser.close()


def _call(page, expression):
    # Evaluated rather than called on window, so it works in an isolated world too.
    return page.evaluate(f"async () => {{\n{SCRIPT}\nreturn await {expression};\n}}")


def test_switch_position_after_the_prompt(server, binary, pytestconfig, record):
    with _page(server, binary, pytestconfig, f"{LATITUDE},{LONGITUDE},{ACCURACY:g}") as (context, page):
        before = _call(page, "geolocationPermission()")
        context.grant_permissions(["geolocation"], origin=server.origin)
        read = _call(page, "geolocationRead()")
        watched = _call(page, "geolocationWatch(3000)")
    record.update(permission_before=before, read=read, watched=watched)
    assert before == "prompt"
    assert read["ok"], read
    coords = read["coords"]
    assert (coords["latitude"], coords["longitude"], coords["accuracy"]) == (LATITUDE, LONGITUDE, ACCURACY)
    assert all(coords[name] is None for name in UNSET), coords
    assert read["ms"] >= FIRST_FIX_MS, read
    assert abs(read["now"] - read["timestamp"]) < 5000, read
    assert len(watched) == 1 and watched[0]["ok"], watched
    assert watched[0]["coords"] == coords


@pytest.mark.parametrize("value", ["paris", "91,0", "48.8,2.3,-1"])
def test_bad_value_is_position_unavailable(server, binary, pytestconfig, record, value):
    with _page(server, binary, pytestconfig, value) as (context, page):
        context.grant_permissions(["geolocation"], origin=server.origin)
        read = _call(page, "geolocationRead()")
    record.update(value=value, read=read)
    assert not read["ok"], read
    assert read["code"] == 2, read
