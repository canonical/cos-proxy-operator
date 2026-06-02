import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.model import ActiveStatus, RelationDataContent
from ops.testing import Harness

from charm import COSProxyCharm
from metrics_endpoint_aggregator import MetricsEndpointAggregator

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
            patch("socket.gethostbyaddr", return_value=("localhost", [], ["10.181.49.93"])),
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

    def test_monitors_changed(self):
        # GIVEN a "monitors" relation joins
        rel_id = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id, "nrpe/0")
        self.harness.update_relation_data(rel_id, "nrpe/0", self.default_monitors_unit_data)

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
                **self.default_monitors_unit_data,
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
        # GIVEN the "monitors" and "downstream-prometheus-scrape" relations join
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id_nrpe, "nrpe/0")
        self.harness.update_relation_data(rel_id_nrpe, "nrpe/0", self.default_monitors_unit_data)

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

    def test_monitors_changed_does_not_replay_individual_updates_for_one_unit_change(self):
        # GIVEN a monitors relation with several units and several NRPE checks per unit.
        unit_count = 5
        checks = {
            "check_conntrack": "check_conntrack",
            "check_systemd_scopes": "check_systemd_scopes",
            "check_reboot": "check_reboot",
        }
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")

        def unit_data(unit_id: int) -> dict:
            return {
                **self.default_monitors_unit_data,
                "target-address": f"10.41.168.{unit_id}",
                "target-id": f"{NAGIOS_HOST_CONTEXT}-ubuntu-{unit_id}",
                "monitors": json.dumps(
                    {"monitors": {"remote": {"nrpe": checks}}, "version": "0.3"}
                ),
            }

        for unit_id in range(unit_count - 1):
            self.harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_id}")
            self.harness.update_relation_data(rel_id_nrpe, f"nrpe/{unit_id}", unit_data(unit_id))

        # WHEN one more unit changes, the NRPE provider regenerates from the whole relation.
        with patch.object(
            MetricsEndpointAggregator,
            "set_target_job_data",
            side_effect=AssertionError("batch path should not call per-target update"),
        ), patch.object(
            MetricsEndpointAggregator,
            "set_alert_rule_data",
            side_effect=AssertionError("batch path should not call per-alert update"),
        ):
            self.harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_count - 1}")
            self.harness.update_relation_data(
                rel_id_nrpe, f"nrpe/{unit_count - 1}", unit_data(unit_count - 1)
            )

    def test_monitors_changed_passes_all_generated_targets_and_alerts_as_one_batch(self):
        # GIVEN a monitors relation with several units and several NRPE checks per unit.
        unit_count = 4
        checks = {
            "check_conntrack": "check_conntrack",
            "check_reboot": "check_reboot",
        }
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")

        def unit_data(unit_id: int) -> dict:
            return {
                **self.default_monitors_unit_data,
                "target-address": f"10.41.168.{unit_id}",
                "target-id": f"{NAGIOS_HOST_CONTEXT}-ubuntu-{unit_id}",
                "monitors": json.dumps(
                    {"monitors": {"remote": {"nrpe": checks}}, "version": "0.3"}
                ),
            }

        for unit_id in range(unit_count - 1):
            self.harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_id}")
            self.harness.update_relation_data(rel_id_nrpe, f"nrpe/{unit_id}", unit_data(unit_id))

        batch_calls = []
        original_batch_update = (
            MetricsEndpointAggregator.set_target_job_and_alert_rule_data_batch
        )

        def count_batch_update(aggregator, target_jobs, alert_rules, **kwargs):
            batch_calls.append((target_jobs, alert_rules, kwargs))
            return original_batch_update(aggregator, target_jobs, alert_rules, **kwargs)

        # WHEN one monitors-relation-changed hook runs after another unit appears.
        with patch.object(
            MetricsEndpointAggregator,
            "set_target_job_and_alert_rule_data_batch",
            new=count_batch_update,
        ):
            self.harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_count - 1}")
            self.harness.update_relation_data(
                rel_id_nrpe, f"nrpe/{unit_count - 1}", unit_data(unit_count - 1)
            )

        # THEN the NRPE handler hands the full generated data set to one batch-shaped call.
        # The extra target update is cos-proxy's own vector scrape target.
        self.assertEqual(1, len(batch_calls))
        target_jobs, alert_rules, kwargs = batch_calls[0]
        self.assertEqual(unit_count * len(checks) + 1, len(target_jobs))
        self.assertEqual(unit_count * len(checks), len(alert_rules))
        self.assertEqual([], kwargs["removed_target_jobs"])
        self.assertEqual([], kwargs["removed_alert_rules"])

    def test_monitors_changed_on_non_leader_does_not_build_downstream_batch(self):
        # GIVEN a non-leader unit receives NRPE monitor data.
        self.harness.set_leader(False)
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id_nrpe, "nrpe/0")

        # WHEN monitors-relation-changed runs.
        with patch.object(
            MetricsEndpointAggregator,
            "set_target_job_and_alert_rule_data_batch",
            side_effect=AssertionError("non-leaders should not build or publish downstream data"),
        ):
            self.harness.update_relation_data(rel_id_nrpe, "nrpe/0", self.default_monitors_unit_data)

        # THEN the local enrichment file side effect is preserved.
        self.assertIn("check_conntrack", self.mock_enrichment_file.read_text())

    def test_monitors_departed_passes_removed_targets_and_alerts_as_one_batch(self):
        # GIVEN a monitors relation with two units and several NRPE checks per unit.
        unit_count = 2
        checks = {
            "check_conntrack": "check_conntrack",
            "check_reboot": "check_reboot",
        }
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")

        def unit_data(unit_id: int) -> dict:
            return {
                **self.default_monitors_unit_data,
                "target-address": f"10.41.168.{unit_id}",
                "target-id": f"{NAGIOS_HOST_CONTEXT}-ubuntu-{unit_id}",
                "monitors": json.dumps(
                    {"monitors": {"remote": {"nrpe": checks}}, "version": "0.3"}
                ),
            }

        for unit_id in range(unit_count):
            self.harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_id}")
            self.harness.update_relation_data(rel_id_nrpe, f"nrpe/{unit_id}", unit_data(unit_id))

        batch_calls = []
        downstream_writes = {"scrape_jobs": 0, "alert_rules": 0}
        original_batch_update = (
            MetricsEndpointAggregator.set_target_job_and_alert_rule_data_batch
        )
        original_commit = RelationDataContent._commit

        def count_batch_update(aggregator, target_jobs, alert_rules, **kwargs):
            batch_calls.append((target_jobs, alert_rules, kwargs))
            return original_batch_update(aggregator, target_jobs, alert_rules, **kwargs)

        def count_downstream_write(databag, data):
            for key in downstream_writes:
                if key in data:
                    downstream_writes[key] += 1
            return original_commit(databag, data)

        # WHEN one unit departs, the NRPE provider emits removals and current state together.
        with patch.object(
            MetricsEndpointAggregator,
            "remove_prometheus_jobs",
            side_effect=AssertionError("batch path should not call per-target removal"),
        ), patch.object(
            MetricsEndpointAggregator,
            "remove_alert_rules",
            side_effect=AssertionError("batch path should not call per-alert removal"),
        ), patch.object(
            MetricsEndpointAggregator,
            "set_target_job_and_alert_rule_data_batch",
            new=count_batch_update,
        ), patch.object(
            RelationDataContent,
            "_commit",
            new=count_downstream_write,
        ):
            self.harness.remove_relation_unit(rel_id_nrpe, "nrpe/1")

        # THEN removals are folded into the single batch update.
        self.assertEqual(1, len(batch_calls))
        target_jobs, alert_rules, kwargs = batch_calls[0]
        self.assertEqual(len(checks) + 1, len(target_jobs))
        self.assertEqual(len(checks), len(alert_rules))
        self.assertEqual(len(checks), len(kwargs["removed_target_jobs"]))
        self.assertEqual(len(checks), len(kwargs["removed_alert_rules"]))
        self.assertEqual(1, downstream_writes["scrape_jobs"])
        self.assertEqual(1, downstream_writes["alert_rules"])

    def test_monitors_changed_writes_downstream_data_once_per_batch(self):
        # GIVEN a populated monitors relation and a downstream prometheus relation.
        unit_count = 4
        checks = {
            "check_conntrack": "check_conntrack",
            "check_reboot": "check_reboot",
        }
        rel_id_nrpe = self.harness.add_relation("monitors", "nrpe")
        rel_id_prom = self.harness.add_relation("downstream-prometheus-scrape", "prom")
        self.harness.add_relation_unit(rel_id_prom, "prom/0")

        def unit_data(unit_id: int) -> dict:
            return {
                **self.default_monitors_unit_data,
                "target-address": f"10.41.168.{unit_id}",
                "target-id": f"{NAGIOS_HOST_CONTEXT}-ubuntu-{unit_id}",
                "monitors": json.dumps(
                    {"monitors": {"remote": {"nrpe": checks}}, "version": "0.3"}
                ),
            }

        for unit_id in range(unit_count - 1):
            self.harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_id}")
            self.harness.update_relation_data(rel_id_nrpe, f"nrpe/{unit_id}", unit_data(unit_id))

        downstream_writes = {"scrape_jobs": 0, "alert_rules": 0}
        original_commit = RelationDataContent._commit

        def count_downstream_write(databag, data):
            for key in downstream_writes:
                if key in data:
                    downstream_writes[key] += 1
            return original_commit(databag, data)

        # WHEN one monitors-relation-changed hook runs after another unit appears.
        with patch.object(RelationDataContent, "_commit", new=count_downstream_write):
            self.harness.add_relation_unit(rel_id_nrpe, f"nrpe/{unit_count - 1}")
            self.harness.update_relation_data(
                rel_id_nrpe, f"nrpe/{unit_count - 1}", unit_data(unit_count - 1)
            )

        # THEN the batch implementation updates both downstream keys once.
        self.assertEqual(1, downstream_writes["scrape_jobs"])
        self.assertEqual(1, downstream_writes["alert_rules"])
        downstream_data = self.harness.get_relation_data(rel_id_prom, "cos-proxy")
        alert_rules = json.loads(downstream_data["alert_rules"])
        generated_rules = [
            rule
            for group in alert_rules["groups"]
            for rule in group["rules"]
            if "juju_unit" in rule.get("labels", {})
        ]
        self.assertEqual(unit_count * len(checks), len(generated_rules))
