# RabbitMQ dashboard lingers on non-leader otelcol unit after relation removal

## Summary

When the `rabbitmq-server:dashboards` ↔ `cos-proxy:dashboards` relation is removed, the `[juju] RabbitMQ` dashboard JSON file persists on the non-leader `opentelemetry-collector` unit's disk, even though it is correctly removed from the relation databag and from the leader unit's disk.

**Root cause**: `_create_dashboard_files` (`src/charm.py:405-433`) is a create-only operation — it writes files but never removes stale files that no longer correspond to any active relation. The only cleanup path is `_delete_existing_dashboard_files` (`src/charm.py:356-360`) called from `_dashboards_relation_broken` (`src/charm.py:459-461`), which does a blanket delete of all `*.json` files. On the leader, this runs correctly and `_on_refresh` re-creates only the surviving dashboards. On the non-leader unit, event ordering allows a stale write to reoccur after the cleanup, or the cleanup races with a queued `_create_dashboard_files` call from a prior `relation_changed` event.

**Impact**: Low severity. The `COSAgentProvider` writes dashboards to unit-level databags which any unit can update, but the `GrafanaDashboardAggregator` — which manages the `downstream-grafana-dashboard` path to Grafana — is leader-gated (`lib/charms/grafana_k8s/v0/grafana_dashboard.py:1808,1840,1861`). Only the leader unit's data reaches Grafana, so the stale file on disk is cosmetically incorrect but does not cause incorrect dashboards to be pushed downstream.

**Missing handler**: There is no `relation-departed` handler for the `dashboards` endpoint (`src/charm.py:149-162` only observes `relation_joined`, `relation_changed`, `relation_broken`). When one of multiple dashboard providers departs, stale files for that provider remain on disk with no cleanup path until the final `relation_broken` fires.

## Reproducer

1. Deploy the cos-proxy operator with an opentelemetry-collector subordinate and a RabbitMQ application:
   ```
   juju deploy cos-proxy
   juju deploy opentelemetry-collector --channel dev/edge
   juju deploy rabbitmq-server --channel latest/edge
   ```

2. Relate RabbitMQ to cos-proxy for dashboards, scrape targets, and alert rules:
   ```
   juju relate rabbitmq-server:dashboards cos-proxy:dashboards
   juju relate rabbitmq-server:prometheus-rules cos-proxy:prometheus-rules
   juju relate rabbitmq-server:scrape cos-proxy:prometheus-target
   ```

3. Relate cos-proxy to opentelemetry-collector via the cos-agent relation:
   ```
   juju relate cos-proxy:cos-agent opentelemetry-collector:cos-agent
   ```

4. Relate opentelemetry-collector as a subordinate to RabbitMQ:
   ```
   juju relate rabbitmq-server:juju-info opentelemetry-collector:juju-info
   ```

5. Wait for all units to settle. Verify 11 dashboards on the leader unit disk (3 bundled + 7 from cos-proxy + 1 from RabbitMQ) and 8 on the non-leader unit disk.

6. Remove the RabbitMQ → cos-proxy dashboards relation:
   ```
   juju remove-relation rabbitmq-server:dashboards cos-proxy:dashboards
   ```

7. Verify: the leader unit's disk should show 10 dashboards (3 bundled + 7 from cos-proxy, no RabbitMQ). The non-leader unit's disk will still show 8 dashboards including `juju_[juju]_rabbitmq-cos-agent-cos-proxy-1.json`.

## Environment

- **Cos-proxy operator**: commit `64e6b819e3dd4ec401659c5403325485ccdf0be6`
- **opentelemetry-collector**: `dev/edge` rev 342, version 0.130.0
- **rabbitmq-server**: `latest/edge` rev 303, version 3.12.1
- **Juju**: 3.6.23
- **Cloud**: LXD (localhost), `ubuntu@24.04` base
- **SLA**: unsupported
