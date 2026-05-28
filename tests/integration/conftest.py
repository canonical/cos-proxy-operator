#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Optional

import jubilant
import sh  # type: ignore[import-untyped]
import yaml
from pytest import fixture
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


def pack(root: Path, platform: Optional[str] = None) -> Path:
    """Pack a local charm and return it."""
    args = ["-p", str(root)]
    if platform:
        args.extend(["--platform", platform])

    # charmcraft outputs "Packed <charm_file>" lines to stderr
    charmcraft = sh.Command("charmcraft")
    output = charmcraft.pack(*args, _err_to_out=True)

    packed_charms = [
        line.split()[1] for line in str(output).strip().splitlines() if line.startswith("Packed")
    ]

    if not packed_charms:
        raise ValueError(f"Unable to get packed charm from charmcraft output: {output}")

    if len(packed_charms) > 1:
        raise ValueError(
            "This charm supports multiple platforms. "
            "Pass a `platform` argument to control which charm you're getting."
        )

    return Path(packed_charms[0]).resolve()


@fixture(scope="module")
def patch_update_status_interval(juju: jubilant.Juju):
    """Patch update-status-hook-interval to avoid interference with tests."""
    juju.model_config({"update-status-hook-interval": "1h"})
    yield
    juju.model_config(reset="update-status-hook-interval")


@retry(stop=stop_after_attempt(2), wait=wait_fixed(10))
def patch_otel_collector_log_level(juju: jubilant.Juju, unit_no: int = 0):
    """Patch the collector's log level to INFO for debug exporter inspection."""
    juju.ssh(
        f"{OTEL_COLLECTOR_APP_NAME}/{unit_no}",
        f"sudo sed -i 's/level: WARN/level: INFO/' "
        f"/etc/otelcol/config.d/{OTEL_COLLECTOR_APP_NAME}_{unit_no}.yaml",
    )
    juju.ssh(f"{OTEL_COLLECTOR_APP_NAME}/{unit_no}", "sudo snap restart opentelemetry-collector")


@fixture(scope="module")
def juju():
    """Juju instance with a temporary model for testing."""
    keep_models: bool = os.environ.get("KEEP_MODELS") is not None
    with jubilant.temp_model(keep=keep_models) as juju:
        juju.cli("set-model-constraints", f"arch={get_system_arch()}")
        yield juju


@fixture(scope="module")
def charm():
    """Charm used for integration testing."""
    if charm_path := os.getenv("CHARM_PATH"):
        logger.info("using charm from env: %s", charm_path)
        return charm_path

    arch = get_system_arch()
    charm_file = REPO_ROOT / f"cos-proxy_ubuntu@24.04-{arch}.charm"
    if charm_file.exists():
        logger.info("using existing charm: %s", charm_file)
        return str(charm_file)

    logger.info("packing charm from %s", REPO_ROOT)
    return str(pack(REPO_ROOT, platform=f"ubuntu@24.04:{arch}"))
