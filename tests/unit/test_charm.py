#  Copyright 2021 Canonical Ltd.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# Learn more at: https://juju.is/docs/sdk
# Learn more about testing at: https://juju.is/docs/sdk/testing

import base64
import json
import lzma
import unittest
import uuid
from unittest.mock import patch

from ops.model import BlockedStatus
from ops.testing import Harness

from charm import COSProxyCharm

ALERT_RULE_1 = """- alert: CPU_Usage
  expr: cpu_usage_idle{is_container!=\"True\", group=\"promoagents-juju\"} < 10
  for: 5m
  labels:
    override_group_by: host
    severity: page
    cloud: juju
  annotations:
    description: |
      Host {{ $labels.host }} has had <  10% idle cpu for the last 5m
    summary: Host {{ $labels.host }} CPU free is less than 10%
"""
ALERT_RULE_2 = """- alert: DiskFull
  expr: disk_free{is_container!=\"True\", fstype!~\".*tmpfs|squashfs|overlay\"}  <1024
  for: 5m
  labels:
    override_group_by: host
    severity: page
  annotations:
    description: |
      Host {{ $labels.host}} {{ $labels.path }} is full
      summary: Host {{ $labels.host }} {{ $labels.path}} is full
"""
RELABEL_INSTANCE_CONFIG = {
    "source_labels": [
        "juju_model",
        "juju_model_uuid",
        "juju_application",
        "juju_unit",
    ],
    "separator": "_",
    "target_label": "instance",
    "regex": "(.*)",
}

DASHBOARD_DUMMY_DATA_1 = {
    "request_12345678": json.dumps(
        {
            "dashboard": {
                "dashboard": {
                    "__inputs": [
                        {"pluginName": "Prometheus"},
                    ],
                    "templating": {
                        "list": [
                            {"datasource": "Juju data"},
                        ],
                    },
                    "panels": {
                        "data": "some_data_to_hash_across",
                        "legendFormat": "{{ host }} | {{ instance_name }}",
                    },
                },
            },
        }
    )
}

DUMMY_FIXED_1 = {
    "charm": "dashboard-app-1",
    "content": '{"__inputs": [], "templating": {"list": [{"datasource": '
    '"${prometheusds}"}]}, "panels": {"data": '
    '"some_data_to_hash_across", "legendFormat": "{{ host }} | {{ instance_name }}"}}',
    "inject_dropdowns": True,
    "juju_topology": {
        "application": "dashboard-app-1",
        "model": "testmodel",
        "model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
        "unit": "dashboard-app-1/0",
    },
}

DASHBOARD_DUMMY_DATA_2 = {
    "request_87654321": json.dumps(
        {
            "dashboard": {
                "dashboard": {
                    "templating": {
                        "list": [
                            {"name": "host"},
                        ],
                    },
                    "panels": {
                        "legendFormat": "{{ host }} | {{ instance_name }}",
                        "data": "different_enough_to_rehash",
                    },
                },
            },
        }
    )
}

DUMMY_FIXED_2 = {
    "charm": "dashboard-app-2",
    "content": '{"templating": {"list": [{"name": "host"}]}, '
    '"panels": {"legendFormat": "{{ host }} | {{ instance_name }}", '
    '"data": "different_enough_to_rehash"}}',
    "inject_dropdowns": True,
    "juju_topology": {
        "application": "dashboard-app-2",
        "model": "testmodel",
        "model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
        "unit": "dashboard-app-2/0",
    },
}


@patch.object(lzma, "compress", new=lambda x, *args, **kwargs: x)
@patch.object(lzma, "decompress", new=lambda x, *args, **kwargs: x)
@patch.object(uuid, "uuid4", new=lambda: "21838076-1191-4a88-8008-234433115007")
@patch.object(base64, "b64encode", new=lambda x: x)
@patch.object(base64, "b64decode", new=lambda x: x)
class COSProxyCharmTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(COSProxyCharm)
        self.harness.set_model_info(name="testmodel", uuid="ae3c0b14-9c3a-4262-b560-7a6ad7d3642f")
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_scrape_target_relation_without_downstream_prometheus_blocks(self):
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

        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing one of (Prometheus|target|nrpe) relation(s)"),
        )

    def test_prometheus_relation_without_scrape_target_blocks(self):
        self.harness.set_leader(True)

        downstream_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id, "cos-prometheus/0")

        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing one of (Prometheus|target|nrpe) relation(s)"),
        )

    def test_grafana_relation_without_dashboards_blocks(self):
        self.harness.set_leader(True)

        downstream_rel_id = self.harness.add_relation(
            "downstream-grafana-dashboard", "cos-grafana"
        )
        self.harness.add_relation_unit(downstream_rel_id, "cos-grafana/0")

        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing one of (Grafana|dashboard) relation(s)"),
        )

    def test_dashboards_without_grafana_relations_blocks(self):
        self.harness.set_leader(True)

        downstream_rel_id = self.harness.add_relation("dashboards", "target-app")
        self.harness.add_relation_unit(downstream_rel_id, "target-app/0")

        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing one of (Grafana|dashboard) relation(s)"),
        )

    def test_scrape_jobs_are_forwarded_on_adding_prometheus_then_targets(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        target_rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(target_rel_id, "target-app/0")
        self.harness.update_relation_data(
            target_rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )
        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))
        expected_jobs = [
            {
                "job_name": "juju_testmodel_ae3c0b1_target-app_prometheus_scrape",
                "static_configs": [
                    {
                        "targets": ["scrape_target_0:1234"],
                        "labels": {
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "target-app",
                            "juju_unit": "target-app/0",
                            "host": "scrape_target_0",
                            "dns_name": "scrape_target_0",
                        },
                    }
                ],
                "relabel_configs": [RELABEL_INSTANCE_CONFIG],
            }
        ]
        self.assertListEqual(scrape_jobs, expected_jobs)

    def test_scrape_jobs_are_forwarded_on_adding_targets_then_prometheus(self):
        self.harness.set_leader(True)

        target_rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(target_rel_id, "target-app/0")
        self.harness.update_relation_data(
            target_rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )
        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))
        expected_jobs = [
            {
                "job_name": "juju_testmodel_ae3c0b1_target-app_prometheus_scrape",
                "static_configs": [
                    {
                        "targets": ["scrape_target_0:1234"],
                        "labels": {
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "target-app",
                            "juju_unit": "target-app/0",
                            "host": "scrape_target_0",
                            "dns_name": "scrape_target_0",
                        },
                    }
                ],
                "relabel_configs": [RELABEL_INSTANCE_CONFIG],
            }
        ]

        self.assertListEqual(scrape_jobs, expected_jobs)

    def test_alert_rules_are_forwarded_on_adding_prometheus_then_targets(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        alert_rules_rel_id = self.harness.add_relation("prometheus-rules", "rules-app")
        self.harness.add_relation_unit(alert_rules_rel_id, "rules-app/0")
        self.harness.update_relation_data(
            alert_rules_rel_id,
            "rules-app/0",
            {"groups": ALERT_RULE_1},
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )

        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        expected_group = {
            "name": "juju_testmodel_ae3c0b1_rules-app_alert_rules",
            "rules": [
                {
                    "alert": "CPU_Usage",
                    "expr": 'cpu_usage_idle{is_container!="True", group="promoagents-juju"} < 10',
                    "for": "5m",
                    "labels": {
                        "override_group_by": "host",
                        "severity": "page",
                        "cloud": "juju",
                        "juju_model": "testmodel",
                        "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                        "juju_application": "rules-app",
                        "juju_unit": "rules-app/0",
                    },
                    "annotations": {
                        "description": "Host {{ $labels.host }} has had <  10% idle cpu for the last 5m\n",
                        "summary": "Host {{ $labels.host }} CPU free is less than 10%",
                    },
                }
            ],
        }

        self.assertDictEqual(group, expected_group)

    def test_alert_rules_are_forwarded_on_adding_targets_then_prometheus(self):
        self.harness.set_leader(True)

        alert_rules_rel_id = self.harness.add_relation("prometheus-rules", "rules-app")
        self.harness.add_relation_unit(alert_rules_rel_id, "rules-app/0")
        self.harness.update_relation_data(
            alert_rules_rel_id,
            "rules-app/0",
            {"groups": ALERT_RULE_1},
        )

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )

        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        expected_group = {
            "name": "juju_testmodel_ae3c0b1_rules-app_alert_rules",
            "rules": [
                {
                    "alert": "CPU_Usage",
                    "expr": 'cpu_usage_idle{is_container!="True", group="promoagents-juju"} < 10',
                    "for": "5m",
                    "labels": {
                        "override_group_by": "host",
                        "severity": "page",
                        "cloud": "juju",
                        "juju_model": "testmodel",
                        "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                        "juju_application": "rules-app",
                        "juju_unit": "rules-app/0",
                    },
                    "annotations": {
                        "description": "Host {{ $labels.host }} has had <  10% idle cpu for the last 5m\n",
                        "summary": "Host {{ $labels.host }} CPU free is less than 10%",
                    },
                }
            ],
        }
        self.assertDictEqual(group, expected_group)

    def test_multiple_scrape_jobs_are_forwarded(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        target_rel_id_1 = self.harness.add_relation("prometheus-target", "target-app-1")
        self.harness.add_relation_unit(target_rel_id_1, "target-app-1/0")
        self.harness.update_relation_data(
            target_rel_id_1,
            "target-app-1/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        target_rel_id_2 = self.harness.add_relation("prometheus-target", "target-app-2")
        self.harness.add_relation_unit(target_rel_id_2, "target-app-2/0")
        self.harness.update_relation_data(
            target_rel_id_2,
            "target-app-2/0",
            {
                "hostname": "scrape_target_1",
                "port": "5678",
            },
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )
        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))
        self.assertEqual(len(scrape_jobs), 2)

        expected_jobs = [
            {
                "job_name": "juju_testmodel_ae3c0b1_target-app-1_prometheus_scrape",
                "static_configs": [
                    {
                        "targets": ["scrape_target_0:1234"],
                        "labels": {
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "target-app-1",
                            "juju_unit": "target-app-1/0",
                            "host": "scrape_target_0",
                            "dns_name": "scrape_target_0",
                        },
                    }
                ],
                "relabel_configs": [RELABEL_INSTANCE_CONFIG],
            },
            {
                "job_name": "juju_testmodel_ae3c0b1_target-app-2_prometheus_scrape",
                "static_configs": [
                    {
                        "targets": ["scrape_target_1:5678"],
                        "labels": {
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "target-app-2",
                            "juju_unit": "target-app-2/0",
                            "host": "scrape_target_1",
                            "dns_name": "scrape_target_1",
                        },
                    }
                ],
                "relabel_configs": [RELABEL_INSTANCE_CONFIG],
            },
        ]

        self.assertListEqual(scrape_jobs, expected_jobs)

    def test_multiple_alert_rules_are_forwarded(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        alert_rules_rel_id_1 = self.harness.add_relation("prometheus-rules", "rules-app-1")
        self.harness.add_relation_unit(alert_rules_rel_id_1, "rules-app-1/0")
        self.harness.update_relation_data(
            alert_rules_rel_id_1,
            "rules-app-1/0",
            {"groups": ALERT_RULE_1},
        )

        alert_rules_rel_id_2 = self.harness.add_relation("prometheus-rules", "rules-app-2")
        self.harness.add_relation_unit(alert_rules_rel_id_2, "rules-app-2/0")
        self.harness.update_relation_data(
            alert_rules_rel_id_2,
            "rules-app-2/0",
            {"groups": ALERT_RULE_2},
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )

        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
        self.assertEqual(len(groups), 2)
        expected_groups = [
            {
                "name": "juju_testmodel_ae3c0b1_rules-app-1_alert_rules",
                "rules": [
                    {
                        "alert": "CPU_Usage",
                        "expr": 'cpu_usage_idle{is_container!="True", group="promoagents-juju"} < 10',
                        "for": "5m",
                        "labels": {
                            "override_group_by": "host",
                            "severity": "page",
                            "cloud": "juju",
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "rules-app-1",
                            "juju_unit": "rules-app-1/0",
                        },
                        "annotations": {
                            "description": "Host {{ $labels.host }} has had <  10% idle cpu for the last 5m\n",
                            "summary": "Host {{ $labels.host }} CPU free is less than 10%",
                        },
                    }
                ],
            },
            {
                "name": "juju_testmodel_ae3c0b1_rules-app-2_alert_rules",
                "rules": [
                    {
                        "alert": "DiskFull",
                        "expr": 'disk_free{is_container!="True", fstype!~".*tmpfs|squashfs|overlay"}  <1024',
                        "for": "5m",
                        "labels": {
                            "override_group_by": "host",
                            "severity": "page",
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "rules-app-2",
                            "juju_unit": "rules-app-2/0",
                        },
                        "annotations": {
                            "description": "Host {{ $labels.host}} {{ $labels.path }} is full\nsummary: Host {{ $labels.host }} {{ $labels.path}} is full\n"
                        },
                    }
                ],
            },
        ]
        self.assertListEqual(groups, expected_groups)

    def test_scrape_job_removal_differentiates_between_applications(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        target_rel_id_1 = self.harness.add_relation("prometheus-target", "target-app-1")
        self.harness.add_relation_unit(target_rel_id_1, "target-app-1/0")
        self.harness.update_relation_data(
            target_rel_id_1,
            "target-app-1/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        target_rel_id_2 = self.harness.add_relation("prometheus-target", "target-app-2")
        self.harness.add_relation_unit(target_rel_id_2, "target-app-2/0")
        self.harness.update_relation_data(
            target_rel_id_2,
            "target-app-2/0",
            {
                "hostname": "scrape_target_1",
                "port": "5678",
            },
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )
        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))
        self.assertEqual(len(scrape_jobs), 2)

        self.harness.remove_relation_unit(target_rel_id_2, "target-app-2/0")
        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))
        self.assertEqual(len(scrape_jobs), 1)

        expected_jobs = [
            {
                "job_name": "juju_testmodel_ae3c0b1_target-app-1_prometheus_scrape",
                "static_configs": [
                    {
                        "targets": ["scrape_target_0:1234"],
                        "labels": {
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "target-app-1",
                            "juju_unit": "target-app-1/0",
                            "host": "scrape_target_0",
                            "dns_name": "scrape_target_0",
                        },
                    }
                ],
                "relabel_configs": [RELABEL_INSTANCE_CONFIG],
            }
        ]
        self.assertListEqual(scrape_jobs, expected_jobs)

    def test_alert_rules_removal_differentiates_between_applications(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        alert_rules_rel_id_1 = self.harness.add_relation("prometheus-rules", "rules-app-1")
        self.harness.add_relation_unit(alert_rules_rel_id_1, "rules-app-1/0")
        self.harness.update_relation_data(
            alert_rules_rel_id_1,
            "rules-app-1/0",
            {"groups": ALERT_RULE_1},
        )

        alert_rules_rel_id_2 = self.harness.add_relation("prometheus-rules", "rules-app-2")
        self.harness.add_relation_unit(alert_rules_rel_id_2, "rules-app-2/0")
        self.harness.update_relation_data(
            alert_rules_rel_id_2,
            "rules-app-2/0",
            {"groups": ALERT_RULE_2},
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )

        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
        self.assertEqual(len(groups), 2)

        self.harness.remove_relation_unit(alert_rules_rel_id_2, "rules-app-2/0")
        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
        self.assertEqual(len(groups), 1)

        expected_groups = [
            {
                "name": "juju_testmodel_ae3c0b1_rules-app-1_alert_rules",
                "rules": [
                    {
                        "alert": "CPU_Usage",
                        "expr": 'cpu_usage_idle{is_container!="True", group="promoagents-juju"} < 10',
                        "for": "5m",
                        "labels": {
                            "override_group_by": "host",
                            "severity": "page",
                            "cloud": "juju",
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "rules-app-1",
                            "juju_unit": "rules-app-1/0",
                        },
                        "annotations": {
                            "description": "Host {{ $labels.host }} has had <  10% idle cpu for the last 5m\n",
                            "summary": "Host {{ $labels.host }} CPU free is less than 10%",
                        },
                    }
                ],
            },
        ]

        self.assertListEqual(groups, expected_groups)

    def test_removing_scrape_jobs_differentiates_between_units(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        target_rel_id = self.harness.add_relation("prometheus-target", "target-app")
        self.harness.add_relation_unit(target_rel_id, "target-app/0")
        self.harness.update_relation_data(
            target_rel_id,
            "target-app/0",
            {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        )

        self.harness.add_relation_unit(target_rel_id, "target-app/1")
        self.harness.update_relation_data(
            target_rel_id,
            "target-app/1",
            {
                "hostname": "scrape_target_1",
                "port": "5678",
            },
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )
        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))

        self.assertEqual(len(scrape_jobs), 1)
        self.assertEqual(len(scrape_jobs[0].get("static_configs")), 2)

        self.harness.remove_relation_unit(target_rel_id, "target-app/1")
        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))

        self.assertEqual(len(scrape_jobs), 1)
        self.assertEqual(len(scrape_jobs[0].get("static_configs")), 1)

        expected_jobs = [
            {
                "job_name": "juju_testmodel_ae3c0b1_target-app_prometheus_scrape",
                "static_configs": [
                    {
                        "targets": ["scrape_target_0:1234"],
                        "labels": {
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "target-app",
                            "juju_unit": "target-app/0",
                            "host": "scrape_target_0",
                            "dns_name": "scrape_target_0",
                        },
                    }
                ],
                "relabel_configs": [RELABEL_INSTANCE_CONFIG],
            }
        ]
        self.assertListEqual(scrape_jobs, expected_jobs)

    def test_removing_alert_rules_differentiates_between_units(self):
        self.harness.set_leader(True)

        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        alert_rules_rel_id = self.harness.add_relation("prometheus-rules", "rules-app")
        self.harness.add_relation_unit(alert_rules_rel_id, "rules-app/0")
        self.harness.update_relation_data(
            alert_rules_rel_id,
            "rules-app/0",
            {"groups": ALERT_RULE_1},
        )

        self.harness.add_relation_unit(alert_rules_rel_id, "rules-app/1")
        self.harness.update_relation_data(
            alert_rules_rel_id,
            "rules-app/1",
            {"groups": ALERT_RULE_2},
        )

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )

        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
        self.assertEqual(len(groups), 1)

        self.harness.remove_relation_unit(alert_rules_rel_id, "rules-app/1")

        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
        self.assertEqual(len(groups), 1)

        expected_groups = [
            {
                "name": "juju_testmodel_ae3c0b1_rules-app_alert_rules",
                "rules": [
                    {
                        "alert": "CPU_Usage",
                        "expr": 'cpu_usage_idle{is_container!="True", group="promoagents-juju"} < 10',
                        "for": "5m",
                        "labels": {
                            "override_group_by": "host",
                            "severity": "page",
                            "cloud": "juju",
                            "juju_model": "testmodel",
                            "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                            "juju_application": "rules-app",
                            "juju_unit": "rules-app/0",
                        },
                        "annotations": {
                            "description": "Host {{ $labels.host }} has had <  10% idle cpu for the last 5m\n",
                            "summary": "Host {{ $labels.host }} CPU free is less than 10%",
                        },
                    }
                ],
            },
        ]

        self.assertListEqual(groups, expected_groups)

    def test_dashboard_are_forwarded(self):
        self.harness.set_leader(True)

        grafana_rel_id = self.harness.add_relation("downstream-grafana-dashboard", "cos-grafana")
        self.harness.add_relation_unit(grafana_rel_id, "cos-grafana/0")

        target_rel_id = self.harness.add_relation("dashboards", "dashboard-app")
        self.harness.add_relation_unit(target_rel_id, "dashboard-app/0")
        self.harness.update_relation_data(target_rel_id, "dashboard-app/0", DASHBOARD_DUMMY_DATA_1)

        grafana_rel_data = self.harness.get_relation_data(
            grafana_rel_id, self.harness.model.app.name
        )
        dashboards = json.loads(grafana_rel_data.get("dashboards", "{}"))
        self.assertEqual(len(dashboards["templates"]), 1)

    def test_multiple_dashboards_are_forwarded(self):
        self.harness.set_leader(True)

        grafana_rel_id = self.harness.add_relation("downstream-grafana-dashboard", "cos-grafana")
        self.harness.add_relation_unit(grafana_rel_id, "cos-grafana/0")

        target_rel_id_1 = self.harness.add_relation("dashboards", "dashboard-app-1")
        self.harness.add_relation_unit(target_rel_id_1, "dashboard-app-1/0")
        self.harness.update_relation_data(
            target_rel_id_1, "dashboard-app-1/0", DASHBOARD_DUMMY_DATA_1
        )

        target_rel_id_2 = self.harness.add_relation("dashboards", "dashboard-app-2")
        self.harness.add_relation_unit(target_rel_id_2, "dashboard-app-2/0")
        self.harness.update_relation_data(
            target_rel_id_2, "dashboard-app-2/0", DASHBOARD_DUMMY_DATA_2
        )

        grafana_rel_data = self.harness.get_relation_data(
            grafana_rel_id, self.harness.model.app.name
        )
        dashboards = json.loads(grafana_rel_data.get("dashboards", "{}"))
        self.assertEqual(len(dashboards["templates"]), 2)

    def test_dashboards_are_converted(self):
        self.harness.set_leader(True)

        grafana_rel_id = self.harness.add_relation("downstream-grafana-dashboard", "cos-grafana")
        self.harness.add_relation_unit(grafana_rel_id, "cos-grafana/0")

        target_rel_id_1 = self.harness.add_relation("dashboards", "dashboard-app-1")
        self.harness.add_relation_unit(target_rel_id_1, "dashboard-app-1/0")
        self.harness.update_relation_data(
            target_rel_id_1, "dashboard-app-1/0", DASHBOARD_DUMMY_DATA_1
        )

        target_rel_id_2 = self.harness.add_relation("dashboards", "dashboard-app-2")
        self.harness.add_relation_unit(target_rel_id_2, "dashboard-app-2/0")
        self.harness.update_relation_data(
            target_rel_id_2, "dashboard-app-2/0", DASHBOARD_DUMMY_DATA_2
        )

        grafana_rel_data = self.harness.get_relation_data(
            grafana_rel_id, self.harness.model.app.name
        )
        dashboards = json.loads(grafana_rel_data.get("dashboards", "{}"))
        self.assertEqual(len(dashboards["templates"]), 2)
        self.assertEqual(dashboards["templates"]["prog:| {{ ins"], DUMMY_FIXED_1)
        self.assertEqual(dashboards["templates"]["prog:rent_eno"], DUMMY_FIXED_2)
