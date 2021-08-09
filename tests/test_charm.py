# Copyright 2021 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import json
import unittest
from unittest.mock import patch

from charm import LMAProxyCharm
from ops.model import ActiveStatus, BlockedStatus
from ops.testing import Harness


class LMAProxyCharmTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(LMAProxyCharm)
        self.harness.set_model_info(
            name="testmodel",
            uuid="1234567890"
        )
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_prometheus_target_relation_without_upstream(self):
        self.harness.set_leader(True)

        rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(rel_id, "target-app/0")
        self.harness.update_relation_data(
            rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        self.harness.add_relation_unit(rel_id, "target-app/1")
        self.harness.update_relation_data(
            rel_id,
            "target-app/1",
            {
                "hostname": "scrape_target_1",
                "port": "1234",
            },
        )

        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing needed downstream relations: downstream-prometheus-scrape")
        )

    @patch("ops.testing._TestingModelBackend.network_get")
    def test_prometheus_target_relation_with_downstream_added_first(self, mock_net_get):
        self.harness.set_leader(True)

        bind_address = "192.0.0.1"
        fake_network = {
            "bind-addresses": [
                {
                    "interface-name": "eth0",
                    "addresses": [
                        {"hostname": "prometheus-tester-0", "value": bind_address}
                    ],
                }
            ]
        }
        mock_net_get.return_value = fake_network

        downstream_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "lma-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id, "lma-prometheus/0")

        rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(rel_id, "target-app/0")
        self.harness.update_relation_data(
            rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        self.harness.add_relation_unit(rel_id, "target-app/1")
        self.harness.update_relation_data(
            rel_id,
            "target-app/1",
            {
                "hostname": "scrape_target_1",
                "port": "1234",
            },
        )

        self.assertEqual(len(self.harness.charm._stored.scrape_jobs), 1)

        scrape_job = self.harness.charm._stored.scrape_jobs[0]

        self.assertEqual(
            scrape_job["job_name"],
            "juju_testmodel_1234567_target-app_prometheus_scrape"
        )

        self.assertEqual(len(scrape_job["static_configs"]), 2)
        self.assertIn({
            "targets": ["scrape_target_0:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/0",
                "host": "scrape_target_0",
            }
        }, scrape_job["static_configs"])
        self.assertIn({
            "targets": ["scrape_target_1:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/1",
                "host": "scrape_target_1",
            }
        }, scrape_job["static_configs"])

        upstream_rel_data = self.harness.get_relation_data(
            downstream_rel_id, self.harness.model.app.name
        )

        self.assertDictEqual({
            "model": "testmodel",
            "model_uuid": "1234567890",
            "application": "lma-proxy",
        }, json.loads(upstream_rel_data["scrape_metadata"]))

        self.assertEqual(
            upstream_rel_data["alert_rules"],
            json.dumps({
                "groups": []
            })
        )

        scrape_jobs = json.loads(upstream_rel_data["scrape_jobs"])

        self.assertEqual(len(scrape_jobs), 1)

        scrape_job = scrape_jobs[0]

        self.assertEqual(
            scrape_job["job_name"],
            "juju_testmodel_1234567_target-app_prometheus_scrape"
        )

        static_configs = scrape_job["static_configs"]

        self.assertEqual(len(static_configs), 2)

        self.assertIn({
            "targets": ["scrape_target_0:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/0",
                "host": "scrape_target_0",
            }
        }, static_configs)

        self.assertIn({
            "targets": ["scrape_target_1:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/1",
                "host": "scrape_target_1",
            }
        }, static_configs)

        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    @patch("ops.testing._TestingModelBackend.network_get")
    def test_prometheus_target_relation_with_downstream_added_second(self, mock_net_get):
        self.harness.set_leader(True)

        bind_address = "192.0.0.1"
        fake_network = {
            "bind-addresses": [
                {
                    "interface-name": "eth0",
                    "addresses": [
                        {"hostname": "prometheus-tester-0", "value": bind_address}
                    ],
                }
            ]
        }
        mock_net_get.return_value = fake_network

        rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(rel_id, "target-app/0")
        self.harness.update_relation_data(
            rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        self.harness.add_relation_unit(rel_id, "target-app/1")
        self.harness.update_relation_data(
            rel_id,
            "target-app/1",
            {
                "hostname": "scrape_target_1",
                "port": "1234",
            },
        )

        downstream_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "lma-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id, "lma-prometheus/0")

        self.assertEqual(len(self.harness.charm._stored.scrape_jobs), 1)

        scrape_job = self.harness.charm._stored.scrape_jobs[0]

        self.assertEqual(
            scrape_job["job_name"],
            "juju_testmodel_1234567_target-app_prometheus_scrape"
        )

        self.assertEqual(len(scrape_job["static_configs"]), 2)
        self.assertIn({
            "targets": ["scrape_target_0:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/0",
                "host": "scrape_target_0",
            }
        }, scrape_job["static_configs"])
        self.assertIn({
            "targets": ["scrape_target_1:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/1",
                "host": "scrape_target_1",
            }
        }, scrape_job["static_configs"])

        upstream_rel_data = self.harness.get_relation_data(
            downstream_rel_id, self.harness.model.app.name
        )

        self.assertDictEqual({
            "model": "testmodel",
            "model_uuid": "1234567890",
            "application": "lma-proxy",
        }, json.loads(upstream_rel_data["scrape_metadata"]))

        self.assertEqual(
            upstream_rel_data["alert_rules"],
            json.dumps({
                "groups": []
            })
        )

        scrape_jobs = json.loads(upstream_rel_data["scrape_jobs"])

        self.assertEqual(len(scrape_jobs), 1)

        scrape_job = scrape_jobs[0]

        self.assertEqual(
            scrape_job["job_name"],
            "juju_testmodel_1234567_target-app_prometheus_scrape"
        )

        static_configs = scrape_job["static_configs"]

        self.assertEqual(len(static_configs), 2)

        self.assertIn({
            "targets": ["scrape_target_0:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/0",
                "host": "scrape_target_0",
            }
        }, static_configs)

        self.assertIn({
            "targets": ["scrape_target_1:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/1",
                "host": "scrape_target_1",
            }
        }, static_configs)

        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    @patch("ops.testing._TestingModelBackend.network_get")
    def test_prometheus_target_relation_with_downstream_deleted(self, mock_net_get):
        self.harness.set_leader(True)

        bind_address = "192.0.0.1"
        fake_network = {
            "bind-addresses": [
                {
                    "interface-name": "eth0",
                    "addresses": [
                        {"hostname": "prometheus-tester-0", "value": bind_address}
                    ],
                }
            ]
        }
        mock_net_get.return_value = fake_network

        downstream_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "lma-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id, "lma-prometheus/0")

        rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(rel_id, "target-app/0")
        self.harness.update_relation_data(
            rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        self.assertEqual(len(self.harness.charm._stored.scrape_jobs), 1)

        self.harness.remove_relation_unit(downstream_rel_id, "lma-prometheus/0")
        self.harness.remove_relation(downstream_rel_id)

        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing needed downstream relations: downstream-prometheus-scrape")
        )

    @patch("ops.testing._TestingModelBackend.network_get")
    def test_prometheus_target_relation_with_all_relations_deleted(self, mock_net_get):
        self.harness.set_leader(True)

        bind_address = "192.0.0.1"
        fake_network = {
            "bind-addresses": [
                {
                    "interface-name": "eth0",
                    "addresses": [
                        {"hostname": "prometheus-tester-0", "value": bind_address}
                    ],
                }
            ]
        }
        mock_net_get.return_value = fake_network

        downstream_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "lma-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id, "lma-prometheus/0")

        rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(rel_id, "target-app/0")
        self.harness.update_relation_data(
            rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        self.assertEqual(len(self.harness.charm._stored.scrape_jobs), 1)

        self.harness.remove_relation_unit(downstream_rel_id, "lma-prometheus/0")
        self.harness.remove_relation(downstream_rel_id)

        self.harness.remove_relation_unit(rel_id, "target-app/0")
        self.harness.remove_relation(rel_id)

        self.assertEqual(len(self.harness.charm._stored.scrape_jobs), 0)
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    @patch("ops.testing._TestingModelBackend.network_get")
    def test_prometheus_target_relation_with_one_downstream_relations_deleted(self, mock_net_get):
        self.harness.set_leader(True)

        bind_address = "192.0.0.1"
        fake_network = {
            "bind-addresses": [
                {
                    "interface-name": "eth0",
                    "addresses": [
                        {"hostname": "prometheus-tester-0", "value": bind_address}
                    ],
                }
            ]
        }
        mock_net_get.return_value = fake_network

        downstream_rel_id_1 = self.harness.add_relation(
            "downstream-prometheus-scrape", "lma-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id_1, "lma-prometheus-1/0")

        downstream_rel_id_2 = self.harness.add_relation(
            "downstream-prometheus-scrape", "lma-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id_2, "lma-prometheus-2/0")

        rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(rel_id, "target-app/0")
        self.harness.update_relation_data(
            rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        self.assertEqual(len(self.harness.charm._stored.scrape_jobs), 1)

        self.harness.remove_relation_unit(downstream_rel_id_1, "lma-prometheus-1/0")
        self.harness.remove_relation(downstream_rel_id_1)

        self.harness.remove_relation_unit(rel_id, "target-app/0")
        self.harness.remove_relation(rel_id)

        self.assertEqual(len(self.harness.charm._stored.scrape_jobs), 0)
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())
