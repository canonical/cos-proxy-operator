import base64
import lzma
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from charm import COSProxyCharm
from ops.testing import Harness


@patch.object(lzma, "compress", new=lambda x, *args, **kwargs: x)
@patch.object(lzma, "decompress", new=lambda x, *args, **kwargs: x)
@patch.object(base64, "b64encode", new=lambda x: x)
@patch.object(base64, "b64decode", new=lambda x: x)
class TestRelationMonitors(unittest.TestCase):
    def setUp(self):
        self.mock_enrichment_file = Path(tempfile.mktemp())

        for p in [
            patch("charm.remove_package"),
            patch.object(COSProxyCharm, "_setup_nrpe_exporter"),
            patch.object(COSProxyCharm, "_start_vector"),
            patch.object(COSProxyCharm, "path", property(lambda *_: self.mock_enrichment_file)),
        ]:
            p.start()
            self.addCleanup(p.stop)

        self.harness = Harness(COSProxyCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.set_model_info(name="mymodel", uuid="fe2c9bbb-58ab-40e4-8f70-f27480093fca")
        self.harness.set_leader(True)
        self.harness.begin_with_initial_hooks()

    def tearDown(self):
        self.mock_enrichment_file.unlink(missing_ok=True)

    def test_monitors_changed(self):
        # The unit data below were obtained from the output of:
        # juju show-unit \
        #   cos-proxy/0 --format json | jq '."cos-proxy/0"."relation-info"[0]."related-units"."nrpe/0".data'
        self.harness.add_network("10.41.168.226")

        unit_data = {
            "egress-subnets": "10.41.168.226/32",
            "ingress-address": "10.41.168.226",
            "machine_id": "1",
            "model_id": "fe2c9bbb-58ab-40e4-8f70-f27480093fca",
            "monitors": "{'monitors': {'remote': {'nrpe': {'check_conntrack': 'check_conntrack', 'check_systemd_scopes': 'check_systemd_scopes', 'check_reboot': 'check_reboot'}}}, 'version': '0.3'}",
            "private-address": "10.41.168.226",
            "target-address": "10.41.168.226",
            "target-id": "ubuntu-0",
        }
        rel_id = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id, "nrpe/0")
        self.harness.update_relation_data(rel_id, "nrpe/0", unit_data)

        expected = "\n".join(
            [
                "composite_key,juju_application,juju_unit,command,ipaddr",
                "10.41.168.226_check_conntrack,ubuntu,ubuntu/0,check_conntrack,10.41.168.226",
                "10.41.168.226_check_systemd_scopes,ubuntu,ubuntu/0,check_systemd_scopes,10.41.168.226",
                "10.41.168.226_check_reboot,ubuntu,ubuntu/0,check_reboot,10.41.168.226",
                "",
            ]
        )
        self.assertEqual(expected, self.mock_enrichment_file.read_text())

        # The following simulates `juju config nrpe nagios_host_context="context-1"`
        self.harness.update_relation_data(
            rel_id, "nrpe/0", {**unit_data, **{"target-id": "context-1-ubuntu-0"}}
        )

        expected = "\n".join(
            [
                "composite_key,juju_application,juju_unit,command,ipaddr",
                "10.41.168.226_check_conntrack,context-1-ubuntu,context-1-ubuntu/0,check_conntrack,10.41.168.226",
                "10.41.168.226_check_systemd_scopes,context-1-ubuntu,context-1-ubuntu/0,check_systemd_scopes,10.41.168.226",
                "10.41.168.226_check_reboot,context-1-ubuntu,context-1-ubuntu/0,check_reboot,10.41.168.226",
                "",
            ]
        )
        self.assertEqual(expected, self.mock_enrichment_file.read_text())
