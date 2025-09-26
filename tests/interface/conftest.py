# # Copyright 2022 Canonical Ltd.
# # See LICENSE file for licensing details.
# from unittest.mock import patch

import pytest
from interface_tester import InterfaceTester

from charm import COSProxyCharm


@pytest.fixture
def interface_tester(interface_tester: InterfaceTester):
    interface_tester.configure(
        charm_type=COSProxyCharm,
    )
    yield interface_tester
