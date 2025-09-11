import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.model import ActiveStatus
from ops.testing import Harness

from charm import COSProxyCharm

NAGIOS_HOST_CONTEXT = "ubuntu"
POD_NAME = "ubuntu-is-amazing-0"
JUJU_UNIT = "ubuntu-is-amazing/0"
JUJU_APP = "ubuntu-is-amazing"


class TestRelationMonitors(unittest.TestCase):
    def setUp(self):
        self.mock_enrichment_file = Path(tempfile.mktemp())

        # The unit data below were obtained from the output of:
        # juju show-unit \
        #   cos-proxy/0 --format json | jq '."cos-proxy/0"."relation-info"[0]."related-units"."nrpe/0".data'
        self.default_unit_data = {
            "egress-subnets": "10.41.168.226/32",
            "ingress-address": "10.41.168.226",
            "machine_id": "1",
            "model_id": "fe2c9bbb-58ab-40e4-8f70-f27480093fca",
            "monitors": "{'monitors': {'remote': {'nrpe': {'check_conntrack': 'check_conntrack', 'check_systemd_scopes': 'check_systemd_scopes', 'check_reboot': 'check_reboot'}}}, 'version': '0.3'}",
            "private-address": "10.41.168.226",
            "target-address": "10.41.168.226",
            "target-id": f"{NAGIOS_HOST_CONTEXT}-{POD_NAME}",
            "nagios_host_context": NAGIOS_HOST_CONTEXT,
        }

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
        self.harness.add_network("10.41.168.226")
        self.harness.set_model_info(name="mymodel", uuid="fe2c9bbb-58ab-40e4-8f70-f27480093fca")
        self.harness.set_leader(True)
        self.maxDiff = None

    def tearDown(self):
        self.mock_enrichment_file.unlink(missing_ok=True)

    def test_monitors_changed(self):
        # GIVEN a post-startup charm
        self.harness.begin_with_initial_hooks()

        # WHEN a "monitors" relation joins
        rel_id = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id, "nrpe/0")
        self.harness.update_relation_data(rel_id, "nrpe/0", self.default_unit_data)

        # THEN the csv file contains corresponding targets
        expected = "\n".join(
            [
                "composite_key,juju_application,juju_unit,command,ipaddr",
                f"10.41.168.226_check_conntrack,{JUJU_APP},{JUJU_UNIT},check_conntrack,10.41.168.226",
                f"10.41.168.226_check_systemd_scopes,{JUJU_APP},{JUJU_UNIT},check_systemd_scopes,10.41.168.226",
                f"10.41.168.226_check_reboot,{JUJU_APP},{JUJU_UNIT},check_reboot,10.41.168.226",
                "",
            ]
        )
        self.assertEqual(expected, self.mock_enrichment_file.read_text())

        # AND WHEN the relation data updates with a different prefix
        # The following simulates `juju config nrpe nagios_host_context="context-1"`
        self.harness.update_relation_data(
            rel_id,
            "nrpe/0",
            {
                **self.default_unit_data,
                **{"target-id": "context-1-ubuntu-0", "nagios_host_context": "context-1"},
            },
        )

        # THEN the csv file is replaced with targets with the modified prefix
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

    def test_prometheus(self):
        # GIVEN a post-startup charm
        self.harness.begin_with_initial_hooks()

        # WHEN "monitors" and "downstream-prometheus-scrape" relations join
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id_nrpe, "nrpe/0")
        self.harness.update_relation_data(rel_id_nrpe, "nrpe/0", self.default_unit_data)

        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")

        # THEN alert rules are transferred to prometheus over relation data and
        # nagios_host_context is not part of the expr.
        app_data = self.harness.get_relation_data(rel_id_prom, "cos-proxy")
        self.assertIn("alert_rules", app_data)  # pyright: ignore

        groups = json.loads((app_data["alert_rules"]))["groups"]

        for group in groups:
            for rule in group["rules"]:
                if rule["labels"].get("juju_charm") == "cos-proxy":
                    self.assertEqual("cos-proxy", rule["labels"]["juju_application"])
                else:
                    self.assertIn(f"juju_unit='{JUJU_UNIT}'", rule["expr"])
                    self.assertEqual(JUJU_APP, rule["labels"]["juju_application"])
                    self.assertEqual(JUJU_UNIT, rule["labels"]["juju_unit"])

        # AND status is "active"
        self.harness.evaluate_status()
        self.assertIsInstance(
            self.harness.model.unit.status,
            ActiveStatus,
        )
        # AND relabel configs are ok (we are removing nagios_host_context)
        scrape_jobs = json.loads(app_data["scrape_jobs"])
        for job in scrape_jobs:
            relabel_configs = job["relabel_configs"]
            for config in relabel_configs:
                if target_level := config.get("target_label"):
                    if target_level == "juju_application":
                        self.assertEqual(config["replacement"], JUJU_APP)
                    elif target_level == "juju_unit":
                        self.assertEqual(config["replacement"], JUJU_UNIT)

    def test_cos_agent(self):
        # GIVEN cos-agent, monitors, and downstream-prometheus-scrape relations
        rel_id_agent = self.harness.add_relation("cos-agent", "agent")
        self.harness.add_relation_unit(rel_id_agent, "agent/0")
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id_nrpe, "nrpe/0")
        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")

        self.harness.begin_with_initial_hooks()

        # WHEN "monitors" relation populates its unit data
        self.harness.update_relation_data(rel_id_nrpe, "nrpe/0", self.default_unit_data)

        # THEN cos-agent scrape jobs are identical to those in prometheus relations
        app_data_agent = self.harness.get_relation_data(rel_id_agent, "cos-proxy/0")
        app_data_prom = self.harness.get_relation_data(rel_id_prom, "cos-proxy")
        cos_agent_scrape_jobs = json.loads(app_data_agent["config"])["metrics_scrape_jobs"]
        prom_scrape_jobs = json.loads(app_data_prom["scrape_jobs"])
        self.assertEqual(prom_scrape_jobs, cos_agent_scrape_jobs)
