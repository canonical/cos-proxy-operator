# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.
"""## Overview.

## NRPE Exporter Library Usage

The `NrpeExporterProvider` object may be used by charms to call
configured `nrpe` daemons and export the job results to Prometheus
metrics to facilitate migrating from Nagios+NRPE to
Prometheus+Alertmanager. In order to do this, a charm must
instantiate the `NrpeExporterProvider` object, and optionally a list
of relations which will be checked for `monitor` data to configure
Prometheus jobs.

The parent charm should also provide an instantiated
`MetricsEndpointAggregator`, so events sent from this object can be
consumed and sent through the Prometheus relation, by responding to
`NrpeTargetsChangedEvent`.


    self.framework.observe(
        self.nrpe_exporter_provider.on.nrpe_targets_changed,
        self._on_nrpe_targets_changed,
    )
"""

import json
import logging
import re
from json.decoder import JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple, cast

import yaml
from ops.charm import CharmBase, RelationRole
from ops.framework import (
    EventBase,
    EventSource,
    Object,
    ObjectEvents,
    StoredState,
)

from metrics_endpoint_aggregator import _type_convert_stored

# The unique Charmhub library identifier, never change it
LIBID = "0xdeadbeef"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 9


logger = logging.getLogger(__name__)


DEFAULT_RELATION_NAMES = {
    "general-info": "general-info",
    "monitors": "monitors",
}


class RelationNotFoundError(Exception):
    """Raised if there is no relation with the given name is found."""

    def __init__(self, relation_name: str):
        self.relation_name = relation_name
        self.message = "No relation named '{}' found".format(relation_name)

        super().__init__(self.message)


class RelationInterfaceMismatchError(Exception):
    """Raised if the relation with the given name has a different interface."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_interface: str,
        actual_relation_interface: str,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_interface
        self.actual_relation_interface = actual_relation_interface
        self.message = (
            "The '{}' relation has '{}' as interface rather than the expected '{}'".format(
                relation_name, actual_relation_interface, expected_relation_interface
            )
        )

        super().__init__(self.message)


class RelationRoleMismatchError(Exception):
    """Raised if the relation with the given name has a different role."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_role: RelationRole,
        actual_relation_role: RelationRole,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_role
        self.actual_relation_role = actual_relation_role
        self.message = "The '{}' relation has role '{}' rather than the expected '{}'".format(
            relation_name, repr(actual_relation_role), repr(expected_relation_role)
        )

        super().__init__(self.message)


def _validate_relation_by_interface_and_direction(
    charm: CharmBase,
    relation_name: str,
    expected_relation_interface: str,
    expected_relation_role: RelationRole,
):
    """Verifies that a relation has the necessary characteristics.

    Verifies that the `relation_name` provided: (1) exists in metadata.yaml,
    (2) declares as interface the interface name passed as `relation_interface`
    and (3) has the right "direction", i.e., it is a relation that `charm`
    provides or requires.

    Args:
        charm: a `CharmBase` object to scan for the matching relation.
        relation_name: the name of the relation to be verified.
        expected_relation_interface: the interface name to be matched by the
            relation named `relation_name`.
        expected_relation_role: whether the `relation_name` must be either
            provided or required by `charm`.

    Raises:
        RelationNotFoundError: If there is no relation in the charm's metadata.yaml
            with the same name as provided via `relation_name` argument.
        RelationInterfaceMismatchError: The relation with the same name as provided
            via `relation_name` argument does not have the same relation interface
            as specified via the `expected_relation_interface` argument.
        RelationRoleMismatchError: If the relation with the same name as provided
            via `relation_name` argument does not have the same role as specified
            via the `expected_relation_role` argument.
    """
    if relation_name not in charm.meta.relations:
        raise RelationNotFoundError(relation_name)

    relation = charm.meta.relations[relation_name]

    actual_relation_interface = relation.interface_name
    if actual_relation_interface and actual_relation_interface != expected_relation_interface:
        raise RelationInterfaceMismatchError(
            relation_name, expected_relation_interface, actual_relation_interface
        )

    if expected_relation_role == RelationRole.provides:
        if relation_name not in charm.meta.provides:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.provides, RelationRole.requires
            )
    elif expected_relation_role == RelationRole.requires:
        if relation_name not in charm.meta.requires:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.requires, RelationRole.provides
            )
    else:
        raise Exception("Unexpected RelationDirection: {}".format(expected_relation_role))


def find_key(d: dict, key: str) -> Any:
    """Finds a key nested arbitrarily deeply inside a dictself.

    Principally useful since the structure of NRPE relation data is
    not completely reliable.
    """
    if key in d:
        return d[key]
    for child in d.values():
        if not isinstance(child, dict):
            continue
        val = find_key(child, key)
        if val:
            return val
    return None


class NrpeTargetsChangedEvent(EventBase):
    """Event emitted when NRPE Exporter targets change."""

    def __init__(
        self,
        handle,
        current_targets: List[Dict[str, Any]],
        current_alerts: List[Dict[str, Any]],
        removed_targets: List[str],
        removed_alerts: List[str],
    ):
        super().__init__(handle)
        self.current_targets = current_targets
        self.current_alerts = current_alerts
        self.removed_targets = removed_targets or []
        self.removed_alerts = removed_alerts or []

    def snapshot(self):
        """Save target relation information."""
        return {
            "current_targets": self.current_targets,
            "current_alerts": self.current_alerts,
            "removed_targets": self.removed_targets,
            "removed_alerts": self.removed_alerts,
        }

    def restore(self, snapshot):
        """Restore target relation information."""
        self.current_targets = _type_convert_stored(snapshot["current_targets"])
        self.current_alerts = _type_convert_stored(snapshot["current_alerts"])
        self.removed_targets = _type_convert_stored(snapshot["removed_targets"])
        self.removed_alerts = _type_convert_stored(snapshot["removed_alerts"])


class NrpeEvents(ObjectEvents):
    """Event descriptor for events raised by `NrpeExporterProvider`."""

    nrpe_targets_changed = EventSource(NrpeTargetsChangedEvent)


class NrpeExporterProvider(Object):
    """A NRPE exporter based monitor."""

    on = NrpeEvents()  # pyright: ignore
    _stored = StoredState()

    def __init__(self, charm: CharmBase, relation_names: Optional[dict] = None):
        """A NRPE exporter based monitor.

        Args:
            charm: a `CharmBase` instance that manages this
                instance of the NRPE Exporter.
            relation_names: an optional `dict` containing the interfaces to monitor.

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `prometheus_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.requires`
                role.
        """
        relation_names = relation_names or DEFAULT_RELATION_NAMES
        for relation_name, relation_interface in relation_names.items():
            _validate_relation_by_interface_and_direction(
                charm, relation_name, relation_interface, RelationRole.requires
            )

        super().__init__(charm, "_".join(relation_names.keys()))
        self._charm = charm
        self._stored.set_default(endpoints=[], alert_rules=[])
        self._relation_names = relation_names
        for relation_name in relation_names.keys():
            events = self._charm.on[relation_name]
            self.framework.observe(events.relation_changed, self._on_nrpe_relation_changed)
            self.framework.observe(events.relation_departed, self._on_nrpe_relation_changed)
        self.framework.observe(self._charm.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self._charm.on.config_changed, self._on_config_changed)

    def _on_upgrade_charm(self, _: EventBase):
        self._regenerate_nrpe()

    def _on_config_changed(self, _: EventBase):
        self._regenerate_nrpe()

    def _on_nrpe_relation_changed(self, _: EventBase):
        self._regenerate_nrpe()

    def _regenerate_nrpe(self):
        """Handle changes with related endpoints.

        Anytime there are changes in relations between the NRPE exporter
        and NRPE endpoints, the relation tree is scraped, and configurations
        are generated, which can be watched through a `NrpeTargetsChangedEvent`.

        Args:
            event: a `RelationEvent` signifying a change.
        """
        nrpe_endpoints = []
        nrpe_alerts = []  # type: List[Dict]

        for relation_name in self._relation_names.keys():
            for relation in self._charm.model.relations[relation_name]:
                endpoints, alerts = self._generate_data(relation)
                nrpe_endpoints.extend(endpoints)
                nrpe_alerts.extend(alerts)

        removed_endpoints = [
            e["additional_fields"]["updates"]["job_name"]  # pyright: ignore
            for e in _type_convert_stored(self._stored.endpoints)  # pyright: ignore
            if e not in nrpe_endpoints
        ]

        removed_alerts = [
            a
            for a in _type_convert_stored(self._stored.alert_rules)  # pyright: ignore
            if a not in nrpe_alerts
        ]

        self.on.nrpe_targets_changed.emit(  # pyright: ignore
            current_targets=nrpe_endpoints,
            current_alerts=nrpe_alerts,
            removed_targets=removed_endpoints,
            removed_alerts=removed_alerts,
        )

        # Important: update stored state only after relation data is updated successfully.
        # https://github.com/canonical/cos-proxy-operator/issues/88#issuecomment-1798063141
        self._stored.endpoints = nrpe_endpoints
        self._stored.alert_rules = nrpe_alerts  # pyright: ignore

    def endpoints(self) -> list:
        """Fetch the list of NRPE exporter targets.

        Returns:
            A list consisting of all the endpoints and partial configurations
            to be ingested by Prometheus.
        """
        return _type_convert_stored(self._stored.endpoints)  # pyright: ignore

    def alerts(self) -> list:
        """Fetch the list of automatically generated alert rules.

        Returns:
            A list of alert rules, in dict format.
        """
        return _type_convert_stored(self._stored.alert_rules)  # pyright: ignore

    def _generate_data(self, relation) -> Tuple[list, list]:
        """Find NRPE jobs for a single relation, if they exist, and format the data.

        Args:
            relation: an `ops.model.Relation` object whose NRPE
                configuration is checked.

        Returns:
            A tuple consisting of a list (possibly empty) of NRPE endpoints as a dict
                containing parameterized data which can be ingested by Prometheus. If
                endpoints are generated, the second item in the tuple will be a list of
                dynamically created alert rules for those targets

        Machine/reactive monitor data can appear on a number of different interfaces,
        and takes the format of:

            {
                egress-subnets: 10.159.132.134/32
                ingress-address: 10.159.132.134
                machine_id: "11"
                model_id: 89156c40-01c5-4db3-8445-3806a2758fb7
                monitors: '{''monitors'': {''remote'': {''nrpe'': {''check_conntrack'':
                  ''check_conntrack''}}}, ''version'': ''0.3''}'
                nagios_host_context: juju
                nagios_hostname: juju-juju-758fb7-11
                private-address: 10.159.132.134
                target-address: 10.159.132.134
                target-id: juju-ubuntu-5
            }

        Or:
            {
                egress-subnets: 10.159.132.106/32
                ingress-address: 10.159.132.106
                monitors: |
                  version: '0.3'
                  monitors:
                      remote:
                          nrpe:
                              memcached:
                                  command: check_memcached
                private-address: 10.159.132.106
                target_address: 10.159.132.106
                target_id: juju-memcached-0
            }

        It may be YAML *or* JSON, and the command may be a dict with `command` as
        a key *or* it may be a bare string.

        In addition, this information may appear in multiple interfaces with
        different jobs, and requires combining.
        """
        if not relation.units:
            return [], []

        nrpe_endpoints = []
        alerts = []

        binding_relation = self._charm.model.get_binding(relation)

        exporter_address = None
        if binding_relation:
            exporter_address = binding_relation.network.bind_address

        for unit in relation.units:
            monitors = relation.data[unit].get("monitors", "")
            if isinstance(monitors, str):
                try:
                    monitor_data = json.loads(monitors)
                except JSONDecodeError:
                    logger.debug("No JSON data found in the relation. Trying yaml...")
                try:
                    monitor_data = yaml.safe_load(monitors)
                except yaml.YAMLError:
                    logger.warning("NRPE monitor string did not have JSON or YAML. Skipping")
                    continue
            elif isinstance(monitors, dict):
                monitor_data = monitors
            else:
                logger.warning("Received monitor data with an unknown format. Skipping.")
                continue

            if not isinstance(monitor_data, dict):
                logger.warning("Monitor data is not a dict after parsing. Skipping.")
                continue
            jobs = find_key(monitor_data, "nrpe")
            if jobs:
                for val in jobs.values():
                    if isinstance(val, str):
                        cmd = val
                    else:
                        cmd = next(iter(val.values()))
                    # The ID could be `target-id` or `target_id`
                    id = relation.data[unit].get("target-id", "") or relation.data[unit].get(
                        "target_id", ""
                    )
                    id = re.sub(r"^juju[-_]", "", id)

                    nagios_host_context = relation.data[unit].get("nagios_host_context", "")

                    alerts.append(
                        self._generate_alert(relation, cmd, id, unit, nagios_host_context)
                    )

                    nrpe_endpoints.append(
                        self._generate_prometheus_job(
                            relation, unit, cmd, exporter_address, id, nagios_host_context
                        )
                    )
            else:
                logger.debug("No NRPE check is defined.")

        return nrpe_endpoints, alerts

    def _remove_host_context(self, label: str, nagios_host_context: str) -> str:
        # "nagios_host_content" is needed to extract it from the "label" which is generated
        # using the "target-id"
        # so that we can correctly relabel juju_application and juju_unit.
        nagios_host_context = nagios_host_context + "-" if nagios_host_context else ""
        return label.replace(nagios_host_context, "", 1)

    def _generate_alert(self, relation, cmd, id, unit, nagios_host_context) -> dict:
        """Generate an on-the-fly Alert rule."""
        unit_label = re.sub(r"^(.*?)[-_](\d+)$", r"\1/\2", id.replace("_", "-"))
        app_label = re.sub(r"^(.*?)[-_]\d+$", r"\1", id.replace("_", "-"))

        unit_label = self._remove_host_context(unit_label, nagios_host_context)
        app_label = self._remove_host_context(app_label, nagios_host_context)

        return {
            "alert": "{}NrpeAlert".format("".join([x.title() for x in cmd.split("_")])),
            # Average over 5 minutes considering a 60-second scrape interval
            # We need to "round" so the severity label is always set. This is
            # necessary for PagerDuty's dynamic notifications.
            #
            # We multiply the `absent_over_time` by 2 so the value is mapped to a critical severity.
            "expr": f"round(avg_over_time(command_status{{juju_unit='{unit_label}',command='{cmd}'}}[15m])) {'>=' if cast(bool, self._charm.model.config.get('nrpe_alert_on_warning')) else '>'} 1"
            + f" or ((absent_over_time(command_status{{juju_unit='{unit_label}',command='{cmd}'}}[10m]) == 1))*2"
            + f" or ((absent_over_time(up{{juju_unit='{unit_label}'}}[10m]) == 1))*2",
            "for": "0m",
            "labels": {
                "severity": "{{ if eq $value 0.0 -}} info {{- else if eq $value 1.0 -}} warning {{- else if eq $value 2.0 -}} critical {{- else if eq $value 3.0 -}} error {{- end }}",
                "juju_model": self.model.name,
                "juju_application": app_label,
                "juju_unit": unit_label,
                "nrpe_application": relation.app.name,
                "nrpe_unit": unit.name,
            },
            "annotations": {
                "summary": "Unit {{ $labels.juju_unit }}: {{ $labels.command }} {{ $labels.severity }}.",
                "description": "Check provided by nrpe_exporter in model {{ $labels.juju_model }} is failing.\n"
                "Failing check = {{ $labels.command }}\n"
                "Unit = {{ $labels.juju_unit }}\n"
                "Value = {{ $value }}\n"
                "Legend:\n"
                "  - StatusOK       = 0\n"
                "  - StatusWarning  = 1\n"
                "  - StatusCritical = 2\n"
                "  - StatusUnknown  = 3",
            },
        }

    def _generate_prometheus_job(
        self, relation, unit, cmd, exporter_address, id, nagios_host_context
    ) -> dict:
        """Generate an on-the-fly Prometheus scrape job."""
        # IP address could be 'target-address' OR 'target_address'
        addr = relation.data[unit].get("target-address", "") or relation.data[unit].get(
            "target_address", ""
        )

        return {
            "app_name": relation.app.name,
            "target": {
                unit.name: {
                    "hostname": addr,
                    "port": "5666",
                },
            },
            "additional_fields": {
                "relabel_configs": [
                    {"source_labels": ["__address__"], "target_label": "__param_target"},
                    {"source_labels": ["__param_target"], "target_label": "instance"},
                    {"source_labels": ["__param_command"], "target_label": "command"},
                    {
                        "target_label": "__address__",
                        "replacement": "{}:9275".format(exporter_address),
                    },
                    {
                        "target_label": "juju_unit",
                        # Turn nagios-host-context-sql-foo-0 into sql-foo/0
                        "replacement": re.sub(
                            r"^(.*?)[-_](\d+)$",
                            r"\1/\2",
                            self._remove_host_context(id.replace("_", "-"), nagios_host_context),
                        ),
                    },
                    {
                        "target_label": "juju_application",
                        # Turn nagios-host-context-sql-foo-0 into sql-foo
                        "replacement": re.sub(
                            r"^(.*?)[-_](\d+)$",
                            r"\1",
                            self._remove_host_context(id.replace("_", "-"), nagios_host_context),
                        ),
                    },
                ],
                "updates": {
                    "params": {
                        "command": [cmd],
                        "ssl": [True],
                    },
                    "metrics_path": "/export",
                    # Override job_name with something specific to the NRPE job
                    # and the unit it's coming from.
                    #
                    # relation.data[unit]["target-id"] contains the monitored
                    # charm, as `juju-{appname}-{unit_number}
                    "job_name": "juju_{}_{}_{}_{}_prometheus_scrape".format(
                        self.model.name,
                        self.model.uuid[:7],
                        id.replace("-", "_"),
                        cmd,
                    ),
                },
            },
        }
