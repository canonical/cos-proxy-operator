#!/usr/bin/env python3
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
"""This charm provides an interface between machine/reactive charms and COS charms.

COS is composed of Charmed Operators running in Kubernetes using the Operator Framework
rather than Charms.Reactive. The purpose of this module is to provide a bridge between
reactive charms and Charmed Operators to provide observability into both types of
deployments from a single place -- COS.

Interfaces from COS should be offered in Juju, and consumed by the appropriate model so
relations can be established.

Currently supported interfaces are for:
    * Grafana dashboards
    * Prometheus scrape targets
    * NRPE Endpoints
"""

import logging
import stat
import textwrap
from pathlib import Path

from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardAggregator
from charms.nrpe_exporter.v0.nrpe_exporter import NrpeExporterProvider
from charms.operator_libs_linux.v1.systemd import (
    daemon_reload,
    service_restart,
    service_running,
    service_stop,
)
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointAggregator
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus

logger = logging.getLogger(__name__)


class COSProxyCharm(CharmBase):
    """This class instantiates Charmed Operator libraries and sets the status of the charm.

    No actual work is performed by the charm payload. It is only the libraries which are
    expected to listen on relations, respond to relation events, and set outgoing relation
    data.
    """

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(
            have_grafana=False,
            have_dashboards=False,
            have_prometheus=False,
            have_targets=False,
            have_nrpe=False,
        )

        self._dashboard_aggregator = GrafanaDashboardAggregator(self)

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

        self.metrics_aggregator = MetricsEndpointAggregator(
            self,
            {
                "prometheus": "downstream-prometheus-scrape",
                "scrape_target": "prometheus-target",
                "alert_rules": "prometheus-rules",
            },
        )

        self.nrpe_exporter = NrpeExporterProvider(self)
        self.framework.observe(self.nrpe_exporter.on.nrpe_targets_changed, self._forward_nrpe)

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

        self.framework.observe(self.on.monitors_relation_joined, self._nrpe_relation_joined)
        self.framework.observe(self.on.monitors_relation_broken, self._nrpe_relation_broken)

        self.framework.observe(self.on.general_info_relation_joined, self._nrpe_relation_joined)
        self.framework.observe(self.on.general_info_relation_broken, self._nrpe_relation_broken)

        self.framework.observe(self.on.stop, self._on_stop)

        self._set_status()

    def _dashboards_relation_joined(self, _):
        self._stored.have_dashboards = True
        self._set_status()

    def _dashboards_relation_broken(self, _):
        self._stored.have_dashboards = False
        self._set_status()

    def _on_stop(self, _):
        """Ensure that nrpe exporter is removed and stopped."""
        if service_running("nrpe-exporter"):
            service_stop("nrpe-exporter")

        files = ["/usr/local/bin/nrpe_exporter", "/etc/systemd/systemd/nrpe-exporter.service"]

        for f in files:
            if Path(f).exists():
                Path(f).unlink()

    def _nrpe_relation_joined(self, _):
        self._stored.have_nrpe = True

        # Make sure the exporter binary is present with a systemd service
        if not Path("/usr/local/bin/nrpe_exporter").exists():
            with open(self.model.resources.fetch("nrpe-exporter"), "rb") as f:
                with open("/usr/local/bin/nrpe_exporter", "wb") as g:
                    chunk_size = 4096
                    chunk = f.read(chunk_size)
                    while len(chunk) > 0:
                        g.write(chunk)
                        chunk = f.read(chunk_size)

            st = Path("/usr/local/bin/nrpe_exporter")
            st.chmod(st.stat().st_mode | stat.S_IEXEC)

            systemd_template = textwrap.dedent(
                """
                [Unit]
                Description=NRPE Prometheus exporter

                [Service]
                ExecStart=/usr/local/bin/nrpe_exporter

                [Install]
                WantedBy=multi-user.target
                """
            )

            with open("/etc/systemd/system/nrpe-exporter.service", "w") as f:
                f.write(systemd_template)

            daemon_reload()
            service_restart("nrpe-exporter.service")

        self._set_status()

    def _nrpe_relation_broken(self, _):
        self._stored.have_nrpe = False
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
        if self._stored.have_nrpe:
            self._forward_nrpe(None)
        self._set_status()

    def _downstream_prometheus_scrape_relation_broken(self, _):
        self._stored.have_prometheus = False
        self._set_status()

    def _forward_nrpe(self, _):
        """Send NRPE jobs over to MetricsEndpointAggregator."""
        nrpes = self.nrpe_exporter.endpoints()

        for nrpe in nrpes:
            self.metrics_aggregator.set_target_job_data(
                nrpe["target"], nrpe["app_name"], **nrpe["additional_fields"]
            )

    def _set_status(self):
        message = ""
        if (self._stored.have_grafana and not self._stored.have_dashboards) or (
            self._stored.have_dashboards and not self._stored.have_grafana
        ):
            message = " one of (Grafana|dashboard) relation(s) "

        if (
            self._stored.have_prometheus
            and not (self._stored.have_targets or self._stored.have_nrpe)
        ) or (
            (self._stored.have_targets or self._stored.have_nrpe)
            and not self._stored.have_prometheus
        ):
            message += "{} one of (Prometheus|target|nrpe) relation(s)".format(
                "and" if message else ""
            )

        message = "Missing {}".format(message.strip()) if message else ""

        if message:
            self.unit.status = BlockedStatus(message)
        else:
            self.unit.status = ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    main(COSProxyCharm)
