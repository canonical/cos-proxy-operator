# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""## Overview.

## Vector Library Usage

The `VectorProvider` object may be used by charms to configure
Vector, which is a tool which can consume observability data from
a large number of sources, modify/manipulate it, and send it onwards
to appropriate 'sinks'.

Vector also provides an equivalent to `node_exporter` so metrics from
the host itself can be collected.
"""

import json
import logging
import re
from typing import Optional

import yaml
from ops.charm import CharmBase, RelationEvent
from ops.framework import EventBase, EventSource, Object, ObjectEvents

# The unique Charmhub library identifier, never change it
LIBID = "0xdeadbeef"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 2


logger = logging.getLogger(__name__)


DEFAULT_RELATION_NAMES = {"filebeat": "elastic-beats", "downstream-logging": "loki_push_api"}

# `$` is interpolated in the actual config, so use `$$` to use `$capture_group`
# comment is here because otherwise Python's YAML serializer does the wrong thing,
# and VRL tries to interpret it
DEFAULT_VECTOR_CONFIG = """
---
data_dir: /var/lib/vector
api:
  enabled: true
  address: "[::]:8686"
  playground: false
enrichment_tables:
  nrpe:
    type: file
    file:
      path: /etc/vector/nrpe_lookup.csv
      encoding:
        type: csv
    schema:
      composite_key: string
      juju_application: string
      juju_unit: string
      command: string
      ipaddr: string
sources:
  nrpe-logs:
    type: journald
    since_now: false
    current_boot_only: true
    include_units:
      - nrpe-exporter
  host_metrics:
    filesystem:
      devices:
        excludes: [binfmt_misc]
      filesystems:
        excludes: [binfmt_misc]
      mountPoints:
        excludes: ["*/proc/sys/fs/binfmt_misc"]
    scrape_interval_secs: 60
    type: host_metrics
  internal_metrics:
    type: internal_metrics
    scrape_interval_secs: 60
  logstash:
    address: "0.0.0.0:5044"
    type: logstash
transforms:
  enrich-nrpe:
    type: remap
    inputs:
      - nrpe-logs
    source: |-
      fields = parse_key_value!(.message)

      . = fields

      composite_key = join!([fields.address, "_", fields.command])
      .composite_key = composite_key
      row = get_enrichment_table_record!("nrpe", {"composite_key": composite_key})

      .juju_unit = row.juju_unit
      .juju_application = row.juju_application
      .command = row.command
      .ip_address = fields.address
      .duration = fields.duration
      .return_code = fields.return_code
      .output = fields.command_output
  mangle-logstash:
    type: remap
    inputs:
      - logstash
    source: |-
      # Remove some fields
      del(.@metadata)
      del(.prospector)
      del(.log)
      del(.input)
      del(.offset)
      del(.source_type)
      del(.host)

      .timestamp = del(.@timestamp)
      .filename = del(.source)

      .hostname = del(.beat.hostname)
      del(.beat)

      .juju_model = .fields.juju_model_name
      .juju_model_uuid = .fields.juju_model_uuid
      .juju_unit = .fields.juju_principal_unit
      del(.fields)

      .juju_application = replace!(.juju_unit, r'^(?P<app>.*?)/\d+$', "$$app")

      structured =
        parse_syslog(.message) ??
        parse_common_log(.message) ??
        parse_regex(.message, r'^(?P<level>\w+)\s(?P<module>[\.\w]+)\s?(?:\[.*?req-(?P<request_id>[-a-z0-9]+).*?\])?\s?(?:\[instance:\s+(?P<instance>[-a-z0-9]+)\])?(?:\[-\])?(?P<msg>.+)') ??
        {"message": .message}
      . = merge(., structured)

      . = flatten(.)
      . = map_keys(.) -> |key| { replace(key, r'^.*?\.(?P<rest>.*)', "$$rest") }
sinks:
  prom_exporter:
    type: prometheus_exporter
    inputs:
      - host_metrics
      - internal_metrics
    address: "[::]:9090"
  stdout:
    inputs:
      - mangle-logstash
      - enrich-nrpe
    type: console
    encoding:
      codec: json
    target: stdout
"""


class VectorConfigChangedEvent(EventBase):
    """Event emitted when NRPE Exporter targets change."""

    def __init__(self, handle, config: str):
        super().__init__(handle)
        self.config = config

    def snapshot(self):
        """Save target relation information."""
        return {"config": self.config}

    def restore(self, snapshot):
        """Restore target relation information."""
        self.config = snapshot["config"]


class VectorEvents(ObjectEvents):
    """Event descriptor for events raised by `NrpeExporterProvider`."""

    config_changed = EventSource(VectorConfigChangedEvent)


class VectorProvider(Object):
    """A provider for Vector, an observability swiss-army knife."""

    on = VectorEvents()

    def __init__(self, charm: CharmBase, relation_names: Optional[dict] = None):
        """A Vector exporter.

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

        super().__init__(charm, "_".join(relation_names.keys()))
        self._charm = charm
        self._relation_names = relation_names
        for relation_name in relation_names.keys():
            events = self._charm.on[relation_name]
            self.framework.observe(events.relation_changed, self._on_log_relation_changed)
            self.framework.observe(events.relation_departed, self._on_log_relation_changed)

    def _on_log_relation_changed(self, event: RelationEvent):
        """Handle changes with related endpoints.

        Anytime there are changes in relations between the filebeat and/or Loki
        endpoints, fire an event so the providing charm can fetch the new config
        and restart vector.

        Args:
            event: a `RelationEvent` signifying a change.
        """
        self.on.config_changed.emit(config=self.config)

    @property
    def config(self) -> str:
        """Build a configuration for Vector."""
        config_template = yaml.safe_load(DEFAULT_VECTOR_CONFIG)
        loki_endpoints = []
        loki_sinks = {}
        for relation_name in self._relation_names.keys():
            for relation in self._charm.model.relations[relation_name]:
                if not relation.units:
                    continue
                for unit in relation.units:
                    if endpoint := relation.data[unit].get("endpoint", ""):
                        loki_endpoints.append(json.loads(endpoint))

        if loki_endpoints:
            for idx, endpoint in enumerate(loki_endpoints):
                loki_sinks.update(
                    {
                        f"loki-{idx}": {
                            "type": "loki",
                            "inputs": ["mangle-logstash", "enrich-nrpe"],
                            # vector wants the base URL, use that
                            "endpoint": re.sub(
                                r"^(.*?)/loki/api/v1/push$", r"\1", endpoint["url"]
                            ),
                            "healthcheck": {"enabled": False},
                            "acknowledgements": {"enabled": True},
                            "out_of_order_action": "accept",
                            "encoding": {"codec": "json"},
                            "labels": {
                                "juju_model": "{{ juju_model }}",
                                "juju_unit": "{{ juju_unit }}",
                                "juju_application": "{{ juju_application }}",
                                "ip_address": "{{ ip_address }}",
                                "filename": "{{ filename }}",
                                "hostname": "{{ hostname }}",
                                "command": "{{ command }}",
                            },
                        }
                    }
                )

        config_template["sinks"].update(loki_sinks)
        return yaml.dump(config_template)
