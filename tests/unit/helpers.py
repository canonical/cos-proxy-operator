# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
from pathlib import Path

import yaml

CHARM_ROOT = Path(__file__).parent.parent.parent


def get_charm_meta() -> dict:
    raw_meta = (CHARM_ROOT / "charmcraft").with_suffix(".yaml").read_text()
    return yaml.safe_load(raw_meta)
