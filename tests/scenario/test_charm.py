import json

import scenario
from charm import COSProxyCharm

from tests.scenario.helpers import get_charm_meta

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


def test_scrape_jobs_are_forwarded_on_adding_prometheus_then_targets():
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
    model = scenario.Model(model_name, model_uuid)

    ctx = scenario.Context(COSProxyCharm, meta=get_charm_meta())
    state_in = scenario.State(
        leader=True,
        relations=[prometheus_scrape_relation, prometheus_target_relation],
        model=model,
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
    out = ctx.run(prometheus_target_relation.changed_event, state_in)

    # Assert
    actual_jobs = json.loads(out.relations[0].local_app_data.get("scrape_jobs", []))
    assert actual_jobs == expected_jobs
