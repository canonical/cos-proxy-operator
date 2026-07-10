#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import List

from conftest import OTEL_COLLECTOR_APP_NAME
from jubilant import CLIError, Juju

# A rule name that is unique to telegraf's built-in alert rules
# (see: tests/integration README, telegraf rev75 ships CPU_Usage, DiskFull, etc.)
TELEGRAF_RULE_MARKER = "CPU_Usage"


def assert_pattern_in_snap_logs(juju: Juju, grep_filters: List[str]):
    """Assert that patterns appear in the opentelemetry-collector snap logs."""
    if not grep_filters:
        raise ValueError("grep_filters must not be empty")

    cmd = (
        "sudo snap logs opentelemetry-collector -n=all"
        + " | "
        + " | ".join([f"grep '{p}'" for p in grep_filters])
    )
    try:
        otelcol_logs = juju.ssh(f"{OTEL_COLLECTOR_APP_NAME}/0", command=cmd)
    except CLIError:
        raise AssertionError(
            f"Failed to fetch logs with filters {grep_filters!r} from {OTEL_COLLECTOR_APP_NAME}/0"
        )

    assert otelcol_logs, f"Logs matching {grep_filters!r} not found in {OTEL_COLLECTOR_APP_NAME}/0"


def assert_pattern_absent_in_otelcol_config(juju: Juju, pattern: str):
    """Assert that a pattern does NOT appear in the otelcol generated config file."""
    config_path = f"/etc/otelcol/config.d/{OTEL_COLLECTOR_APP_NAME}_0.yaml"
    try:
        config = juju.ssh(
            f"{OTEL_COLLECTOR_APP_NAME}/0",
            command=f"sudo cat {config_path} | grep '{pattern}' || true",
        )
    except CLIError:
        return  # File absent = pattern absent = success
    assert not config.strip(), (
        f"Pattern {pattern!r} was unexpectedly found in otelcol config after relation removal"
    )


def get_alert_rule_files_in_otelcol(juju: Juju) -> str:
    """Return a string of all alert rule file paths in otelcol's charm directory."""
    unit = f"{OTEL_COLLECTOR_APP_NAME}/0"
    unit_dir = f"unit-{OTEL_COLLECTOR_APP_NAME.replace('-', '-')}-0"
    rules_dir = f"/var/lib/juju/agents/{unit_dir}/charm/prometheus_alert_rules"
    try:
        return juju.ssh(unit, command=f"find {rules_dir} -type f 2>/dev/null || true")
    except CLIError:
        return ""


def get_alert_rules_content_in_otelcol(juju: Juju, pattern: str) -> str:
    """Return lines matching pattern in the content of otelcol's alert rule files.

    Uses grep -r over the prometheus_alert_rules directory so callers can test for
    the presence or absence of specific rule names that are unique to telegraf
    (e.g. ``CPU_Usage``, ``DiskFull``).
    """
    unit = f"{OTEL_COLLECTOR_APP_NAME}/0"
    unit_dir = f"unit-{OTEL_COLLECTOR_APP_NAME.replace('-', '-')}-0"
    rules_dir = f"/var/lib/juju/agents/{unit_dir}/charm/prometheus_alert_rules"
    try:
        return juju.ssh(
            unit,
            command=f"grep -r '{pattern}' {rules_dir} 2>/dev/null || true",
        )
    except CLIError:
        return ""
