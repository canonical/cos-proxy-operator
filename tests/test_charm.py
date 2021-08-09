# Copyright 2021 Canonical
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest

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
                "juju_unit": "target-app/0"
            }
        }, scrape_job["static_configs"])
        self.assertIn({
            "targets": ["scrape_target_1:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/1"
            }
        }, scrape_job["static_configs"])

        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing needed upstream relations: upstream-prometheus-scrape")
        )

    def test_prometheus_target_relation_with_upstream(self):
        self.harness.add_relation("upstream-prometheus-scrape", "lma-prometheus")

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
                "juju_unit": "target-app/0"
            }
        }, scrape_job["static_configs"])
        self.assertIn({
            "targets": ["scrape_target_1:1234"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "1234567890",
                "juju_application": "target-app",
                "juju_unit": "target-app/1"
            }
        }, scrape_job["static_configs"])

        self.assertEqual(self.harness.model.unit.status, ActiveStatus())
