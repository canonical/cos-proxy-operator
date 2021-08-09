#!/usr/bin/env python3
# Copyright 2021 Canonical
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Hello, Juju example charm.

This charm is a demonstration of a machine charm written using the Charmed
Operator Framework. It deploys a simple Python Flask web application and
implements a relation to the PostgreSQL charm.
"""

import logging

from charms.prometheus_k8s.v0.prometheus import PrometheusConsumer
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

logger = logging.getLogger(__name__)


class LMAProxyCharm(CharmBase):

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(scrape_jobs=[])

        self.framework.observe(
            self.on.prometheus_target_relation_joined,
            self._on_prometheus_target_relation_changed,
        )
        self.framework.observe(
            self.on.prometheus_target_relation_changed,
            self._on_prometheus_target_relation_changed,
        )
        self.framework.observe(
            self.on.prometheus_target_relation_broken,
            self._on_prometheus_target_relation_changed,
        )

        # The PrometheusConsumer takes care of all the handling
        # of the "upstream-prometheus-scrape" relation
        # Note self._stored.scrape_jobs is of type StoredDict
        self.prometheus = PrometheusConsumer(
            self,
            "upstream-prometheus-scrape",
            {"prometheus": ">=2.0"},
            self.on.start,
            jobs=vars(self._stored.scrape_jobs)["_under"],
        )

    def _on_prometheus_target_relation_changed(self, event):
        """Iterate all the existing prometheus_scrape relations
        and regenerate the list of jobs
        """

        self.unit.status = MaintenanceStatus("Updating Prometheus scrape jobs")

        if "prometheus-target" in self.model.relations.keys():
            scrape_jobs = []

            for target_relation in self.model.relations["prometheus-target"]:
                # One static config per unit, as we need to specify
                # the entire Juju topology manually
                juju_model = self.model.name
                juju_model_uuid = self.model.uuid
                juju_application = target_relation.app.name

                scrape_jobs.append(
                    {
                        "job_name": "juju_{}_{}_{}_prometheus_scrape".format(
                            juju_model,
                            juju_model_uuid[:7],
                            juju_application,
                        ),
                        "static_configs": [
                            {
                                "targets": [
                                    "{}:{}".format(
                                        target_relation.data[unit].get("hostname"),
                                        target_relation.data[unit].get("port"),
                                    )
                                ],
                                "labels": {
                                    "juju_model": juju_model,
                                    "juju_model_uuid": juju_model_uuid,
                                    "juju_application": juju_application,
                                    "juju_unit": unit.name,
                                },
                            }
                            for unit in target_relation.units
                            if unit.app is not self.app
                        ],
                    }
                )

            self._stored.scrape_jobs = scrape_jobs
        else:
            self._stored.scrape_jobs = []

        # Set the charm to blocked if there is no upstream to send
        self._check_needed_upstream_exists()

    def _check_needed_upstream_exists(self):
        """Set the charm to blocked if there is no upstream of the
        right type available
        """

        missing_upstreams = []

        if self._stored.scrape_jobs:
            if not self.model.relations["upstream-prometheus-scrape"]:
                missing_upstreams.append("upstream-prometheus-scrape")

        if missing_upstreams:
            self.unit.status = BlockedStatus(
                "Missing needed upstream relations: {}".format(
                    ", ".join(missing_upstreams)
                )
            )
        else:
            self.unit.status = ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    main(LMAProxyCharm)
