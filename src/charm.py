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

import json
import logging
import platform
import re
import shutil
import socket
import stat
import textwrap
from csv import DictReader, DictWriter
from pathlib import Path
from typing import Any, Dict, List, Optional, cast
from urllib import request

from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardAggregator
from charms.nrpe_exporter.v0.nrpe_exporter import (
    NrpeExporterProvider,
    NrpeTargetsChangedEvent,
)
from charms.operator_libs_linux.v0.apt import remove_package
from charms.operator_libs_linux.v1.systemd import (
    daemon_reload,
    service_restart,
    service_resume,
    service_running,
    service_stop,
)
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointAggregator
from charms.vector.v0.vector import VectorProvider
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
            have_loki=False,
            have_filebeat=False,
        )

        self._dashboard_aggregator = GrafanaDashboardAggregator(self)

        self.framework.observe(
            self.on.dashboards_relation_joined,  # pyright: ignore
            self._dashboards_relation_joined,
        )

        self.framework.observe(
            self.on.dashboards_relation_broken,  # pyright: ignore
            self._dashboards_relation_broken,
        )

        self.framework.observe(
            self.on.downstream_grafana_dashboard_relation_joined,  # pyright: ignore
            self._downstream_grafana_dashboard_relation_joined,
        )

        self.framework.observe(
            self.on.downstream_grafana_dashboard_relation_broken,  # pyright: ignore
            self._downstream_grafana_dashboard_relation_broken,
        )

        self.metrics_aggregator = MetricsEndpointAggregator(self, resolve_addresses=True)

        self.nrpe_exporter = NrpeExporterProvider(self)
        self.framework.observe(
            self.nrpe_exporter.on.nrpe_targets_changed,  # pyright: ignore
            self._on_nrpe_targets_changed,
        )

        self.vector = VectorProvider(self)
        self.framework.observe(
            self.on.filebeat_relation_joined, self._on_filebeat_relation_joined  # pyright: ignore
        )
        self.framework.observe(
            self.vector.on.config_changed, self._write_vector_config  # pyright: ignore
        )

        self.framework.observe(
            self.on.downstream_logging_relation_joined,  # pyright: ignore
            self._downstream_logging_relation_joined,
        )

        self.framework.observe(
            self.on.downstream_logging_relation_broken,  # pyright: ignore
            self._downstream_logging_relation_broken,
        )

        self.framework.observe(
            self.on.prometheus_target_relation_joined,  # pyright: ignore
            self._prometheus_target_relation_joined,
        )
        self.framework.observe(
            self.on.prometheus_target_relation_broken,  # pyright: ignore
            self._prometheus_target_relation_broken,
        )

        self.framework.observe(
            self.on.downstream_prometheus_scrape_relation_joined,  # pyright: ignore
            self._downstream_prometheus_scrape_relation_joined,
        )
        self.framework.observe(
            self.on.downstream_prometheus_scrape_relation_broken,  # pyright: ignore
            self._downstream_prometheus_scrape_relation_broken,
        )

        self.framework.observe(
            self.on.monitors_relation_joined, self._nrpe_relation_joined  # pyright: ignore
        )
        self.framework.observe(
            self.on.monitors_relation_broken, self._nrpe_relation_broken  # pyright: ignore
        )

        self.framework.observe(
            self.on.general_info_relation_joined, self._nrpe_relation_joined  # pyright: ignore
        )
        self.framework.observe(
            self.on.general_info_relation_broken, self._nrpe_relation_broken  # pyright: ignore
        )

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.stop, self._on_stop)

        self._set_status()

    def _dashboards_relation_joined(self, _):
        self._stored.have_dashboards = True
        self._set_status()

    def _dashboards_relation_broken(self, _):
        self._stored.have_dashboards = False
        self._set_status()

    def _on_install(self, _):
        """Initial charm setup."""
        # Cull out rsyslog so the disk doesn't fill up, and we don't use it for anything, so we
        # don't need to install/setup logrotate
        remove_package("rsyslog")
        self.unit.set_workload_version("n/a")

    def _on_stop(self, _):
        """Ensure that nrpe exporter is removed and stopped."""
        if service_running("nrpe-exporter"):
            service_stop("nrpe-exporter")

        files = ["/usr/local/bin/nrpe-exporter", "/etc/systemd/systemd/nrpe-exporter.service"]

        for f in files:
            if Path(f).exists():
                Path(f).unlink()

    def _nrpe_relation_joined(self, _):
        self._stored.have_nrpe = True
        self._setup_nrpe_exporter()
        self._start_vector()
        self._set_status()

    def _setup_nrpe_exporter(self):
        # Make sure the exporter binary is present with a systemd service
        if not Path("/usr/local/bin/nrpe-exporter").exists():
            arch = platform.machine()
            arch = "amd64" if arch == "x86_64" else arch
            res = "nrpe_exporter-{}".format(arch)

            st = Path(res)
            st.chmod(st.stat().st_mode | stat.S_IEXEC)
            shutil.copy(str(st.absolute()), "/usr/local/bin/nrpe-exporter")

            systemd_template = textwrap.dedent(
                """
                [Unit]
                Description=NRPE Prometheus exporter
                Wants=network-online.target
                After=network-online.target

                [Service]
                LimitNPROC=infinity
                LimitNOFILE=infinity
                ExecStart=/usr/local/bin/nrpe-exporter
                Restart=always

                [Install]
                WantedBy=multi-user.target
                """
            )

            with open("/etc/systemd/system/nrpe-exporter.service", "w") as f:
                f.write(systemd_template)

            daemon_reload()
            service_restart("nrpe-exporter.service")

            # This seems dumb, since it's actually unmasking and setting it to
            # `enable --now`, but it's the only method which ACTUALLY enables it
            # so it will survive reboots
            service_resume("nrpe-exporter.service")

    def _nrpe_relation_broken(self, _):
        self._stored.have_nrpe = False
        self._set_status()

    def _on_filebeat_relation_joined(self, event):
        self._stored.have_filebeat = True
        self._start_vector()

        event.relation.data[self.unit]["private-address"] = socket.getfqdn()
        event.relation.data[self.unit]["port"] = "5044"

    def _start_vector(self):
        # Make sure the vector binary is present with a systemd service
        if not Path("/usr/local/bin/vector").exists():
            res = "vector"

            st = Path(res)
            st.chmod(st.stat().st_mode | stat.S_IEXEC)
            shutil.copy(str(st.absolute()), "/usr/local/bin/vector")

            systemd_template = textwrap.dedent(
                """
                [Unit]
                Description="Vector - An observability pipelines tool"
                Documentation=https://vector.dev/
                Wants=network-online.target
                After=network-online.target

                [Service]
                Type=exec
                LimitNPROC=infinity
                LimitNOFILE=infinity
                Environment="LOG_FORMAT=json"
                Environment="VECTOR_CONFIG_YAML=/etc/vector/aggregator/vector.yaml"
                ExecStartPre=/usr/local/bin/vector validate
                ExecStart=/usr/local/bin/vector -w
                ExecReload=/usr/local/bin/vector validate
                ExecReload=/bin/kill -HUP $MAINPID
                Restart=always
                AmbientCapabilities=CAP_NET_BIND_SERVICE

                [Install]
                WantedBy=multi-user.target
                """
            )

            with open("/etc/systemd/system/vector.service", "w") as f:
                f.write(systemd_template)

            self._write_vector_config(None)
            self._modify_enrichment_file()

            daemon_reload()
            service_restart("vector.service")
            service_resume("vector.service")

    @property
    def path(self):
        """Path to the nrpe targets file."""
        path = Path("/etc/vector/nrpe_lookup.csv")
        return path

    def _modify_enrichment_file(self, endpoints: Optional[List[Dict[str, Any]]] = None):
        fieldnames = ["composite_key", "juju_application", "juju_unit", "command", "ipaddr"]
        path = self.path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="") as f:
                writer = DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()

        if not endpoints:
            return

        current = []
        for endpoint in endpoints:
            target = (
                f"{endpoint['target'][next(iter(endpoint['target']))]['hostname']}_"
                + f"{endpoint['additional_fields']['updates']['params']['command'][0]}"
            )

            unit_name = next(
                filter(
                    lambda d: "target_label" in d and d["target_label"] == "juju_unit",  # type: ignore
                    endpoint["additional_fields"]["relabel_configs"],
                )
            )["replacement"]

            # Out of all the fieldnames in the csv file, "composite_key" and "juju_unit" are sufficient to uniquely
            # identify targets. This is needed for calculating an up-to-date list of targets on relation changes.
            current.append((target, unit_name))

        with path.open(newline="") as f:
            reader = DictReader(f)
            contents = [r for r in reader if (r["composite_key"], r["juju_unit"]) in current]

            for endpoint in endpoints:
                unit = next(
                    iter(
                        [
                            c["replacement"]
                            for c in endpoint["additional_fields"]["relabel_configs"]
                            if c["target_label"] == "juju_unit"
                        ]
                    )
                )
                entry = {
                    "composite_key": f"{endpoint['target'][next(iter(endpoint['target']))]['hostname']}_"
                    + f"{endpoint['additional_fields']['updates']['params']['command'][0]}",
                    "juju_application": re.sub(r"^(.*?)/\d+$", r"\1", unit),
                    "juju_unit": unit,
                    "command": endpoint["additional_fields"]["updates"]["params"]["command"][0],
                    "ipaddr": f"{endpoint['target'][next(iter(endpoint['target']))]['hostname']}",
                }

                if entry not in contents:
                    contents.append(entry)

        if contents:
            with path.open("w", newline="") as f:
                writer = DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()

                for c in contents:
                    writer.writerow(c)

    def _write_vector_config(self, _):
        if not Path("/var/lib/vector").exists():
            Path("/var/lib/vector").mkdir(parents=True, exist_ok=True)

        vector_config = Path("/etc/vector/aggregator/vector.yaml")
        if not vector_config.exists():
            vector_config.parent.mkdir(parents=True, exist_ok=True)

        loki_endpoints = []
        for relation in self.model.relations["downstream-logging"]:
            if not relation.units:
                continue
            for unit in relation.units:
                if endpoint := relation.data[unit].get("endpoint", ""):
                    loki_endpoints.append(json.loads(endpoint)["url"])

        if loki_endpoints:
            for e in loki_endpoints:
                try:
                    dest = re.sub(r"^(.*?/loki/api/v1)/push$", r"\1/series", e)
                    r = request.urlopen(dest)
                    if r.code != 200:
                        self.unit.status = BlockedStatus(
                            "One or more Loki endpoints is not reachable!"
                        )
                        return
                except request.HTTPError:
                    self.unit.status = BlockedStatus(
                        "One or more Loki endpoints is not reachable!"
                    )
                    return

        config = self.vector.config
        with vector_config.open("w") as f:
            f.write(config)

    def _filebeat_relation_broken(self, _):
        self._stored.have_filebeat = False
        self._set_status()

    def _downstream_logging_relation_joined(self, _):
        self._stored.have_loki = True
        self._set_status()

    def _downstream_logging_relation_broken(self, _):
        self._stored.have_loki = False
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
        if self._stored.have_nrpe:  # pyright: ignore
            self._on_nrpe_targets_changed(None)
        self._set_status()

    def _downstream_prometheus_scrape_relation_broken(self, _):
        self._stored.have_prometheus = False
        self._set_status()

    def _on_nrpe_targets_changed(self, event):
        """Send NRPE jobs over to MetricsEndpointAggregator."""
        if event and isinstance(event, NrpeTargetsChangedEvent):
            removed_targets = event.removed_targets
            for r in removed_targets:
                self.metrics_aggregator.remove_prometheus_jobs(r)

            removed_alerts = event.removed_alerts
            for a in removed_alerts:
                self.metrics_aggregator.remove_alert_rules(
                    self.metrics_aggregator.group_name(a["labels"]["juju_unit"]),  # type: ignore
                    a["labels"]["juju_unit"],  # type: ignore
                )

            nrpes = cast(List[Dict[str, Any]], event.current_targets)
        else:
            # If the event arg is None, then the stored state value is already up to date.
            nrpes = self.nrpe_exporter.endpoints()

        self._modify_enrichment_file(endpoints=nrpes)

        for nrpe in nrpes:
            self.metrics_aggregator.set_target_job_data(
                nrpe["target"], nrpe["app_name"], **nrpe["additional_fields"]
            )

        alerts = self.nrpe_exporter.alerts()
        for alert in alerts:
            self.metrics_aggregator.set_alert_rule_data(
                re.sub(r"/", "_", alert["labels"]["juju_unit"]),
                alert,
                label_rules=False,
            )

    def _set_status(self):
        message = ""
        if (self._stored.have_grafana and not self._stored.have_dashboards) or (  # pyright: ignore
            self._stored.have_dashboards and not self._stored.have_grafana  # pyright: ignore
        ):
            message = " one of (Grafana|dashboard) relation(s) "

        if (
            self._stored.have_loki  # pyright: ignore
            and not (self._stored.have_filebeat or self._stored.have_nrpe)  # pyright: ignore
        ) or (
            self._stored.have_filebeat and not self._stored.have_loki  # pyright: ignore
        ):
            message = " one of (Loki|filebeat) relation(s) "

        if (
            self._stored.have_prometheus  # pyright: ignore
            and not (self._stored.have_targets or self._stored.have_nrpe)  # pyright: ignore
        ) or (
            (self._stored.have_targets or self._stored.have_nrpe)  # pyright: ignore
            and not self._stored.have_prometheus  # pyright: ignore
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
