#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for alert rule propagation: telegraf → cos-proxy → otelcol."""

import jubilant
import pytest
from assertions import TELEGRAF_RULE_MARKER, get_alert_rules_content_in_otelcol
from conftest import (
    APP_BASE,
    APP_NAME,
    OTEL_COLLECTOR_APP_NAME,
    TELEGRAF_APP_NAME,
    TELEGRAF_BASE,
    UBUNTU_APP_NAME,
    deploy_otelcol,
)
from jubilant import Juju
from tenacity import retry, stop_after_attempt, wait_fixed

pytestmark = pytest.mark.usefixtures("patch_update_status_interval")


def test_deploy_cos_proxy(juju: Juju, charm: str):
    """Deploy cos-proxy. Expect BlockedStatus: no upstream or downstream relations yet."""
    juju.deploy(charm, APP_NAME, base=APP_BASE)
    juju.wait(
        lambda status: jubilant.all_blocked(status, APP_NAME),
        timeout=10 * 60,
        delay=10,
        successes=3,
    )


def test_deploy_otelcol_and_integrate(juju: Juju):
    """Deploy otelcol and integrate with cos-proxy via cos-agent."""
    deploy_otelcol(juju)
    juju.integrate(
        f"{APP_NAME}:cos-agent",
        f"{OTEL_COLLECTOR_APP_NAME}:cos-agent",
    )
    juju.wait(
        lambda status: jubilant.all_blocked(status, APP_NAME),
        timeout=10 * 60,
        delay=10,
        successes=3,
    )


def test_deploy_telegraf_and_integrate(juju: Juju):
    """Deploy ubuntu + telegraf, integrate telegraf's alert rules with cos-proxy."""
    juju.deploy(UBUNTU_APP_NAME, channel="latest/stable", base=TELEGRAF_BASE)
    juju.deploy(TELEGRAF_APP_NAME, channel="latest/stable")
    juju.integrate(f"{TELEGRAF_APP_NAME}:juju-info", f"{UBUNTU_APP_NAME}:juju-info")
    juju.integrate(
        f"{APP_NAME}:prometheus-rules",
        f"{TELEGRAF_APP_NAME}:prometheus-rules",
    )
    juju.wait(
        lambda status: jubilant.all_active(status, APP_NAME),
        error=jubilant.any_error,
        timeout=25 * 60,
        delay=10,
        successes=3,
    )


@retry(stop=stop_after_attempt(20), wait=wait_fixed(15))
def test_alert_rules_appear_in_otelcol(juju: Juju):
    """Verify that telegraf's alert rules appear in otelcol's prometheus_alert_rules directory."""
    content = get_alert_rules_content_in_otelcol(juju, TELEGRAF_RULE_MARKER)
    assert content.strip(), (
        f"Expected telegraf rule marker {TELEGRAF_RULE_MARKER!r} in otelcol alert rules but got nothing"
    )


def test_forward_alert_rules_false(juju: Juju):
    """Verify that setting forward_alert_rules=false clears alert rules from otelcol."""
    juju.config(APP_NAME, {"forward_alert_rules": "false"})
    juju.wait(
        lambda status: jubilant.all_active(status, APP_NAME),
        error=jubilant.any_error,
        timeout=5 * 60,
        delay=10,
        successes=3,
    )

    @retry(stop=stop_after_attempt(20), wait=wait_fixed(10))
    def _assert_rules_gone():
        content = get_alert_rules_content_in_otelcol(juju, TELEGRAF_RULE_MARKER)
        assert not content.strip(), (
            f"Expected telegraf rule {TELEGRAF_RULE_MARKER!r} to be absent but found:\n{content}"
        )

    _assert_rules_gone()


def test_forward_alert_rules_true(juju: Juju):
    """Verify that re-enabling forward_alert_rules=true restores alert rules in otelcol."""
    juju.config(APP_NAME, {"forward_alert_rules": "true"})
    juju.wait(
        lambda status: jubilant.all_active(status, APP_NAME),
        error=jubilant.any_error,
        timeout=5 * 60,
        delay=10,
        successes=3,
    )

    @retry(stop=stop_after_attempt(20), wait=wait_fixed(10))
    def _assert_rules_back():
        content = get_alert_rules_content_in_otelcol(juju, TELEGRAF_RULE_MARKER)
        assert content.strip(), (
            f"Expected telegraf rule {TELEGRAF_RULE_MARKER!r} to return but got nothing"
        )

    _assert_rules_back()


def test_remove_telegraf_relation(juju: Juju):
    """Remove the prometheus-rules relation between cos-proxy and telegraf."""
    juju.remove_relation(
        f"{APP_NAME}:prometheus-rules",
        f"{TELEGRAF_APP_NAME}:prometheus-rules",
    )
    juju.wait(
        lambda status: jubilant.all_blocked(status, APP_NAME),
        timeout=10 * 60,
        delay=10,
        successes=3,
    )


@retry(stop=stop_after_attempt(20), wait=wait_fixed(10))
def test_alert_rules_absent_from_otelcol(juju: Juju):
    """Verify that telegraf's alert rules are removed from otelcol after relation removal."""
    content = get_alert_rules_content_in_otelcol(juju, TELEGRAF_RULE_MARKER)
    assert not content.strip(), (
        f"Expected telegraf rule {TELEGRAF_RULE_MARKER!r} to be absent after removal but found:\n{content}"
    )
