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

from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointAggregator
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardAggregator
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus

logger = logging.getLogger(__name__)


class LMAProxyCharm(CharmBase):

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(
            have_grafana=False,
            have_prometheus=False,
            have_targets=False
        )

        self._dashboard_aggregator = GrafanaDashboardAggregator(
           self,
           {
             "grafana": "downstream-grafana-dashboard",
             "grafana-dashboard": "dashboards",
           },
        )

        self.framework.observe(
            self.on.dashboards_relation_joined,
            self._dashboards_relation_joined,
        )

        self.framework.observe(
            self.on.dashboards_relation_broken,
            self._dashboards_relation_broken,
        )

        self.framework.observe(
            self.on.downstream_grafana_dashboard_relation_joined,
            self._downstream_grafana_dashboard_relation_joined,
        )

        self.framework.observe(
            self.on.downstream_grafana_dashboard_relation_broken,
            self._downstream_grafana_dashboard_relation_broken,
        )

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

    def _dashboards_relation_joined(self, _):
        self._stored.have_dashboards = True
        self._set_status()

    def _dashboards_relation_broken(self, _):
        self._stored.have_dashboards = False
        self._set_status()

    def _downstream_grafana_dashboard_relation_joined(self, _):
        self._stored.have_grafana = True
        self._set_status()

    def _downstream_grafana_dashboard_relation_broken(self, _):
        self._stored.have_grafana = False
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
        gen = lambda val, message: message if not val else ""
        messages = {
            "grafana": gen("Grafana relation", self._stored.have_grafana),
            "prometheus": gen("Prometheus relation", self._stored.have_prometheus),
            "scrape": gen("scrape targets", self._stored.have_targets),
            "dashboards": gen("dashboards", self._stored.have_dashboards)
        }

        message = " and ".join(messages.values())

        message = "Missing {}".format(message) if message else ""

        if message:
            self.unit.status = BlockedStatus(message)
        else:
            self.unit.status = ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    main(LMAProxyCharm)
