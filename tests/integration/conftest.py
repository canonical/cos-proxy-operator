#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import platform
from pathlib import Path

import yaml
from jubilant import Juju
from pytest import fixture
from pytest_jubilant import pack
from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger("conftest")
REPO_ROOT = Path(__file__).parent.parent.parent
METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = "cos-proxy"
APP_BASE = next(iter(METADATA["platforms"])).split(":")[0]
OTEL_COLLECTOR_APP_NAME = "opentelemetry-collector"
COS_CHANNEL = "2/edge"


def get_system_arch() -> str:
    """Return the architecture of this machine, mapping values to amd64 or arm64."""
    arch = platform.machine().lower()
    if arch in ["x86_64", "amd64"]:
        arch = "amd64"
    elif arch in ["aarch64", "arm64", "armv8b", "armv8l"]:
        arch = "arm64"
    return arch


@fixture(scope="module")
def patch_update_status_interval(juju: Juju):
    """Patch update-status-hook-interval to avoid interference with tests."""
    juju.model_config({"update-status-hook-interval": "1h"})
    yield
    juju.model_config(reset="update-status-hook-interval")


@retry(stop=stop_after_attempt(2), wait=wait_fixed(10))
def patch_otel_collector_log_level(juju: Juju, unit_no=0):
    """Patch the collector's log level to INFO for debug exporter inspection."""
    juju.ssh(
        f"{OTEL_COLLECTOR_APP_NAME}/{unit_no}",
        f"sudo sed -i 's/level: WARN/level: INFO/' "
        f"/etc/otelcol/config.d/{OTEL_COLLECTOR_APP_NAME}_{unit_no}.yaml",
    )
    juju.ssh(f"{OTEL_COLLECTOR_APP_NAME}/{unit_no}", "sudo snap restart opentelemetry-collector")


@fixture(scope="module", autouse=True)
def patch_model_constraints_architecture(juju: Juju):
    """Set model constraints to match the current architecture."""
    juju.cli("set-model-constraints", f"arch={get_system_arch()}")


@fixture(scope="module")
def charm():
    """Charm used for integration testing."""
    if charm_path := os.getenv("CHARM_PATH"):
        logger.info("using charm from env")
        return charm_path
    arch = get_system_arch()
    if Path(charm_file := REPO_ROOT / f"cos-proxy_ubuntu@24.04-{arch}.charm").exists():
        logger.info(f"using existing charm from {REPO_ROOT}")
        return charm_file
    logger.info(f"packing from {REPO_ROOT}")
    return pack(REPO_ROOT)
