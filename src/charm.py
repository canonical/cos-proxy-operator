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
import os

import yaml
from charms.prometheus_k8s.v0.prometheus import PrometheusConsumer
from ops.charm import CharmBase, RelationBrokenEvent, RelationDepartedEvent
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

logger = logging.getLogger(__name__)

RULES_DIR = "/etc/prometheus/rules"


def ensure_directory(directory):
    """Ensure the directory exists, create if not."""

    if not os.path.exists(directory):
        try:
            os.makedirs(directory)
        except OSError as e:
            logger.error("Unable to make directory %s, error %s", directory, e)

            return


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
            self.on.prometheus_target_relation_departed,
            self._on_prometheus_target_relation_changed,
        )
        self.framework.observe(
            self.on.prometheus_target_relation_broken,
            self._on_prometheus_target_relation_changed,
        )

        self.framework.observe(
            self.on.downstream_prometheus_scrape_relation_joined,
            self._on_downstream_prometheus_scrape_relation_changed,
        )
        self.framework.observe(
            self.on.downstream_prometheus_scrape_relation_changed,
            self._on_downstream_prometheus_scrape_relation_changed,
        )
        self.framework.observe(
            self.on.downstream_prometheus_scrape_relation_broken,
            self._on_downstream_prometheus_scrape_relation_changed,
        )

        self.framework.observe(
            self.on.prometheus_rules_relation_joined,
            self._on_prometheus_rules_relation_changed,
        )
        self.framework.observe(
            self.on.prometheus_rules_relation_joined,
            self._on_prometheus_rules_relation_changed,
        )
        self.framework.observe(
            self.on.prometheus_rules_relation_broken,
            self._on_prometheus_rules_relation_changed,
        )

        # The PrometheusConsumer takes care of all the handling
        # of the "downstream-prometheus-scrape" relation
        # Note self._stored.scrape_jobs is of type StoredDict
        self.prometheus = PrometheusConsumer(
            self,
            "downstream-prometheus-scrape",
            {"prometheus": ">=2.0"},
            self.on.start,
            jobs=vars(self._stored.scrape_jobs)["_under"],
            alert_rules_path=RULES_DIR,
        )

    def _on_downstream_prometheus_scrape_relation_changed(self, event):
        self._check_needed_downstream_exists(event)

    def _on_prometheus_target_relation_changed(self, event):
        """Iterate all the existing prometheus_scrape relations
        and regenerate the list of jobs
        """

        self.unit.status = MaintenanceStatus("Updating Prometheus scrape jobs")

        scrape_jobs = []

        for target_relation in self.model.relations["prometheus-target"]:
            if isinstance(event, RelationBrokenEvent):
                if target_relation is event.relation:
                    continue

            # One static config per unit, as we need to specify
            # the entire Juju topology manually
            juju_model = self.model.name
            juju_model_uuid = self.model.uuid
            juju_application = target_relation.app.name

            # Add a target only when we have at least the hostname
            units_to_target = [
                unit
                for unit in target_relation.units
                if unit.app is not self.app
                and not (
                    isinstance(event, RelationDepartedEvent) and event.unit is unit
                )
                and target_relation.data[unit].get("hostname")
            ]

            if units_to_target:
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
                                    if target_relation.data[unit].get("port")
                                    else str(target_relation.data[unit].get("hostname"))
                                ],
                                "labels": {
                                    "juju_model": juju_model,
                                    "juju_model_uuid": juju_model_uuid,
                                    "juju_application": juju_application,
                                    "juju_unit": unit.name,
                                    "host": target_relation.data[unit].get("hostname"),
                                },
                            }
                            for unit in units_to_target
                        ],
                    }
                )

        self._stored.scrape_jobs = scrape_jobs

        logger.debug("Scrape-jobs updated: %s", scrape_jobs)

        # Set the charm to blocked if there is no downstream to send
        self._check_needed_downstream_exists()

    def _on_prometheus_rules_relation_changed(self, event):
        """Iterate all the existing prometheus_rules relations

        Regenerates the list of rules definitions for each related application
        """

        self.unit.status = MaintenanceStatus("Updating Prometheus scrape jobs")
        ensure_directory(RULES_DIR)

        for target_relation in self.model.relations["prometheus-rules"]:

            if isinstance(event, RelationBrokenEvent):
                if target_relation is event.relation:
                    continue

            # One static config per unit, as we need to specify
            # the entire Juju topology manually
            juju_model = self.model.name
            juju_model_uuid = self.model.uuid
            juju_application = target_relation.app.name

            # Add a target only when we have the 'groups' field
            units_to_target = [
                unit
                for unit in target_relation.units
                if unit.app is not self.app
                and not (
                    isinstance(event, RelationDepartedEvent) and event.unit is unit
                )
                and target_relation.data[unit].get("groups")
            ]

            # some units have rules for us to implement

            for unit in units_to_target:
                logger.debug("Writing relation rules for %s", unit.id)
                unit_rules = target_relation.data[unit].get("groups")
                # add this to the dict
                rules_list = yaml.safe_load(unit_rules)
                # PrometheusConsumer expects on .rule file per alert rule
                # multiple units will overwrite the same file, with the same data
                # TODO, clean up the files when relations are removed
                for rule in rules_list:
                    rule["labels"].update(
                        {
                            "juju_model": juju_model,
                            "juju_model_uuid": juju_model_uuid,
                            "juju_application": juju_application,
                        }
                    )
                    filename = "juju_{}_{}_{}_{}.rule".format(
                        juju_model,
                        juju_model_uuid,
                        juju_application,
                        rule["alert"],
                    )
                    try:
                        path = os.path.join(RULES_DIR, filename)
                        rules = yaml.dump([rule])
                        with open(path, "w") as rules_file:
                            rules_file.write(rules)
                    except IOError as e:
                        logger.error("Write to file %s failed with %s", path, e)

        self.unit.status = ActiveStatus()
        # The PrometheusConsumer _should_ call _set_scrape_metadata on relation events
        # which updates the downstream relation data based on finding the .rule files.
        self.prometheus._set_scrape_metadata(event)

    def _check_needed_downstream_exists(self, event=None):
        """Set the charm to blocked if there is no downstream of the
        right type available
        """

        missing_downstreams = []

        if self._stored.scrape_jobs:
            # I cannot figure out how to appease the linter so there goes repetition
            if isinstance(event, RelationBrokenEvent):
                if event.relation.name == "downstream-prometheus-scrape":
                    missing_downstreams.append("downstream-prometheus-scrape")
            elif not self.model.relations["downstream-prometheus-scrape"]:
                missing_downstreams.append("downstream-prometheus-scrape")

        if missing_downstreams:
            self.unit.status = BlockedStatus(
                "Missing needed downstream relations: {}".format(
                    ", ".join(missing_downstreams)
                )
            )
        else:
            self.unit.status = ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    main(LMAProxyCharm)
