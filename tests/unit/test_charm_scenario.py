import json
from unittest.mock import MagicMock, patch

import scenario

from charm import COSProxyCharm
from metrics_endpoint_aggregator import _dedupe_list

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


@patch("builtins.open", new_callable=MagicMock)
def test_scrape_jobs_are_forwarded_on_adding_prometheus_then_targets(mock_open):
    # Arrange
    prometheus_scrape_relation = scenario.Relation(
        "downstream-prometheus-scrape",
        remote_app_name="cos-prometheus",
        remote_units_data={
            0: {},
        },
    )
    prometheus_target_relation = scenario.Relation(
        "prometheus-target",
        remote_app_name="target-app",
        remote_units_data={
            0: {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        },
    )

    model_name = "testmodel"
    model_uuid = "ae3c0b14-9c3a-4262-b560-7a6ad7d3642f"
    model = scenario.Model(name=model_name, uuid=model_uuid)

    ctx = scenario.Context(COSProxyCharm)
    state_in = scenario.State(
        leader=True,
        relations=[prometheus_scrape_relation, prometheus_target_relation],
        model=model,
        config={"forward_alert_rules": True},
    )

    expected_jobs = [
        {
            "job_name": f"juju_{model_name}_{model_uuid[:7]}_target-app_prometheus_scrape",
            "static_configs": [
                {
                    "targets": ["scrape_target_0:1234"],
                    "labels": {
                        "juju_model": model_name,
                        "juju_model_uuid": model_uuid,
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

    # Act
    out = ctx.run(ctx.on.relation_changed(relation=prometheus_target_relation), state=state_in)
    relation = next(iter(out.relations))
    # Assert
    actual_jobs = json.loads(relation.local_app_data.get("scrape_jobs", "[]"))
    assert actual_jobs == expected_jobs


@patch.object(COSProxyCharm, "_modify_enrichment_file", lambda *a, **kw: None)
def test_deduplicated_alert_rules():
    # GIVEN Cos-proxy is receiving alert rules from multiple sources:
    # Dynamic-rules, prometheus-rules, generic-rules, src/prometheus_alert_rules
    ctx = scenario.Context(COSProxyCharm)
    monitors = scenario.Relation(
        endpoint="monitors",
        remote_units_data={
            0: {
                "monitors": """{
                "monitors": {
                    "remote": {
                        "nrpe": {
                            "check_conntrack": "check_conntrack",
                            "check_systemd_scopes": "check_systemd_scopes",
                            "check_reboot": "check_reboot"
                        }
                    }
                },
                "version": "0.3"
            }"""
            }
        },
    )
    prometheus_rules = scenario.Relation(
        endpoint="prometheus-rules",
        remote_units_data={
            0: {
                "groups": "- alert: RabbitMQ_split_brain\n"
                "  expr: count(count(rabbitmq_queues) by (job)) > 1\n"
                "  for: 5m\n"
                "  labels:\n"
                "    severity: page\n"
                "    application: rabbitmq-server"
            }
        },
    )
    prometheus_scrape = scenario.Relation(
        "downstream-prometheus-scrape",
        remote_app_name="cos-prometheus",
        remote_units_data={
            0: {},
        },
    )
    state_in = scenario.State(
        leader=True, relations=[monitors, prometheus_scrape, prometheus_rules]
    )

    # WHEN multiple config-changed events trigger the charm
    state_out = ctx.run(ctx.on.config_changed(), state=state_in)
    state_out = ctx.run(ctx.on.config_changed(), state=state_out)

    # THEN these alerts are forwarded to the downstream Prometheus
    prometheus_scrape = next(
        (
            relation
            for relation in state_out.relations
            if relation.endpoint == "downstream-prometheus-scrape"
        ),
        None,
    )
    assert prometheus_scrape
    groups = json.loads(prometheus_scrape.local_app_data["alert_rules"])["groups"]

    # AND there are no duplicate alert rule groups
    # Note: using lib.charms.prometheus_k8s.v0.prometheus_scrape._dedupe_list
    assert groups == _dedupe_list(groups)


@patch.object(COSProxyCharm, "_modify_enrichment_file", lambda *a, **kw: None)
def test_deduplicated_scrape_jobs():
    # GIVEN Cos-proxy is receiving scrape jobs
    ctx = scenario.Context(COSProxyCharm)
    prometheus_target_relation = scenario.Relation(
        "prometheus-target",
        remote_app_name="target-app",
        remote_units_data={
            0: {
                "hostname": "scrape_target_0",
                "port": "1234",
            },
        },
    )
    prometheus_scrape = scenario.Relation(
        "downstream-prometheus-scrape",
        remote_app_name="cos-prometheus",
        remote_units_data={
            0: {},
        },
    )
    state_in = scenario.State(
        leader=True, relations=[prometheus_target_relation, prometheus_scrape]
    )

    # WHEN multiple config-changed events trigger the charm
    state_out = ctx.run(ctx.on.config_changed(), state=state_in)
    state_out = ctx.run(ctx.on.config_changed(), state=state_out)

    # THEN these scrape jobs are forwarded to the downstream Prometheus
    prometheus_scrape = next(
        (
            relation
            for relation in state_out.relations
            if relation.endpoint == "downstream-prometheus-scrape"
        ),
        None,
    )
    assert prometheus_scrape
    scrape_jobs = json.loads(prometheus_scrape.local_app_data["scrape_jobs"])

    # AND there are no duplicate scrape jobs
    # Note: using lib.charms.prometheus_k8s.v0.prometheus_scrape._dedupe_list
    assert scrape_jobs == _dedupe_list(scrape_jobs)
