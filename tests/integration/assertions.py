#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from typing import List

from conftest import OTEL_COLLECTOR_APP_NAME
from jubilant import CLIError, Juju


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
