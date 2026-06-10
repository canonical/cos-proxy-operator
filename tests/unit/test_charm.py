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
import shutil
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from ops.model import ActiveStatus, BlockedStatus
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


EXPECTED_VECTOR_SCRAPE = {
    "job_name": "juju_testmodel_ae3c0b1_cos-proxy_prometheus_scrape",
    "static_configs": [
        {
            "targets": [f"{socket.gethostbyname(socket.getfqdn())}:9090"],
            "labels": {
                "juju_model": "testmodel",
                "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                "juju_application": "cos-proxy",
                "juju_unit": "cos-proxy/0",
                "host": socket.gethostbyname(socket.getfqdn()),
                "dns_name": socket.getfqdn(),
            },
        }
    ],
    "relabel_configs": [
        {
            "source_labels": ["juju_model", "juju_model_uuid", "juju_application", "juju_unit"],
            "separator": "_",
            "target_label": "instance",
            "regex": "(.*)",
        }
    ],
}

BUNDLED_RULES = [
    {
        "name": "testmodel_ae3c0b14_cos_proxy_vector_restarted_rules",
        "rules": [
            {
                "alert": "VectorRestarted",
                "expr": "vector_uptime_seconds < (vector_uptime_seconds offset 5m)",
                "for": "0m",
                "labels": {
                    "severity": "info",
                    "juju_model": "testmodel",
                    "juju_model_uuid": "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f",
                    "juju_application": "cos-proxy",
                    "juju_charm": "cos-proxy",
                },
                "annotations": {
                    "summary": "Vector restarted (instance {{ $labels.instance }})",
                    "description": "Vector has just been restarted, less than one minute ago on {{ $labels.instance }}. Telemetry loss may have occurred.\nVALUE = {{ $value }}\nLABELS = {{ $labels }}\n",
                },
            }
        ],
    }
]


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
        self.harness.evaluate_status()
        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus(
                "Missing ['cos-agent']|['downstream-prometheus-scrape'] for prometheus-target"
            ),
        )

    def test_no_incoming_relations_blocks(self):
        self.harness.set_leader(True)
        self.harness.evaluate_status()
        self.assertEqual(
            self.harness.model.unit.status, BlockedStatus("Add at least one incoming relation")
        )

        downstream_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(downstream_rel_id, "cos-prometheus/0")

        downstream_rel_id = self.harness.add_relation(
            "downstream-grafana-dashboard", "cos-grafana"
        )
        self.harness.add_relation_unit(downstream_rel_id, "cos-grafana/0")
        self.harness.evaluate_status()
        self.assertEqual(
            self.harness.model.unit.status, BlockedStatus("Add at least one incoming relation")
        )

    def test_dashboards_without_grafana_relations_blocks(self):
        self.harness.set_leader(True)

        downstream_rel_id = self.harness.add_relation("dashboards", "target-app")
        self.harness.add_relation_unit(downstream_rel_id, "target-app/0")
        self.harness.evaluate_status()
        self.assertEqual(
            self.harness.model.unit.status,
            BlockedStatus("Missing ['cos-agent']|['downstream-grafana-dashboard'] for dashboards"),
        )

    @patch.object(COSProxyCharm, "_setup_nrpe_exporter")
    @patch.object(COSProxyCharm, "_start_vector")
    def test_has_outgoing_dashboard_relation_without_incoming(self, *_unused):
        """Assert charm is not Blocked when we have a valid subset of the supported relations.

        This tests that if we have a cos-agent, which can consume a dashboard, but no incoming
        dashboards, that the charm does not block.
        """
        self.harness.set_leader(True)

        # WHEN we have a downstream relation that can process dashboards
        rel_id = self.harness.add_relation("downstream-grafana-dashboard", "grafana")
        self.harness.add_relation_unit(rel_id, "grafana/0")

        # AND we have some other relation pair in place (e.g. a prometheus target)
        rel_id = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id, "nrpe/0")
        rel_id = self.harness.add_relation("cos-agent", "cos-agent")
        self.harness.add_relation_unit(rel_id, "cos-agent/0")

        # AND we have no dashboard relation
        # THEN the charm should not be blocked
        self.harness.evaluate_status()
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    @patch.object(COSProxyCharm, "_setup_nrpe_exporter")
    @patch.object(COSProxyCharm, "_start_vector")
    @patch.object(COSProxyCharm, "_modify_enrichment_file")
    def test_has_vector_scrape_job_when_nrpe_relation_is_present(self, *_unused):
        self.harness.set_leader(True)

        rel_id = self.harness.add_relation("monitors", "nrpe")
        self.harness.add_relation_unit(rel_id, "nrpe/0")

        # WHEN we have a downstream relation that can process dashboards
        prometheus_rel_id = self.harness.add_relation(
            "downstream-prometheus-scrape", "cos-prometheus"
        )
        self.harness.add_relation_unit(prometheus_rel_id, "cos-prometheus/0")

        prometheus_rel_data = self.harness.get_relation_data(
            prometheus_rel_id, self.harness.model.app.name
        )

        scrape_jobs = json.loads(prometheus_rel_data.get("scrape_jobs", "[]"))
        self.assertEqual(scrape_jobs, [EXPECTED_VECTOR_SCRAPE])

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

        generic_groups = [g for g in groups if "HostHealth" in g["name"]]
        non_generic_groups = [g for g in groups if "HostHealth" not in g["name"]]
        self.assertTrue(len(generic_groups) >= 1)
        self.assertCountEqual(non_generic_groups, BUNDLED_RULES + expected_groups)

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

        generic_groups = [g for g in groups if "HostHealth" in g["name"]]
        non_generic_groups = [g for g in groups if "HostHealth" not in g["name"]]
        self.assertTrue(len(generic_groups) >= 1)
        self.assertCountEqual(non_generic_groups, BUNDLED_RULES + expected_groups)

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

        generic_groups = [g for g in groups if "HostHealth" in g["name"]]
        non_generic_groups = [g for g in groups if "HostHealth" not in g["name"]]
        self.assertTrue(len(generic_groups) >= 1)
        self.assertCountEqual(non_generic_groups, BUNDLED_RULES + expected_groups)

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
        non_generic = [g for g in groups if "HostHealth" not in g["name"]]
        self.assertEqual(len(non_generic), len(BUNDLED_RULES) + 2)

        self.harness.remove_relation_unit(alert_rules_rel_id_2, "rules-app-2/0")
        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
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

        generic_groups = [g for g in groups if "HostHealth" in g["name"]]
        non_generic_groups = [g for g in groups if "HostHealth" not in g["name"]]
        self.assertTrue(len(generic_groups) >= 1)
        self.assertCountEqual(non_generic_groups, BUNDLED_RULES + expected_groups)

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
        non_generic = [g for g in groups if "HostHealth" not in g["name"]]
        self.assertEqual(len(non_generic), len(BUNDLED_RULES) + 1)

        self.harness.remove_relation_unit(alert_rules_rel_id, "rules-app/1")

        alert_rules = json.loads(prometheus_rel_data.get("alert_rules", "{}"))
        groups = alert_rules.get("groups", [])
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

        generic_groups = [g for g in groups if "HostHealth" in g["name"]]
        non_generic_groups = [g for g in groups if "HostHealth" not in g["name"]]
        self.assertTrue(len(generic_groups) >= 1)
        self.assertCountEqual(non_generic_groups, BUNDLED_RULES + expected_groups)

    @patch.object(COSProxyCharm, "_create_dashboard_files")
    def test_dashboard_are_forwarded(self, mock_create_dashboard_files):
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

    @patch.object(COSProxyCharm, "_create_dashboard_files")
    def test_multiple_dashboards_are_forwarded(self, mock_create_dashboard_files):
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

    @patch.object(COSProxyCharm, "_create_dashboard_files")
    def test_dashboards_are_converted(self, mock_create_dashboard_files):
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


class TestCreateDashboardFiles(unittest.TestCase):
    """Tests for _create_dashboard_files and _delete_existing_dashboard_files."""

    def setUp(self):
        self.harness = Harness(COSProxyCharm)
        self.harness.set_model_info(name="testmodel", uuid="ae3c0b14-9c3a-4262-b560-7a6ad7d3642f")
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)

    def test_request_format_reads_from_remote_unit(self):
        """request_* keys in the remote unit databag produce a file each."""
        dashboard_content = {"title": "My Dashboard", "panels": []}
        rel_id = self.harness.add_relation("dashboards", "some-app")
        self.harness.add_relation_unit(rel_id, "some-app/0")
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                rel_id,
                "some-app/0",
                {"request_abc123": json.dumps({"dashboard": dashboard_content})},
            )

        self.harness.charm._create_dashboard_files(self.tmpdir)

        files = list(Path(self.tmpdir).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "request_request_abc123.json")
        with open(files[0]) as f:
            self.assertEqual(json.load(f), dashboard_content)

    def test_own_unit_data_is_ignored(self):
        dashboard_content = {"title": "My Dashboard", "panels": []}
        rel_id = self.harness.add_relation("dashboards", "some-app")
        self.harness.add_relation_unit(rel_id, "some-app/0")
        # Write request_* into cos-proxy's own unit bag, not the remote unit's bag.
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                rel_id,
                "cos-proxy/0",
                {"request_abc123": json.dumps({"dashboard": dashboard_content})},
            )

        self.harness.charm._create_dashboard_files(self.tmpdir)

        files = list(Path(self.tmpdir).glob("*.json"))
        self.assertEqual(files, [], "no files should be created from own-unit data")

    def test_direct_format_creates_file_from_remote_unit(self):
        dashboard_content = {"title": "RabbitMQ-Overview", "uid": "Kn5xm-gZk"}
        rel_id = self.harness.add_relation("dashboards", "rabbitmq-server")
        self.harness.add_relation_unit(rel_id, "rabbitmq-server/0")
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                rel_id,
                "rabbitmq-server/0",
                {
                    "dashboard": json.dumps(dashboard_content),
                    "name": "RabbitMQ-Overview",
                },
            )

        self.harness.charm._create_dashboard_files(self.tmpdir)

        files = list(Path(self.tmpdir).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "dashboard_RabbitMQ-Overview.json")
        with open(files[0]) as f:
            self.assertEqual(json.load(f), dashboard_content)

    def test_direct_format_name_is_sanitised(self):
        """Spaces and slashes in the dashboard name are replaced with underscores."""
        rel_id = self.harness.add_relation("dashboards", "some-app")
        self.harness.add_relation_unit(rel_id, "some-app/0")
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                rel_id,
                "some-app/0",
                {
                    "dashboard": json.dumps({"title": "My Cool Dashboard"}),
                    "name": "My Cool/Dashboard Name",
                },
            )

        self.harness.charm._create_dashboard_files(self.tmpdir)

        files = list(Path(self.tmpdir).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "dashboard_My_Cool_Dashboard_Name.json")

    def test_dashboard_key_without_name_key_is_ignored(self):
        """A remote unit that provides a 'dashboard' key but no 'name' key is skipped."""
        rel_id = self.harness.add_relation("dashboards", "some-app")
        self.harness.add_relation_unit(rel_id, "some-app/0")
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                rel_id,
                "some-app/0",
                {"dashboard": json.dumps({"title": "Orphan Dashboard"})},
            )

        self.harness.charm._create_dashboard_files(self.tmpdir)

        files = list(Path(self.tmpdir).glob("*.json"))
        self.assertEqual(files, [])

    def test_unrelated_keys_produce_no_files(self):
        """Remote unit data with neither request_* nor dashboard+name keys creates nothing."""
        rel_id = self.harness.add_relation("dashboards", "some-app")
        self.harness.add_relation_unit(rel_id, "some-app/0")
        with self.harness.hooks_disabled():
            self.harness.update_relation_data(
                rel_id, "some-app/0", {"egress-subnets": "10.0.0.1/32"}
            )

        self.harness.charm._create_dashboard_files(self.tmpdir)

        files = list(Path(self.tmpdir).glob("*.json"))
        self.assertEqual(files, [])

    def test_delete_removes_request_and_dashboard_files(self):
        """_delete_existing_dashboard_files removes both request_* and dashboard_* JSON files."""
        Path(self.tmpdir, "request_request_abc.json").write_text("{}")
        Path(self.tmpdir, "dashboard_RabbitMQ-Overview.json").write_text("{}")
        Path(self.tmpdir, "notes.txt").write_text("keep me")

        self.harness.charm._delete_existing_dashboard_files(self.tmpdir)

        remaining = [p.name for p in Path(self.tmpdir).iterdir()]
        self.assertEqual(remaining, ["notes.txt"])

    def test_delete_on_missing_directory_does_not_raise(self):
        """_delete_existing_dashboard_files is a no-op when the directory does not exist."""
        nonexistent = str(Path(self.tmpdir) / "nonexistent")
        # Should not raise.
        self.harness.charm._delete_existing_dashboard_files(nonexistent)


class TestSanitizeDatasources(unittest.TestCase):
    """Unit tests for COSProxyCharm._sanitize_datasources."""

    # Convenience alias so tests don't depend on a full harness.
    _fn = staticmethod(COSProxyCharm._sanitize_datasources)

    def test_prometheus_input_replaced_with_prometheusds(self):
        dashboard = {
            "__inputs": [
                {"name": "DS_PROMETHEUS", "type": "datasource", "pluginId": "prometheus"}
            ],
            "panels": [{"datasource": "${DS_PROMETHEUS}"}],
        }
        result = self._fn(dashboard)
        self.assertEqual(result["panels"][0]["datasource"], "${prometheusds}")

    def test_loki_input_replaced_with_lokids(self):
        dashboard = {
            "__inputs": [
                {"name": "DS_LOKI", "type": "datasource", "pluginId": "loki"}
            ],
            "panels": [{"datasource": "${DS_LOKI}"}],
        }
        result = self._fn(dashboard)
        self.assertEqual(result["panels"][0]["datasource"], "${lokids}")

    def test_template_variable_datasource_field_is_also_replaced(self):
        """The datasource field inside template variable definitions must be updated too.

        This is the exact scenario that caused the 'Datasource ${DS_PROMETHEUS} was not
        found' error: the COS pipeline replaced panel datasource refs but missed the
        datasource field inside query-type template variables.
        """
        dashboard = {
            "__inputs": [
                {"name": "DS_PROMETHEUS", "type": "datasource", "pluginId": "prometheus"}
            ],
            "panels": [{"datasource": "${DS_PROMETHEUS}"}],
            "templating": {
                "list": [
                    {
                        "name": "namespace",
                        "type": "query",
                        "datasource": "${DS_PROMETHEUS}",
                        "query": "label_values(up, namespace)",
                    }
                ]
            },
        }
        result = self._fn(dashboard)
        self.assertEqual(result["panels"][0]["datasource"], "${prometheusds}")
        self.assertEqual(
            result["templating"]["list"][0]["datasource"], "${prometheusds}"
        )

    def test_inputs_array_is_removed(self):
        dashboard = {
            "__inputs": [
                {"name": "DS_PROMETHEUS", "type": "datasource", "pluginId": "prometheus"}
            ],
            "title": "My Dashboard",
        }
        result = self._fn(dashboard)
        self.assertNotIn("__inputs", result)

    def test_requires_array_is_removed(self):
        dashboard = {
            "__requires": [{"type": "grafana", "id": "grafana", "version": "7.0.0"}],
            "title": "My Dashboard",
        }
        result = self._fn(dashboard)
        self.assertNotIn("__requires", result)

    def test_unknown_plugin_is_left_unchanged(self):
        """Datasource inputs with unknown plugin types are not substituted."""
        dashboard = {
            "__inputs": [
                {"name": "DS_ELASTIC", "type": "datasource", "pluginId": "elasticsearch"}
            ],
            "panels": [{"datasource": "${DS_ELASTIC}"}],
        }
        result = self._fn(dashboard)
        self.assertEqual(result["panels"][0]["datasource"], "${DS_ELASTIC}")

    def test_no_inputs_returns_dashboard_unchanged_except_requires(self):
        dashboard = {"title": "Plain", "panels": [{"datasource": "${prometheusds}"}]}
        result = self._fn(dashboard)
        self.assertEqual(result, {"title": "Plain", "panels": [{"datasource": "${prometheusds}"}]})

    def test_non_datasource_input_type_is_ignored(self):
        dashboard = {
            "__inputs": [{"name": "SOME_CONST", "type": "constant", "value": "42"}],
            "panels": [{"title": "Panel"}],
        }
        result = self._fn(dashboard)
        self.assertNotIn("__inputs", result)
        self.assertEqual(result["panels"], [{"title": "Panel"}])
