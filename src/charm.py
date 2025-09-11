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

import copy
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

import yaml
from charms.grafana_agent.v0.cos_agent import (
    DEFAULT_RELATION_NAME as COS_AGENT_DEFAULT_RELATION_NAME,
)
from charms.grafana_agent.v0.cos_agent import COSAgentProvider, CosAgentProviderUnitData
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardAggregator
from charms.operator_libs_linux.v0.apt import remove_package
from charms.operator_libs_linux.v1.systemd import (
    daemon_reload,
    service_restart,
    service_resume,
    service_running,
    service_stop,
)
from charms.prometheus_k8s.v0.prometheus_scrape import _type_convert_stored
from cosl import JujuTopology, MandatoryRelationPairs
from cosl.rules import AlertRules, generic_alert_groups
from interfaces.prometheus_scrape.v0.schema import (
    AlertGroupModel,
    AlertRulesModel,
    ScrapeJobModel,
    ScrapeStaticConfigModel,
)
from ops import RelationBrokenEvent, RelationChangedEvent
from ops.charm import CharmBase, RelationJoinedEvent
from ops.framework import Object, StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus

from nrpe_exporter import (
    NrpeExporterProvider,
    NrpeTargetsChangedEvent,
)
from vector import VectorProvider

logger = logging.getLogger(__name__)

DASHBOARDS_DIR = "./src/cos_agent/grafana_dashboards"
COS_PROXY_DASHBOARDS_DIR = "./src/grafana_dashboards"
RULES_DIR = "./src/cos_agent/prometheus_alert_rules"
VECTOR_PORT = 9090


def _dedupe_list(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate items in the list via object identity."""
    unique_items = []
    for item in items:
        if item not in unique_items:
            unique_items.append(item)
    return unique_items


class COSProxyCharm(CharmBase):
    """This class instantiates Charmed Operator libraries and sets the status of the charm.

    No actual work is performed by the charm payload. It is only the libraries which are
    expected to listen on relations, respond to relation events, and set outgoing relation
    data.
    """

    _stored = StoredState()

    # The collection of all potential incoming relations
    relation_pairs = {
        "dashboards": [  # must be paired with:
            {"cos-agent"},  # or
            {"downstream-grafana-dashboard"},
        ],
        "filebeat": [  # must be paired with:
            {"downstream-logging"},
        ],
        "monitors": [  # (nrpe) must be paired with:
            {"cos-agent"},  # or
            {"downstream-prometheus-scrape"},
        ],
        "general-info": [  # (nrpe) must be paired with:
            {"cos-agent"},  # or
            {"downstream-prometheus-scrape"},
        ],
        "prometheus-target": [  # must be paired with:
            {"cos-agent"},  # or
            {"downstream-prometheus-scrape"},
        ],
        "prometheus-rules": [  # must be paired with:
            {"cos-agent"},  # or
            {"downstream-prometheus-scrape"},
        ],
        "prometheus": [  # must be paired with:
            {"cos-agent"},  # or
            {"downstream-prometheus-scrape"},
        ],
    }

    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(
            have_grafana=False,
            have_dashboards=False,
            have_prometheus=False,
            have_targets=False,
            have_nrpe=False,
            have_general_info_nrpe=False,
            have_loki=False,
            have_filebeat=False,
            have_gagent=False,
            have_prometheus_rules=False,
            have_prometheus_manual=False,
        )

        self._dashboard_aggregator = GrafanaDashboardAggregator(self)

        self.framework.observe(
            self.on.dashboards_relation_joined,  # pyright: ignore
            self._dashboards_relation_joined,
        )

        self.framework.observe(
            self.on.dashboards_relation_changed,  # pyright: ignore
            self._dashboards_relation_changed,
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

        self._scrape_config = ScrapeConfig(
            self.model.name, self.model.uuid, resolve_addresses=True
        )
        self.metrics_aggregator = MetricsEndpointAggregator(
            self,
            resolve_addresses=True,
            forward_alert_rules=cast(bool, self.config["forward_alert_rules"]),
            path_to_own_alert_rules="./src/prometheus_alert_rules",
        )
        self.cos_agent = COSAgentProvider(
            self,
            scrape_configs=self._get_scrape_configs(),
            metrics_rules_dir=RULES_DIR,
            dashboard_dirs=[COS_PROXY_DASHBOARDS_DIR, DASHBOARDS_DIR],
            refresh_events=[
                self.on.prometheus_target_relation_changed,
                self.on.prometheus_target_relation_broken,
                self.on.dashboards_relation_changed,
                self.on.dashboards_relation_broken,
            ],
        )

        self.framework.observe(
            self.on.cos_agent_relation_joined,  # pyright: ignore
            self._on_cos_agent_relation_joined,
        )

        self.framework.observe(
            self.on.cos_agent_relation_broken,  # pyright: ignore
            self._on_cos_agent_relation_broken,
        )

        self.nrpe_exporter = NrpeExporterProvider(self)
        self.framework.observe(
            self.nrpe_exporter.on.nrpe_targets_changed,  # pyright: ignore
            self._on_nrpe_targets_changed,
        )

        self.vector = VectorProvider(self)
        self.framework.observe(
            self.on.filebeat_relation_joined,
            self._on_filebeat_relation_joined,  # pyright: ignore
        )
        self.framework.observe(
            self.vector.on.config_changed,
            self._write_vector_config,  # pyright: ignore
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
            self.on.prometheus_target_relation_changed,  # pyright: ignore
            self._prometheus_target_relation_changed,
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
            self.on.monitors_relation_joined,
            self._nrpe_relation_joined,  # pyright: ignore
        )
        self.framework.observe(
            self.on.monitors_relation_broken,
            self._nrpe_relation_broken,  # pyright: ignore
        )

        self.framework.observe(
            self.on.general_info_relation_joined,
            self._general_info_relation_joined,  # pyright: ignore
        )
        self.framework.observe(
            self.on.general_info_relation_broken,
            self._general_info_relation_broken,  # pyright: ignore
        )

        self.framework.observe(
            self.on.prometheus_rules_relation_joined,
            self._prometheus_rules_relation_joined,  # pyright: ignore
        )
        self.framework.observe(
            self.on.prometheus_rules_relation_broken,
            self._prometheus_rules_relation_broken,  # pyright: ignore
        )

        self.framework.observe(
            self.on.prometheus_relation_joined,
            self._prometheus_manual_relation_joined,
            # pyright: ignore
        )
        self.framework.observe(
            self.on.prometheus_relation_broken,
            self._prometheus_manual_relation_broken,
            # pyright: ignore
        )

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.stop, self._on_stop)
        self.framework.observe(self.on.collect_unit_status, self._set_status)

        self.framework.observe(self.on.config_changed, self._update_alerts)

    def _update_alerts(self, event):
        self.metrics_aggregator._set_prometheus_data()

    def _on_cos_agent_relation_joined(self, _):
        self._stored.have_gagent = True

    def _on_cos_agent_relation_broken(self, _):
        self._stored.have_gagent = False

    def _delete_existing_dashboard_files(self, dashboards_dir: str):
        directory = Path(dashboards_dir)
        if directory.exists():
            for file_path in directory.glob("request_*.json"):
                file_path.unlink()

    def _create_dashboard_files(self, dashboards_dir: str):
        dashboards_rel = self._dashboard_aggregator._target_relation

        directory = Path(dashboards_dir)
        directory.mkdir(parents=True, exist_ok=True)

        for relation in self.model.relations[dashboards_rel]:
            for k in relation.data[self.unit].keys():
                if k.startswith("request_"):
                    dashboard = json.loads(relation.data[self.unit][k])["dashboard"]  # type: ignore
                    dashboard_file_path = (
                        directory / f"request_{k}.json"
                    )  # Using the key as filename
                    with open(dashboard_file_path, "w") as dashboard_file:
                        json.dump(dashboard, dashboard_file, indent=4)

    def _get_scrape_configs(self):
        """Return the scrape jobs."""
        jobs = []
        stored_jobs = _type_convert_stored(self.metrics_aggregator._stored.jobs)  # pyright: ignore
        if stored_jobs:
            for job_data in stored_jobs:
                stored_jobs_model = ScrapeJobModel(**job_data)  # pyright: ignore
                jobs.append(stored_jobs_model.model_dump())

        for relation in self.model.relations[self.metrics_aggregator._target_relation]:
            targets = self.metrics_aggregator._get_targets(relation)
            if targets and relation.app:
                target_job_data = self._scrape_config.from_targets(targets, relation.app.name)
                target_job = ScrapeJobModel(**target_job_data)
                jobs.append(target_job.model_dump())
        return jobs

    def _get_alert_groups(self) -> AlertRulesModel:
        """Return the alert rules groups."""
        alert_rules_model = AlertRulesModel(groups=[])
        stored_rules = _type_convert_stored(self.metrics_aggregator._stored.alert_rules)  # pyright: ignore
        if stored_rules:
            for rule_data in stored_rules:
                stored_rules_model = AlertGroupModel(**rule_data)  # pyright: ignore
                alert_rules_model.groups.append(stored_rules_model)

        for relation in self.model.relations[self.metrics_aggregator._alert_rules_relation]:
            unit_rules = self.metrics_aggregator._get_alert_rules(relation)
            if unit_rules and relation.app:
                appname = relation.app.name
                rules = self.metrics_aggregator._label_alert_rules(unit_rules, appname)
                group_name = self.metrics_aggregator.group_name(appname)
                group = AlertGroupModel(name=group_name, rules=rules)
                alert_rules_model.groups.append(group)

        return alert_rules_model

    def _handle_prometheus_alert_rule_files(self, rules_dir: str, app_name: str):
        groups = self._get_alert_groups()

        directory = Path(rules_dir)
        directory.mkdir(parents=True, exist_ok=True)
        alert_rules_file_path = directory / f"{app_name}-rules.yaml"

        with open(alert_rules_file_path, "w") as alert_rules_file:
            yaml.dump(groups.model_dump(), alert_rules_file, default_flow_style=False)

    def _dashboards_relation_joined(self, _):
        self._stored.have_dashboards = True

    def _dashboards_relation_changed(self, _):
        self._create_dashboard_files(DASHBOARDS_DIR)

    def _dashboards_relation_broken(self, _):
        self._stored.have_dashboards = False
        self._delete_existing_dashboard_files(DASHBOARDS_DIR)

    def _prometheus_rules_relation_joined(self, _):
        self._stored.have_prometheus_rules = True

    def _prometheus_rules_relation_broken(self, _):
        self._stored.have_prometheus_rules = False

    def _prometheus_manual_relation_joined(self, _):
        self._stored.have_prometheus_manual = True

    def _prometheus_manual_relation_broken(self, _):
        self._stored.have_prometheus_manual = False

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
        self._setup_nrpe_exporter()
        self._start_vector()
        self._stored.have_nrpe = True

    def _general_info_relation_joined(self, _):
        self._setup_nrpe_exporter()
        self._start_vector()
        self._stored.have_general_info_nrpe = True

    def _setup_nrpe_exporter(self):
        # Make sure the exporter binary is present with a systemd service
        if not Path("/usr/local/bin/nrpe-exporter").exists():
            arch = platform.machine()

            # Machine names vary. Here we follow Ubuntu's convention of "amd64" and "aarch64".
            # https://stackoverflow.com/a/45124927/3516684
            # https://en.wikipedia.org/wiki/Uname
            if arch in ["x86_64", "amd64"]:
                arch = "amd64"
            elif arch in ["aarch64", "arm64", "armv8b", "armv8l"]:
                arch = "aarch64"
            # else: keep arch as is

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

    def _general_info_relation_broken(self, _):
        self._stored.have_general_info_nrpe = False

    def _on_filebeat_relation_joined(self, event):
        self._stored.have_filebeat = True
        self._start_vector()

        event.relation.data[self.unit]["private-address"] = self.hostname
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

        config = self.vector.config
        with vector_config.open("w") as f:
            f.write(config)

    def _filebeat_relation_broken(self, _):
        self._stored.have_filebeat = False

    def _downstream_logging_relation_joined(self, _):
        self._stored.have_loki = True

    def _downstream_logging_relation_broken(self, _):
        self._stored.have_loki = False

    def _downstream_grafana_dashboard_relation_joined(self, _):
        self._stored.have_grafana = True

    def _downstream_grafana_dashboard_relation_broken(self, _):
        self._stored.have_grafana = False

    def _prometheus_target_relation_joined(self, _):
        self._stored.have_targets = True

    def _prometheus_target_relation_changed(self, event: RelationChangedEvent):
        self._handle_prometheus_alert_rule_files(RULES_DIR, event.app.name)

    def _prometheus_target_relation_broken(self, event: RelationBrokenEvent):
        self._stored.have_targets = False
        self._handle_prometheus_alert_rule_files(RULES_DIR, event.app.name)

    def _downstream_prometheus_scrape_relation_joined(self, _):
        if self._stored.have_nrpe:  # pyright: ignore
            self._on_nrpe_targets_changed(None)
        self._stored.have_prometheus = True

    def _downstream_prometheus_scrape_relation_broken(self, _):
        self._stored.have_prometheus = False

    def _nrpes_to_scrape_jobs(self, nrpes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert nrpe endpoints into scrape jobs.

        The intention of this method is to mimic the behavior of the scrape jobs creation in the
        MetricsEndpointAggregator on prometheus relations.

        There are some assumptions made:
            1. nrpe["target"] will only have one key-value pair, otherwise we ignore the rest
                - This could be an issue when nrpe has multiple units
            2. The "dns_name" for an nrpe endpoint set in the "static_configs"
        """
        jobs = []
        for nrpe in nrpes:
            nrpe_targets = [f"{t['hostname']}:{t['port']}" for t in nrpe["target"].values()]
            if len(nrpe_targets) > 1:
                logger.warning(
                    f"There are multiple nrpe targets ({nrpe['target']}), so the scrape job "
                    "will be built from the context of only the first target."
                )
            host = nrpe_targets[0].split(":")[0]
            labels = {
                "juju_model": self.model.name,
                "juju_model_uuid": self.model.uuid,
                "juju_application": nrpe["app_name"],
                "juju_unit": list(set(nrpe["target"].keys()))[0],
                "host": host,
                "dns_name": host,
            }
            static_cfgs = [ScrapeStaticConfigModel(**{"targets": nrpe_targets, "labels": labels})]
            additional = nrpe["additional_fields"]
            relabel_configs = self._scrape_config.relabel_configs()
            relabel_configs.extend(additional["relabel_configs"])
            extra = {
                "relabel_configs": relabel_configs,
                "params": additional["updates"]["params"],
            }
            job = ScrapeJobModel(
                job_name=additional["updates"]["job_name"],
                metrics_path=additional["updates"]["metrics_path"],
                static_configs=static_cfgs,
                **extra,
            )
            jobs.append(job.model_dump(exclude_unset=True))

        return jobs

    def _cos_agent_scrape_jobs_to_databag(self, scrape_jobs: List[Dict[str, Any]]):
        data_key = CosAgentProviderUnitData.KEY
        for relation in self.model.relations[COS_AGENT_DEFAULT_RELATION_NAME]:
            if data_key not in relation.data[self.unit]:
                continue
            data = json.loads(relation.data[self.unit][data_key])
            data.setdefault("metrics_scrape_jobs", [])
            jobs = _dedupe_list(scrape_jobs)
            data["metrics_scrape_jobs"] = jobs
            relation.data[self.unit][data_key] = CosAgentProviderUnitData(**data).json()

    def _on_nrpe_targets_changed(self, event: Optional[NrpeTargetsChangedEvent]):
        """Send NRPE jobs over to MetricsEndpointAggregator."""
        if event and isinstance(event, NrpeTargetsChangedEvent):
            removed_targets = event.removed_targets
            for r in removed_targets:
                self.metrics_aggregator.remove_prometheus_jobs(r)  # type: ignore

            removed_alerts = event.removed_alerts
            for a in removed_alerts:
                self.metrics_aggregator.remove_alert_rules(
                    self.metrics_aggregator.group_name(a["labels"]["juju_unit"]),  # type: ignore
                    a["labels"]["juju_unit"],  # type: ignore
                )

            nrpes = cast(List[Dict[str, Any]], event.current_targets)
            current_alerts = event.current_alerts
        else:
            # If the event arg is None, then the stored state value is already up-to-date.
            nrpes = self.nrpe_exporter.endpoints()
            current_alerts = self.nrpe_exporter.alerts()

        self._modify_enrichment_file(endpoints=nrpes)

        # Add scrape jobs for prometheus relations
        for nrpe in nrpes:
            self.metrics_aggregator.set_target_job_data(
                nrpe["target"], nrpe["app_name"], **nrpe["additional_fields"]
            )
        vector_target = {self.unit.name: {"hostname": self.host, "port": VECTOR_PORT}}
        self.metrics_aggregator.set_target_job_data(vector_target, self.app.name)

        # Add scrape jobs for cos-agent relations
        cos_agent_jobs = self._nrpes_to_scrape_jobs(nrpes)
        cos_agent_jobs.append(self._scrape_config.from_targets(vector_target, self.app.name))
        self._cos_agent_scrape_jobs_to_databag(cos_agent_jobs)

        for alert in current_alerts:
            self.metrics_aggregator.set_alert_rule_data(
                re.sub(r"/", "_", alert["labels"]["juju_unit"]),  # type: ignore
                alert,  # type: ignore
                label_rules=False,
            )

    def _set_status(self, _event):
        # Put charm in blocked status if all incoming relations are missing
        # We could obtain the set of active relations with:
        # {k for k, v in self.model.relations.items() if v}
        # but that would incur a relation-list and multiple relation-get calls, so building up from
        # stored state instead.
        active_relations = {
            rel
            for (rel, indicator) in [
                ("downstream-grafana-dashboard", self._stored.have_grafana),
                ("cos-agent", self._stored.have_gagent),
                ("dashboards", self._stored.have_dashboards),
                ("downstream-logging", self._stored.have_loki),
                ("filebeat", self._stored.have_filebeat),
                ("monitors", self._stored.have_nrpe),
                ("general-info", self._stored.have_general_info_nrpe),
                ("downstream-prometheus-scrape", self._stored.have_prometheus),
                ("prometheus-target", self._stored.have_targets),
                ("prometheus-rules", self._stored.have_prometheus_rules),
                ("prometheus", self._stored.have_prometheus_manual),
            ]
            if indicator
        }

        # Set blocked if _all_ incoming relations are missing. This helps notice under-configured
        # or redundant cos-proxy instances.
        if not set(self.relation_pairs.keys()).intersection(active_relations):
            self.unit.status = BlockedStatus("Add at least one incoming relation")
            logger.info(
                "Missing incoming relation(s). Add one or more of: %s.",
                ", ".join(self.relation_pairs.keys()),
            )
            return

        if missing := MandatoryRelationPairs(self.relation_pairs).get_missing_as_str(
            *active_relations
        ):
            self.unit.status = BlockedStatus(f"Missing {missing}")
            return

        self.unit.status = ActiveStatus()

    @property
    def hostname(self) -> str:
        """Unit's hostname."""
        return socket.getfqdn()

    @property
    def host(self) -> str:
        """Unit's ip address."""
        return socket.gethostbyname(self.hostname)


class ScrapeConfig:
    """A Prometheus scrape config instance.

    Follows an opinionated version of the upstream format:
    https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config
    """

    def __init__(
        self,
        model_name: str,
        model_uuid: str,
        relabel_instance=True,
        resolve_addresses=False,
    ):
        self._model_name = model_name
        self._model_uuid = model_uuid
        self._relabel_instance = relabel_instance
        self._resolve_addresses = resolve_addresses

    def job_name(self, appname: str) -> str:
        """Construct a scrape job name.

        Each relation has its own unique scrape job name. All units in
        the relation are scraped as part of the same scrape job.

        Args:
            appname: string name of a related application.

        Returns:
            a string Prometheus scrape job name for the application.
        """
        return f"juju_{self._model_name}_{self._model_uuid[:7]}_{appname}_prometheus_scrape"

    def from_targets(self, targets, application_name: str, **kwargs) -> dict:
        """Construct a static scrape job for an application from targets.

        Args:
            targets: a dictionary providing hostname and port for all scrape target. The keys of
                this dictionary are unit names. Values corresponding to these keys are themselves
                a dictionary with keys "hostname" and "port".
            application_name: a string name of the application for which this static scrape job is
                being constructed.
            kwargs: a `dict` of the extra arguments passed to the function

        Returns:
            A dictionary corresponding to a Prometheus static scrape job configuration for one
            application. The returned dictionary may be transformed into YAML and appended to the
            list of any existing list of Prometheus static configs.
        """
        job = {
            "job_name": self.job_name(application_name),
            "static_configs": [
                {
                    "targets": ["{}:{}".format(target["hostname"], target["port"])],
                    "labels": {
                        "juju_model": self._model_name,
                        "juju_model_uuid": self._model_uuid,
                        "juju_application": application_name,
                        "juju_unit": unit_name,
                        "host": target["hostname"],
                        # Expanding this will merge the dicts and replace the
                        # topology labels if any were present/found
                        **self._static_config_extra_labels(target),
                    },
                }
                for unit_name, target in targets.items()
            ],
            "relabel_configs": self.relabel_configs() + kwargs.get("relabel_configs", []),
        }
        job.update(kwargs.get("updates", {}))

        return job

    def _static_config_extra_labels(self, target: Dict[str, str]) -> Dict[str, str]:
        """Build a list of extra static config parameters, if specified."""
        extra_info = {}

        if self._resolve_addresses:
            try:
                dns_name = socket.gethostbyaddr(target["hostname"])[0]
            except OSError:
                logger.debug("Could not perform DNS lookup for %s", target["hostname"])
                dns_name = target["hostname"]
            extra_info["dns_name"] = dns_name

        return extra_info

    def relabel_configs(self) -> List[Dict[str, Any]]:
        """Create Juju topology relabeling configuration.

        Using Juju topology for instance labels ensures that these
        labels are stable across unit recreation.

        Returns:
            a list of Prometheus relabeling configurations. Each item in
            this list is one relabel configuration.
        """
        return (
            [
                {
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
            ]
            if self._relabel_instance
            else []
        )


class MetricsEndpointAggregator(Object):
    """Aggregate metrics from multiple scrape targets.

    `MetricsEndpointAggregator` collects scrape target information from one
    or more related charms and forwards this to a `MetricsEndpointConsumer`
    charm, which may be in a different Juju model. However, it is
    essential that `MetricsEndpointAggregator` itself resides in the same
    model as its scrape targets, as this is currently the only way to
    ensure in Juju that the `MetricsEndpointAggregator` will be able to
    determine the model name and uuid of the scrape targets.

    `MetricsEndpointAggregator` should be used in place of
    `MetricsEndpointProvider` in the following two use cases:

    1. Integrating one or more scrape targets that do not support the
    `prometheus_scrape` interface.

    2. Integrating one or more scrape targets through cross model
    relations. Although the [Scrape Config Operator](https://charmhub.io/cos-configuration-k8s)
    may also be used for the purpose of supporting cross model
    relations.

    Using `MetricsEndpointAggregator` to build a Prometheus charm client
    only requires instantiating it. Instantiating
    `MetricsEndpointAggregator` is similar to `MetricsEndpointProvider` except
    that it requires specifying the names of three relations: the
    relation with scrape targets, the relation for alert rules, and
    that with the Prometheus charms. For example

    ```python
    self._aggregator = MetricsEndpointAggregator(
        self,
        {
            "prometheus": "monitoring",
            "scrape_target": "prometheus-target",
            "alert_rules": "prometheus-rules"
        }
    )
    ```

    `MetricsEndpointAggregator` assumes that each unit of a scrape target
    sets in its unit-level relation data two entries with keys
    "hostname" and "port". If it is required to integrate with charms
    that do not honor these assumptions, it is always possible to
    derive from `MetricsEndpointAggregator` overriding the `_get_targets()`
    method, which is responsible for aggregating the unit name, host
    address ("hostname") and port of the scrape target.
    `MetricsEndpointAggregator` also assumes that each unit of a
    scrape target sets in its unit-level relation data a key named
    "groups". The value of this key is expected to be the string
    representation of list of Prometheus Alert rules in YAML format.
    An example of a single such alert rule is

    ```yaml
    - alert: HighRequestLatency
      expr: job:request_latency_seconds:mean5m{job="myjob"} > 0.5
      for: 10m
      labels:
        severity: page
      annotations:
        summary: High request latency
    ```

    Once again if it is required to integrate with charms that do not
    honour these assumptions about alert rules then an object derived
    from `MetricsEndpointAggregator` may be used by overriding the
    `_get_alert_rules()` method.

    `MetricsEndpointAggregator` ensures that Prometheus scrape job
    specifications and alert rules are annotated with Juju topology
    information, just like `MetricsEndpointProvider` and
    `MetricsEndpointConsumer` do.

    By default, `MetricsEndpointAggregator` ensures that Prometheus
    "instance" labels refer to Juju topology. This ensures that
    instance labels are stable over unit recreation. While it is not
    advisable to change this option, if required it can be done by
    setting the "relabel_instance" keyword argument to `False` when
    constructing an aggregator object.
    """

    _stored = StoredState()

    def __init__(
        self,
        charm,
        relation_names: Optional[dict] = None,
        relabel_instance=True,
        resolve_addresses=False,
        path_to_own_alert_rules: Optional[str] = None,
        *,
        forward_alert_rules: bool = True,
    ):
        """Construct a `MetricsEndpointAggregator`.

        Args:
            charm: a `CharmBase` object that manages this
                `MetricsEndpointAggregator` object. Typically, this is
                `self` in the instantiating class.
            relation_names: a dictionary with three keys. The value
                of the "scrape_target" and "alert_rules" keys are
                the relation names over which scrape job and alert rule
                information is gathered by this `MetricsEndpointAggregator`.
                And the value of the "prometheus" key is the name of
                the relation with a `MetricsEndpointConsumer` such as
                the Prometheus charm.
            relabel_instance: A boolean flag indicating if Prometheus
                scrape job "instance" labels must refer to Juju Topology.
            resolve_addresses: A boolean flag indiccating if the aggregator
                should attempt to perform DNS lookups of targets and append
                a `dns_name` label
            path_to_own_alert_rules: Optionally supply a path for alert rule files
            forward_alert_rules: a boolean flag to toggle forwarding of charmed alert rules
        """
        self._charm = charm
        self._scrape_config = ScrapeConfig(
            self._charm.model.name, self._charm.model.uuid, relabel_instance, resolve_addresses
        )

        relation_names = relation_names or {}
        self._prometheus_relation = relation_names.get(
            "prometheus", "downstream-prometheus-scrape"
        )
        self._target_relation = relation_names.get("scrape_target", "prometheus-target")
        self._alert_rules_relation = relation_names.get("alert_rules", "prometheus-rules")

        super().__init__(charm, self._prometheus_relation)
        self.topology = JujuTopology.from_charm(charm)

        self._stored.set_default(jobs=[], alert_rules=[])

        self._forward_alert_rules = forward_alert_rules

        # manage Prometheus charm relation events
        prometheus_events = self._charm.on[self._prometheus_relation]
        self.framework.observe(prometheus_events.relation_joined, self._set_prometheus_data)

        self.path_to_own_alert_rules = path_to_own_alert_rules

        # manage list of Prometheus scrape jobs from related scrape targets
        target_events = self._charm.on[self._target_relation]
        self.framework.observe(target_events.relation_changed, self._on_prometheus_targets_changed)
        self.framework.observe(
            target_events.relation_departed, self._on_prometheus_targets_departed
        )

        # manage alert rules for Prometheus from related scrape targets
        alert_rule_events = self._charm.on[self._alert_rules_relation]
        self.framework.observe(alert_rule_events.relation_changed, self._on_alert_rules_changed)
        self.framework.observe(alert_rule_events.relation_departed, self._on_alert_rules_departed)

    def _set_prometheus_data(self, event: Optional[RelationJoinedEvent] = None):
        """Ensure every new Prometheus instances is updated.

        Any time a new Prometheus unit joins the relation with
        `MetricsEndpointAggregator`, that Prometheus unit is provided
        with the complete set of existing scrape jobs and alert rules.
        """
        if not self._charm.unit.is_leader():
            return

        # Gather the scrape jobs
        jobs = [] + _type_convert_stored(
            self._stored.jobs  # pyright: ignore
        )  # list of scrape jobs, one per relation
        for relation in self.model.relations[self._target_relation]:
            targets = self._get_targets(relation)
            if targets and relation.app:
                jobs.append(self._scrape_config.from_targets(targets, relation.app.name))

        # Gather the alert rules
        groups = [] + _type_convert_stored(
            self._stored.alert_rules  # pyright: ignore
        )  # list of alert rule groups
        for relation in self.model.relations[self._alert_rules_relation]:
            unit_rules = self._get_alert_rules(relation)
            if unit_rules and relation.app:
                appname = relation.app.name
                rules = self._label_alert_rules(unit_rules, appname)
                group = {"name": self.group_name(appname), "rules": rules}
                groups.append(group)
        alert_rules = AlertRules(query_type="promql", topology=self.topology)
        # Add alert rules from file
        if self.path_to_own_alert_rules:
            alert_rules.add_path(self.path_to_own_alert_rules, recursive=True)
        # Add generic alert rules
        alert_rules.add(
            copy.deepcopy(generic_alert_groups.application_rules),
            group_name_prefix=self.topology.identifier,
        )
        groups.extend(alert_rules.as_dict()["groups"])

        groups = _dedupe_list(groups)
        jobs = _dedupe_list(jobs)

        # Set scrape jobs and alert rules in relation data
        relations = [event.relation] if event else self.model.relations[self._prometheus_relation]
        for rel in relations:
            rel.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)  # type: ignore
            rel.data[self._charm.app]["alert_rules"] = json.dumps(  # type: ignore
                {"groups": groups if self._forward_alert_rules else []}
            )

    def _on_prometheus_targets_changed(self, event):
        """Update scrape jobs in response to scrape target changes.

        When there is any change in relation data with any scrape
        target, the Prometheus scrape job, for that specific target is
        updated.
        """
        targets = self._get_targets(event.relation)
        if not targets:
            return

        # new scrape job for the relation that has changed
        self.set_target_job_data(targets, event.relation.app.name)

    def set_target_job_data(self, targets: dict, app_name: str, **kwargs) -> None:
        """Update scrape jobs in response to scrape target changes.

        When there is any change in relation data with any scrape
        target, the Prometheus scrape job, for that specific target is
        updated. Additionally, if this method is called manually, do the
        same.

        Args:
            targets: a `dict` containing target information
            app_name: a `str` identifying the application
            kwargs: a `dict` of the extra arguments passed to the function
        """
        if not self._charm.unit.is_leader():
            return

        # new scrape job for the relation that has changed
        updated_job = self._scrape_config.from_targets(targets, app_name, **kwargs)

        for relation in self.model.relations[self._prometheus_relation]:
            jobs = json.loads(relation.data[self._charm.app].get("scrape_jobs", "[]"))
            # list of scrape jobs that have not changed
            jobs = [job for job in jobs if updated_job["job_name"] != job["job_name"]]
            jobs.append(updated_job)
            relation.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)

            if not _type_convert_stored(self._stored.jobs) == jobs:  # pyright: ignore
                self._stored.jobs = jobs

    def _on_prometheus_targets_departed(self, event):
        """Remove scrape jobs when a target departs.

        Any time a scrape target departs, any Prometheus scrape job
        associated with that specific scrape target is removed.
        """
        job_name = self._scrape_config.job_name(event.relation.app.name)
        unit_name = event.unit.name
        self.remove_prometheus_jobs(job_name, unit_name)

    def remove_prometheus_jobs(self, job_name: str, unit_name: Optional[str] = ""):
        """Given a job name and unit name, remove scrape jobs associated.

        The `unit_name` parameter is used for automatic, relation data bag-based
        generation, where the unit name in labels can be used to ensure that jobs with
        similar names (which are generated via the app name when scanning relation data
        bags) are not accidentally removed, as their unit name labels will differ.
        For NRPE, the job name is calculated from an ID sent via the NRPE relation, and is
        sufficient to uniquely identify the target.
        """
        if not self._charm.unit.is_leader():
            return

        for relation in self.model.relations[self._prometheus_relation]:
            jobs = json.loads(relation.data[self._charm.app].get("scrape_jobs", "[]"))
            if not jobs:
                continue

            changed_job = [j for j in jobs if j.get("job_name") == job_name]
            if not changed_job:
                continue
            changed_job = changed_job[0]

            # list of scrape jobs that have not changed
            jobs = [job for job in jobs if job.get("job_name") != job_name]

            # list of scrape jobs for units of the same application that still exist
            configs_kept = [
                config
                for config in changed_job["static_configs"]  # type: ignore
                if config.get("labels", {}).get("juju_unit") != unit_name
            ]

            if configs_kept:
                changed_job["static_configs"] = configs_kept  # type: ignore
                jobs.append(changed_job)

            relation.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)

            if not _type_convert_stored(self._stored.jobs) == jobs:  # pyright: ignore
                self._stored.jobs = jobs

    def _get_targets(self, relation) -> dict:
        """Fetch scrape targets for a relation.

        Scrape target information is returned for each unit in the
        relation. This information contains the unit name, network
        hostname (or address) for that unit, and port on which a
        metrics endpoint is exposed in that unit.

        Args:
            relation: an `ops.model.Relation` object for which scrape
                targets are required.

        Returns:
            a dictionary whose keys are names of the units in the
            relation. There values associated with each key is itself
            a dictionary of the form
            ```
            {"hostname": hostname, "port": port}
            ```
        """
        targets = {}
        for unit in relation.units:
            if not (unit_databag := relation.data.get(unit)):
                continue
            if not (hostname := unit_databag.get("hostname")):
                continue
            port = unit_databag.get("port", 80)
            targets.update({unit.name: {"hostname": hostname, "port": port}})

        return targets

    def _on_alert_rules_changed(self, event):
        """Update alert rules in response to scrape target changes.

        When there is any change in alert rule relation data for any
        scrape target, the list of alert rules for that specific
        target is updated.
        """
        unit_rules = self._get_alert_rules(event.relation)
        if not unit_rules:
            return

        app_name = event.relation.app.name
        self.set_alert_rule_data(app_name, unit_rules)

    def set_alert_rule_data(self, name: str, unit_rules: dict, label_rules: bool = True) -> None:
        """Consolidate incoming alert rules (from stored-state or event) with those from relation data.

        The unit rules should be a dict, which have additional Juju topology labels added. For
        rules generated by the NRPE exporter, they are pre-labeled so lookups can be performed.
        The unit rules are combined with the alert rules from relation data before being written
        back to relation data and stored-state.
        """
        if not self._charm.unit.is_leader():
            return

        if label_rules:
            rules = self._label_alert_rules(unit_rules, name)
        else:
            rules = [unit_rules]
        updated_group = {"name": self.group_name(name), "rules": rules}

        for relation in self.model.relations[self._prometheus_relation]:
            alert_rules = json.loads(relation.data[self._charm.app].get("alert_rules", "{}"))
            groups = alert_rules.get("groups", [])
            # list of alert rule groups that have not changed
            for group in groups:
                if group["name"] == updated_group["name"]:
                    group["rules"] = [r for r in group["rules"] if r not in updated_group["rules"]]
                    group["rules"].extend(updated_group["rules"])

            if updated_group["name"] not in [g["name"] for g in groups]:
                groups.append(updated_group)

            groups = _dedupe_list(groups)

            relation.data[self._charm.app]["alert_rules"] = json.dumps(
                {"groups": groups if self._forward_alert_rules else []}
            )

            if not _type_convert_stored(self._stored.alert_rules) == groups:  # pyright: ignore
                self._stored.alert_rules = groups

    def _on_alert_rules_departed(self, event):
        """Remove alert rules for departed targets.

        Any time a scrape target departs any alert rules associated
        with that specific scrape target is removed.
        """
        group_name = self.group_name(event.relation.app.name)
        unit_name = event.unit.name
        self.remove_alert_rules(group_name, unit_name)

    def remove_alert_rules(self, group_name: str, unit_name: str) -> None:
        """Remove an alert rule group from relation data."""
        if not self._charm.unit.is_leader():
            return

        for relation in self.model.relations[self._prometheus_relation]:
            alert_rules = json.loads(relation.data[self._charm.app].get("alert_rules", "{}"))
            if not alert_rules:
                continue

            groups = alert_rules.get("groups", [])
            if not groups:
                continue

            changed_group = [group for group in groups if group["name"] == group_name]
            if not changed_group:
                continue
            changed_group = changed_group[0]

            # list of alert rule groups that have not changed
            groups = [group for group in groups if group["name"] != group_name]

            # list of alert rules not associated with departing unit
            rules_kept = [
                rule
                for rule in changed_group.get("rules")  # type: ignore
                if rule.get("labels").get("juju_unit") != unit_name
            ]

            if rules_kept:
                changed_group["rules"] = rules_kept  # type: ignore
                groups.append(changed_group)

            groups = _dedupe_list(groups)

            relation.data[self._charm.app]["alert_rules"] = json.dumps(
                {"groups": groups if self._forward_alert_rules else []}
            )

            if not _type_convert_stored(self._stored.alert_rules) == groups:  # pyright: ignore
                self._stored.alert_rules = groups

    def _get_alert_rules(self, relation) -> dict:
        """Fetch alert rules for a relation.

        Each unit of the related scrape target may have its own
        associated alert rules. Alert rules for all units are returned
        indexed by unit name.

        Args:
            relation: an `ops.model.Relation` object for which alert
                rules are required.

        Returns:
            a dictionary whose keys are names of the units in the
            relation. There values associated with each key is a list
            of alert rules. Each rule is in dictionary format. The
            structure "rule dictionary" corresponds to single
            Prometheus alert rule.
        """
        rules = {}
        for unit in relation.units:
            if not (unit_databag := relation.data.get(unit)):
                continue
            if not (unit_rules := yaml.safe_load(unit_databag.get("groups", ""))):
                continue

            rules.update({unit.name: unit_rules})

        return rules

    def group_name(self, unit_name: str) -> str:
        """Construct name for an alert rule group.

        Each unit in a relation may define its own alert rules. All
        rules, for all units in a relation are grouped together and
        given a single alert rule group name.

        Args:
            unit_name: string name of a related application.

        Returns:
            a string Prometheus alert rules group name for the unit.
        """
        unit_name = re.sub(r"/", "_", unit_name)
        return "juju_{}_{}_{}_alert_rules".format(self.model.name, self.model.uuid[:7], unit_name)

    def _label_alert_rules(self, unit_rules, app_name: str) -> list:
        """Apply juju topology labels to alert rules.

        Args:
            unit_rules: a list of alert rules, where each rule is in
                dictionary format.
            app_name: a string name of the application to which the
                alert rules belong.

        Returns:
            a list of alert rules with Juju topology labels.
        """
        labeled_rules = []
        for unit_name, rules in unit_rules.items():
            for rule in rules:
                # the new JujuTopology removed this, so build it up by hand
                matchers = {
                    "juju_{}".format(k): v
                    for k, v in JujuTopology(self.model.name, self.model.uuid, app_name, unit_name)
                    .as_dict(excluded_keys=["charm_name"])
                    .items()
                }
                rule["labels"].update(matchers.items())
                labeled_rules.append(rule)

        return labeled_rules


if __name__ == "__main__":  # pragma: no cover
    main(COSProxyCharm)
