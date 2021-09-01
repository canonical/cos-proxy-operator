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

from charms.prometheus_k8s.v0.prometheus import MetricsEndpointAggregator
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus

logger = logging.getLogger(__name__)


class LMAProxyCharm(CharmBase):

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(have_prometheus=False)
        self._stored.set_default(have_targets=False)

        self._metrics_aggregator = MetricsEndpointAggregator(
            self,
            {
                "prometheus": "downstream-prometheus-scrape",
                "scrape_target": "prometheus-target",
                "alert_rules": "prometheus-rules",
            },
        )

        self.framework.observe(
            self.on.prometheus_target_relation_joined,
            self._prometheus_target_relation_joined,
        )

        self.framework.observe(
            self.on.prometheus_target_relation_broken,
            self._prometheus_target_relation_broken,
        )

        self.framework.observe(
            self.on.downstream_prometheus_scrape_relation_joined,
            self._downstream_prometheus_scrape_relation_joined,
        )

        self.framework.observe(
            self.on.downstream_prometheus_scrape_relation_broken,
            self._downstream_prometheus_scrape_relation_broken,
        )

        self._set_status()

    def _prometheus_target_relation_joined(self, _):
        self._stored.have_targets = True
        self._set_status()

    def _prometheus_target_relation_broken(self, _):
        self._stored.have_targets = False
        self._set_status()

    def _downstream_prometheus_scrape_relation_joined(self, _):
        self._stored.have_prometheus = True
        self._set_status()

    def _downstream_prometheus_scrape_relation_broken(self, _):
        self._stored.have_prometheus = False
        self._set_status()

    def _set_status(self):
        if not self._stored.have_prometheus and not self._stored.have_targets:
            message = "Missing Prometheus and scrape targets"
        elif not self._stored.have_prometheus:
            message = "Missing Prometheus relation"
        elif not self._stored.have_targets:
            message = "Missing scrape targets"
        else:
            message = ""

        if message:
            self.unit.status = BlockedStatus(message)
        else:
            self.unit.status = ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    main(LMAProxyCharm)
