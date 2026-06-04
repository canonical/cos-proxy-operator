#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for charm tracing via cos-agent."""

import jubilant
import pytest
from assertions import assert_pattern_in_snap_logs
from conftest import (
    APP_BASE,
    APP_NAME,
    COS_CHANNEL,
    OTEL_COLLECTOR_APP_NAME,
    patch_otel_collector_log_level,
)
from jubilant import Juju
from pytest_bdd import given, then, when
from tenacity import retry, stop_after_attempt, wait_fixed

pytestmark = pytest.mark.usefixtures("patch_update_status_interval")


def _trigger_update_status_event(juju: Juju, unit_name: str):
    """Manually trigger an update-status hook on a charm unit via SSH."""
    juju.ssh(
        unit_name,
        f"sudo /usr/bin/juju-exec -u {unit_name} "
        "JUJU_DISPATCH_PATH=hooks/update-status "
        f"JUJU_MODEL_NAME={juju.model} "
        f"JUJU_UNIT_NAME={unit_name} "
        f"/var/lib/juju/agents/unit-{unit_name.replace('/', '-')}/charm/dispatch",
    )


@pytest.mark.setup
@given("a cos-proxy charm is deployed")
def test_deploy_cos_proxy(juju: Juju, charm):
    """Deploy the cos-proxy charm."""
    juju.deploy(charm, APP_NAME)
    juju.wait(
        lambda status: jubilant.all_blocked(status, APP_NAME),
        timeout=10 * 60,
        delay=10,
        successes=3,
    )


@pytest.mark.setup
@when("an opentelemetry-collector charm is deployed")
def test_deploy_otel_collector(juju: Juju):
    """Deploy the opentelemetry-collector charm."""
    config = {"tracing_sampling_rate_workload": 100}
    juju.deploy(OTEL_COLLECTOR_APP_NAME, channel="dev/edge", base=APP_BASE, config=config)


@pytest.mark.setup
@when("integrated with the cos-proxy over cos-agent")
def test_integrate_cos_agent(juju: Juju):
    """Integrate cos-proxy with opentelemetry-collector via cos-agent."""
    juju.integrate(
        APP_NAME + ":cos-agent",
        OTEL_COLLECTOR_APP_NAME + ":cos-agent",
    )
    juju.wait(
        lambda status: jubilant.all_blocked(status, OTEL_COLLECTOR_APP_NAME),
        timeout=10 * 60,
        delay=10,
        successes=3,
    )
    juju.wait(
        lambda status: jubilant.all_blocked(status, APP_NAME),
        timeout=10 * 60,
        delay=10,
        successes=6,
    )

    patch_otel_collector_log_level(juju)


@then("charm traces are pushed to the collector")
@retry(stop=stop_after_attempt(10), wait=wait_fixed(10))
def test_charm_traces_are_pushed(juju: Juju):
    """Verify charm traces are pushed to the collector."""
    _trigger_update_status_event(juju, f"{APP_NAME}/0")
    grep_filters = ["ResourceTraces", f"service.name={APP_NAME}", "charm=cos-proxy"]
    assert_pattern_in_snap_logs(juju, grep_filters)
