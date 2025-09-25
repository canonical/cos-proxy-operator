"""Aggregate metrics from multiple scrape targets."""

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml
from charms.prometheus_k8s.v0.prometheus_scrape import _type_convert_stored
from cosl import JujuTopology
from cosl.rules import AlertRules, generic_alert_groups
from ops.charm import RelationJoinedEvent
from ops.framework import Object, StoredState

from scrape_config import ScrapeConfig

logger = logging.getLogger(__name__)


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
        self.framework.observe(prometheus_events.relation_joined, self.update_alerts)

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

    def _set_prometheus_rel_data_from_stored_state(self, event: Optional[RelationJoinedEvent] = None):
        jobs = _type_convert_stored(self._stored.jobs)  # pyright: ignore
        groups = _type_convert_stored(self._stored.alert_rules)  # pyright: ignore
        if event and hasattr(event, "relation"):
            relations = [event.relation]
        else:
            relations = self.model.relations[self._prometheus_relation]
        for rel in relations:
            rel.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)  # type: ignore
            rel.data[self._charm.app]["alert_rules"] = json.dumps(  # type: ignore
                {"groups": groups if self._forward_alert_rules else []}
            )

    def update_alerts(self, event: Optional[RelationJoinedEvent] = None):
        """Recalculate all data sources, update stored state, set relation data."""
        self._set_stored_state_for_downstream_data()
        self._set_prometheus_rel_data_from_stored_state(event)

    def _add_constant_alerts(self, groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return all alerts which are static and not dependant on relations or events."""
        # Add cos-proxy's own rules from file
        alert_rules = AlertRules("promql", self.topology)
        if self.path_to_own_alert_rules:
            alert_rules.add_path(self.path_to_own_alert_rules, recursive=True)

        # Add generic alert rules
        alert_rules.add(
            copy.deepcopy(generic_alert_groups.application_rules),
            group_name_prefix=self.topology.identifier,
        )

        groups.extend(alert_rules.as_dict()["groups"])

        return groups

    def _set_stored_state_for_downstream_data(self):
        """Recalculate all data source and update stored state for alert rules and scrape configs."""
        if not self._charm.unit.is_leader():
            return

        # Gather the scrape jobs from targets relation data
        jobs = [] + _type_convert_stored(self._stored.jobs)  # pyright: ignore
        for relation in self.model.relations[self._target_relation]:
            targets = self._get_targets(relation)
            if targets and relation.app:
                jobs.append(self._scrape_config.from_targets(targets, relation.app.name))

        # Gather the alert rules from rules relation data
        groups = [] + _type_convert_stored(self._stored.alert_rules)  # pyright: ignore

        groups.extend(self._add_constant_alerts(groups))
        groups.extend(self._get_alert_groups_from_rules_relation())

        # Deduplicate prior to storing the data
        groups = _dedupe_list(groups)
        jobs = _dedupe_list(jobs)

        # Update stored state
        if not _type_convert_stored(self._stored.jobs) == jobs:  # pyright: ignore
            self._stored.jobs = jobs
        if not _type_convert_stored(self._stored.alert_rules) == groups:  # pyright: ignore
            self._stored.alert_rules = groups

    def _get_alert_groups_from_rules_relation(self) -> List[Dict[str, Any]]:
        """Return the alert rules groups.

        Add rules from _alert_rules_relation ("alert_rules", "prometheus-rules")
        """
        groups = []
        for relation in self.model.relations[self._alert_rules_relation]:
            unit_rules = self._get_alert_rules(relation)
            if not (unit_rules and relation.app):
                continue
            rules = self._label_alert_rules(unit_rules, relation.app.name)
            groups.append({"name": self.group_name(relation.app.name), "rules": rules})

        return groups

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
        """Update scrape jobs given a scrape target.

        This method updates the stored state and all downstream (e.g. cos-agent, prometheus, etc.)
        relation data.

        Args:
            targets: a `dict` containing target information
            app_name: a `str` identifying the application
            kwargs: a `dict` of the extra arguments passed to the function
        """
        if not self._charm.unit.is_leader():
            return

        # new scrape job for the relation that has changed
        updated_job = self._scrape_config.from_targets(targets, app_name, **kwargs)
        scrape_job_context = ScrapeJobContext(updated_job=updated_job)
        self._update_cos_agent(scrape_job_context)

        for relation in self.model.relations[self._prometheus_relation]:
            jobs = json.loads(relation.data[self._charm.app].get("scrape_jobs", "[]"))
            jobs = scrape_job_context.get_jobs(jobs)
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

    def _update_cos_agent(
        self,
        scrape_job_context: Optional["ScrapeJobContext"] = None,
        alert_rule_context: Optional["AlertRuleContext"] = None,
    ):
        """Coordinate cos-agent (alert rules and scrape configs) with downstream prometheus relation data.

        Update stored state via cos-agent IFF a prometheus relation does not exist. Otherwise, the
        downstream prometheus relation handles this.

        WARNING: This method creates a dependency between COSAgentProvider and MetricsEndpointAggregator.
        """
        if not hasattr(self._charm, "cos_agent"):
            return
        cos_agent_relations = self.model.relations[self._charm.cos_agent._relation_name]
        prom_relations = self.model.relations[self._prometheus_relation]
        if cos_agent_relations and not prom_relations:
            for relation in cos_agent_relations:
                if not (config_str := relation.data[self._charm.unit].get("config")):
                    continue

                config = json.loads(config_str)

                # Scrape configs
                if scrape_job_context is not None:
                    jobs = scrape_job_context.get_jobs(config.get("metrics_scrape_jobs", []))
                    config["metrics_scrape_jobs"] = jobs
                    if not _type_convert_stored(self._stored.jobs) == jobs:  # pyright: ignore
                        self._stored.jobs = jobs

                # Alert rules
                metrics_alert_rules = config.get("metrics_alert_rules", {})
                if alert_rule_context is not None:
                    groups = alert_rule_context.get_groups(metrics_alert_rules)
                else:
                    groups = metrics_alert_rules.get("groups", [])
                config["metrics_alert_rules"] = {
                    "groups": groups if self._forward_alert_rules else []
                }
                if not _type_convert_stored(self._stored.alert_rules) == groups:  # pyright: ignore
                    self._stored.alert_rules = groups

                # Update relation data
                relation.data[self._charm.unit]["config"] = json.dumps(config)

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

        scrape_job_context = ScrapeJobContext(job_name, unit_name)
        self._update_cos_agent(scrape_job_context)

        for relation in self.model.relations[self._prometheus_relation]:
            jobs = json.loads(relation.data[self._charm.app].get("scrape_jobs", "[]"))
            jobs = scrape_job_context.get_jobs(jobs)
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
        alert_rule_context = AlertRuleContext(updated_group=updated_group)
        self._update_cos_agent(alert_rule_context=alert_rule_context)
        for relation in self.model.relations[self._prometheus_relation]:
            alert_rules = json.loads(relation.data[self._charm.app].get("alert_rules", "{}"))
            groups = alert_rule_context.get_groups(alert_rules)
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

        alert_rule_context = AlertRuleContext(group_name, unit_name)
        self._update_cos_agent(alert_rule_context=alert_rule_context)

        for relation in self.model.relations[self._prometheus_relation]:
            alert_rules = json.loads(relation.data[self._charm.app].get("alert_rules", "{}"))
            groups = alert_rule_context.get_groups(alert_rules)
            relation.data[self._charm.app]["alert_rules"] = json.dumps({"groups": groups})

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


def _dedupe_list(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate items in the list via object identity."""
    unique_items = []
    for item in items:
        if item not in unique_items:
            unique_items.append(item)
    return unique_items


@dataclass
class ScrapeJobContext:
    """Coordinate events for scrape jobs."""

    removed_job_name: Optional[str] = None
    removed_unit_name: Optional[str] = None
    updated_job: Dict[str, Any] = field(default_factory=dict)

    def get_jobs(self, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """In the event of scrape job additions, removal, or both, return the resulting list of scrape jobs."""
        # Add scrape jobs
        jobs = [job for job in jobs if job["job_name"] != self.updated_job.get("job_name")]
        if self.updated_job:
            jobs.append(self.updated_job)

        # Remove scrape jobs
        if not (changed_job := [j for j in jobs if j.get("job_name") == self.removed_job_name]):
            return jobs
        changed_job = changed_job[0]
        jobs = [job for job in jobs if job.get("job_name") != self.removed_job_name]
        if not self.removed_unit_name:
            return jobs
        # list of scrape jobs not associated with the departing unit
        configs_kept = [
            config
            for config in changed_job["static_configs"]  # type: ignore
            if config.get("labels", {}).get("juju_unit") != self.removed_unit_name
        ]
        if configs_kept:
            changed_job["static_configs"] = configs_kept  # type: ignore
            jobs.append(changed_job)

        return jobs


@dataclass
class AlertRuleContext:
    """Coordinate events for alert rules."""

    removed_group_name: Optional[str] = None
    removed_unit_name: Optional[str] = None
    updated_group: Dict[str, Any] = field(default_factory=dict)

    def get_groups(self, alert_rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        """In the event of alert rule additions, removal, or both, return the resulting list of alert rule groups."""
        if not (groups := _dedupe_list(alert_rules.get("groups", []))):
            return groups

        # Add alert rule groups
        for group in groups:
            if group["name"] == self.updated_group.get("name"):
                group["rules"] = [
                    r for r in group["rules"] if r not in self.updated_group["rules"]
                ]
                group["rules"].extend(self.updated_group["rules"])
        group_names = [g["name"] for g in groups]
        if self.updated_group and self.updated_group.get("name") not in group_names:
            groups.append(self.updated_group)

        # Remove alert rule groups
        if not (
            changed_group := [
                group for group in groups if group["name"] == self.removed_group_name
            ]
        ):
            return groups
        changed_group = changed_group[0]
        groups = [group for group in groups if group["name"] != self.removed_group_name]
        if not self.removed_unit_name:
            return groups
        # list of alert rules not associated with the departing unit
        rules_kept = [
            rule
            for rule in changed_group.get("rules")  # type: ignore
            if rule.get("labels").get("juju_unit") != self.removed_unit_name
        ]
        if rules_kept:
            changed_group["rules"] = rules_kept  # type: ignore
            groups.append(changed_group)

        return groups
