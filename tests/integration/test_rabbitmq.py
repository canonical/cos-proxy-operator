#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for rabbitmq-server compatibility: rabbitmq → cos-proxy → otelcol.

Verifies that dashboards (dashboard+name format), alert rules, and scrape targets
from a charm-helpers/reactive charm (rabbitmq-server) propagate correctly through
cos-proxy to the opentelemetry-collector.
"""

import logging

import jubilant
import pytest
from assertions import (
    RABBITMQ_DASHBOARD_MARKER,
    RABBITMQ_RULE_MARKER,
    get_alert_rules_content_in_otelcol,
    get_dashboard_content_in_otelcol,
    get_scrape_config_content_in_otelcol,
)
from conftest import (
    APP_BASE,
    APP_NAME,
    OTEL_COLLECTOR_APP_NAME,
    deploy_otelcol,
)
from jubilant import Juju
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.usefixtures("patch_update_status_interval")

RABBITMQ_APP_NAME = "rabbitmq-server"
RABBITMQ_CHANNEL = "latest/edge"

RETRY = retry(
    retry=retry_if_exception_type(AssertionError),
    wait=wait_exponential(multiplier=1, min=2, max=45),
    stop=stop_after_attempt(10),
    after=after_log(logger, logging.INFO),
)


def test_deploy_cos_proxy(juju: Juju, charm: str):
    """Deploy cos-proxy. Expect BlockedStatus: no upstream or downstream relations yet."""
    juju.deploy(charm, APP_NAME, base=APP_BASE)
    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, APP_NAME) and jubilant.all_agents_idle(status, APP_NAME)
        ),
        error=jubilant.any_error,
        timeout=10 * 60,
    )


def test_deploy_otelcol_and_integrate(juju: Juju):
    """Deploy otelcol and integrate with cos-proxy via cos-agent."""
    deploy_otelcol(juju)
    juju.integrate(f"{APP_NAME}:cos-agent", f"{OTEL_COLLECTOR_APP_NAME}:cos-agent")
    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, APP_NAME, OTEL_COLLECTOR_APP_NAME)
            and jubilant.all_agents_idle(status, APP_NAME, OTEL_COLLECTOR_APP_NAME)
        ),
        error=jubilant.any_error,
        timeout=10 * 60,
    )


def test_deploy_rabbitmq_and_integrate(juju: Juju):
    """Deploy rabbitmq-server, integrate dashboards, alert rules, and scrape targets."""
    juju.deploy(RABBITMQ_APP_NAME, channel=RABBITMQ_CHANNEL, base="ubuntu@24.04")

    # Logs, Metrics, Alert rules, Dashboards
    juju.integrate(f"{RABBITMQ_APP_NAME}:juju-info", f"{OTEL_COLLECTOR_APP_NAME}:juju-info")
    juju.integrate(f"{APP_NAME}:prometheus-target", f"{RABBITMQ_APP_NAME}:scrape")
    juju.integrate(f"{APP_NAME}:prometheus-rules", f"{RABBITMQ_APP_NAME}:prometheus-rules")
    juju.integrate(f"{APP_NAME}:dashboards", f"{RABBITMQ_APP_NAME}:dashboards")
    juju.wait(
        lambda status: (
            jubilant.all_active(status, APP_NAME, RABBITMQ_APP_NAME)
            and jubilant.all_blocked(status, OTEL_COLLECTOR_APP_NAME)
            and jubilant.all_agents_idle(
                status, APP_NAME, RABBITMQ_APP_NAME, OTEL_COLLECTOR_APP_NAME
            )
        ),
        error=jubilant.any_error,
        timeout=25 * 60,
    )


@RETRY
def test_dashboards_appear_in_otelcol(juju: Juju):
    """Verify that rabbitmq-server's dashboard arrives in otelcol via the dashboard+name format."""
    content = get_dashboard_content_in_otelcol(juju, RABBITMQ_DASHBOARD_MARKER)
    assert content.strip(), (
        f"Expected dashboard containing {RABBITMQ_DASHBOARD_MARKER!r} in otelcol but got nothing"
    )


@RETRY
def test_alert_rules_appear_in_otelcol(juju: Juju):
    """Verify that rabbitmq-server's alert rules arrive in otelcol."""
    content = get_alert_rules_content_in_otelcol(juju, RABBITMQ_RULE_MARKER)
    assert content.strip(), (
        f"Expected alert rule marker {RABBITMQ_RULE_MARKER!r} in otelcol alert rules "
        f"but got nothing"
    )


@RETRY
def test_scrape_targets_appear_in_otelcol(juju: Juju):
    """Verify that rabbitmq-server's scrape target arrives in otelcol's generated config."""
    content = get_scrape_config_content_in_otelcol(juju, RABBITMQ_APP_NAME)
    assert content.strip(), (
        f"Expected {RABBITMQ_APP_NAME!r} in otelcol scrape config but got nothing"
    )


def test_remove_rabbitmq_relations(juju: Juju):
    """Remove all relations between cos-proxy and rabbitmq-server."""
    for relation in ["dashboards", "prometheus-rules", "prometheus-target"]:
        endpoint = f"{APP_NAME}:{relation}"
        rabbitmq_endpoint = {
            "dashboards": f"{RABBITMQ_APP_NAME}:dashboards",
            "prometheus-rules": f"{RABBITMQ_APP_NAME}:prometheus-rules",
            "prometheus-target": f"{RABBITMQ_APP_NAME}:scrape",
        }[relation]
        juju.remove_relation(endpoint, rabbitmq_endpoint)

    juju.wait(
        lambda status: (
            jubilant.all_blocked(status, APP_NAME) and jubilant.all_agents_idle(status, APP_NAME)
        ),
        error=jubilant.any_error,
        timeout=10 * 60,
    )


@RETRY
def test_dashboards_absent_from_otelcol(juju: Juju):
    """Verify that rabbitmq-server's dashboard is removed from otelcol after relation removal."""
    content = get_dashboard_content_in_otelcol(juju, RABBITMQ_DASHBOARD_MARKER)
    assert not content.strip(), (
        f"Expected dashboard {RABBITMQ_DASHBOARD_MARKER!r} to be absent after removal "
        f"but found:\n{content}"
    )


@RETRY
def test_alert_rules_absent_from_otelcol(juju: Juju):
    """Verify that rabbitmq-server's alert rules are removed from otelcol after relation removal."""
    content = get_alert_rules_content_in_otelcol(juju, RABBITMQ_RULE_MARKER)
    assert not content.strip(), (
        f"Expected alert rule {RABBITMQ_RULE_MARKER!r} to be absent after removal "
        f"but found:\n{content}"
    )


@RETRY
def test_scrape_targets_absent_from_otelcol(juju: Juju):
    """Verify that rabbitmq-server's scrape target is removed from otelcol after relation removal."""
    content = get_scrape_config_content_in_otelcol(juju, RABBITMQ_APP_NAME)
    assert not content.strip(), (
        f"Expected {RABBITMQ_APP_NAME!r} in otelcol scrape config to be absent after removal "
        f"but found:\n{content}"
    )
