import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set
from unittest.mock import patch

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
        self.default_monitors_unit_data = {
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

        self.default_target_unit_data = {
            "egress-subnets": "10.181.49.93/32",
            "hostname": "10.181.49.93",
            "ingress-address": "10.181.49.93",
            "port": "9103",
            "private-address": "10.181.49.93",
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
        self.harness.begin_with_initial_hooks()

    def tearDown(self):
        self.mock_enrichment_file.unlink(missing_ok=True)

    def get_app_names_from_scrape_jobs(self, scrape_jobs: Dict[str, Any]) -> Set[str]:
        app_names = [
            s.get("labels", {}).get("juju_application")
            for c in scrape_jobs
            for s in c.get("static_configs", [])
        ]
        assert all(app_names)
        return set(app_names)

    def get_app_names_from_alert_rules(self, alert_rules: Dict[str, Any]) -> List[str]:
        """Gather the app names found in alert groups and its groups.

        If multiple alert rules exist, only capture the app name once. Whereas, if multiple groups
        contain the same app name in alert rules, then capture those duplicates. E.g. both vector
        and generic alert rules would show cos-proxy as the app, but they are unique alert rules.
        """
        app_names = []
        for g in alert_rules.get("groups", []):
            group_app_names = set()  # To track unique applications in the current group
            for r in g.get("rules", []):
                app_name = r.get("labels", {}).get("nrpe_application") or r.get("labels", {}).get(
                    "juju_application"
                )
                if app_name:
                    group_app_names.add(
                        app_name
                    )  # Add to the group set to avoid duplicates within the group
            app_names.extend(group_app_names)
        return app_names

    def get_jobs_from_app_data(
        self, outgoing_app: str, app_data: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        assert outgoing_app in ["prom", "agent"]
        assert app_data
        if outgoing_app == "agent":
            return json.loads(app_data["config"])["metrics_scrape_jobs"]
        return json.loads(app_data["scrape_jobs"])

    def get_alert_rules_from_app_data(
        self, outgoing_app: str, app_data: Dict[str, str]
    ) -> List[Dict[str, Any]]:
        assert outgoing_app in ["prom", "agent"]
        assert app_data
        if outgoing_app == "agent":
            return json.loads(app_data["config"])["metrics_alert_rules"]
        return json.loads(app_data["alert_rules"])

    def assert_outgoing_data_on_removal(
        self, outgoing_apps: Dict[str, int], nrpe_id: int, target_id: int
    ):
        """Assert that scrape jobs and alert rules in the outgoing relation's app data.

        Starting with multiple incoming relations over different endpoints, sequentially remove
        them. For each stage, assertions are made against the app data for expected scrape jobs
        and alert rules existing in the outgoing relation data bags.
        """
        for app_name, rel_id in outgoing_apps.items():
            app_or_unit = "cos-proxy/0" if app_name == "agent" else "cos-proxy"
            assert app_name in ["prom", "agent"]
            app_data = self.harness.get_relation_data(rel_id, app_or_unit)
            scrape_jobs = self.get_jobs_from_app_data(app_name, app_data)
            alert_rules = self.get_alert_rules_from_app_data(app_name, app_data)
            # THEN cos-agent has the expected scrape jobs
            assert {"cos-proxy", "nrpe", "telegraf"} == self.get_app_names_from_scrape_jobs(
                scrape_jobs
            )
            # AND cos-agent has the expected alert rules
            # NOTE: x1 cos-proxy for vector, x1 cos-proxy for generic alerts
            assert ["cos-proxy", "cos-proxy", "nrpe"] == self.get_app_names_from_alert_rules(
                alert_rules
            )

        # AND WHEN the monitors relation is removed
        self.harness.remove_relation(nrpe_id)
        for app_name, rel_id in outgoing_apps.items():
            app_or_unit = "cos-proxy/0" if app_name == "agent" else "cos-proxy"
            app_data = self.harness.get_relation_data(rel_id, app_or_unit)
            scrape_jobs = self.get_jobs_from_app_data(app_name, app_data)
            alert_rules = self.get_alert_rules_from_app_data(app_name, app_data)
            # TODO: Get the telegraf alerts as well
            # THEN cos-agent has no more nrpe jobs
            assert {"telegraf", "cos-proxy"} == self.get_app_names_from_scrape_jobs(scrape_jobs)
            # AND cos-agent has no more nrpe alert rules
            # NOTE: x1 cos-proxy for vector, x1 cos-proxy for generic alerts
            assert ["cos-proxy", "cos-proxy"] == self.get_app_names_from_alert_rules(alert_rules)

        # AND WHEN the prometheus-target relation is removed
        self.harness.remove_relation(target_id)
        for app_name, rel_id in outgoing_apps.items():
            app_or_unit = "cos-proxy/0" if app_name == "agent" else "cos-proxy"
            app_data = self.harness.get_relation_data(rel_id, app_or_unit)
            scrape_jobs = self.get_jobs_from_app_data(app_name, app_data)
            # THEN cos-agent has no more telegraf jobs
            assert {"cos-proxy"} == self.get_app_names_from_scrape_jobs(scrape_jobs)
            # AND cos-agent has no more telegraf alert rules
            # NOTE: x1 cos-proxy for vector, x1 cos-proxy for generic alerts
            assert ["cos-proxy", "cos-proxy"] == self.get_app_names_from_alert_rules(alert_rules)

    def test_only_prometheus(self):
        # TODO: The first 10 lines are repeated for each test, put this in a conftest or something
        # GIVEN the "downstream-prometheus-scrape", "monitors", and "prometheus-target" relations exist
        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        rel_id_target = self.harness.add_relation("prometheus-target", "telegraf")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")
        self.harness.add_relation_unit(rel_id_nrpe, "nrpe/0")
        self.harness.add_relation_unit(rel_id_target, "telegraf/0")

        # WHEN the incoming relations populate their unit data
        self.harness.update_relation_data(rel_id_nrpe, "nrpe/0", self.default_monitors_unit_data)
        self.harness.update_relation_data(
            rel_id_target, "telegraf/0", self.default_target_unit_data
        )

        self.assert_outgoing_data_on_removal({"prom": rel_id_prom}, rel_id_nrpe, rel_id_target)

    def test_only_cos_agent(self):
        # GIVEN the "cos-agent", "monitors", and "prometheus-target" relations exist
        rel_id_agent = self.harness.add_relation("cos-agent", "agent")
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        rel_id_target = self.harness.add_relation("prometheus-target", "telegraf")
        self.harness.add_relation_unit(rel_id_agent, "agent/0")
        self.harness.add_relation_unit(rel_id_nrpe, "nrpe/0")
        self.harness.add_relation_unit(rel_id_target, "telegraf/0")
        # TODO: I need to add a prometheus-rules relation here as well

        # WHEN the incoming relations populate their unit data
        self.harness.update_relation_data(rel_id_nrpe, "nrpe/0", self.default_monitors_unit_data)
        self.harness.update_relation_data(
            rel_id_target, "telegraf/0", self.default_target_unit_data
        )

        self.assert_outgoing_data_on_removal({"agent": rel_id_agent}, rel_id_nrpe, rel_id_target)

    def test_cos_agent_with_downstream_prometheus(self):
        # GIVEN the "cos-agent", "downstream-prometheus-scrape", "monitors", and
        # "prometheus-target" relations exist
        rel_id_agent = self.harness.add_relation("cos-agent", "agent")
        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        rel_id_target = self.harness.add_relation("prometheus-target", "telegraf")
        self.harness.add_relation_unit(rel_id_agent, "agent/0")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")
        self.harness.add_relation_unit(rel_id_nrpe, "nrpe/0")
        self.harness.add_relation_unit(rel_id_target, "telegraf/0")

        # WHEN the incoming relations populate their unit data
        self.harness.update_relation_data(rel_id_nrpe, "nrpe/0", self.default_monitors_unit_data)
        self.harness.update_relation_data(
            rel_id_target, "telegraf/0", self.default_target_unit_data
        )

        agent_app_data = self.harness.get_relation_data(rel_id_agent, "cos-proxy/0")
        prom_app_data = self.harness.get_relation_data(rel_id_prom, "cos-proxy")
        cos_agent_config = json.loads(agent_app_data["config"])

        # THEN "cos-agent" scrape jobs are identical to those in the "downstream-prometheus-scrape" relation
        self.assertEqual(
            json.loads(prom_app_data["scrape_jobs"]), cos_agent_config["metrics_scrape_jobs"]
        )

        # AND "cos-agent" alert rules are identical to those in the "downstream-prometheus-scrape" relation
        self.assertEqual(
            json.loads(prom_app_data["alert_rules"]), cos_agent_config["metrics_alert_rules"]
        )

        self.assert_outgoing_data_on_removal(
            {"agent": rel_id_agent, "prom": rel_id_prom}, rel_id_nrpe, rel_id_target
        )
