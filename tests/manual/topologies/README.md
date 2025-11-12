## Background
Below are a couple of scenarios to test deployment topologies introduced in `INTEGRATING.md`.


### Relating directly to cos-proxy
- Deploy [`cos-lite`](https://charmhub.io/topics/canonical-observability-stack) in a k8s model
  - Offer `grafana:grafana-dashboard` and `prometheus:metrics-endpoint` endpoints
- Deploy [`cos-proxy-SAAS-bundle`](cos-proxy-SAAS-bundle.yaml) in a new lxd model.

## Relating over the cos-agent interface
- Deploy [`cos-lite`](https://charmhub.io/topics/canonical-observability-stack) in a k8s model
  - Offer `grafana:grafana-dashboard` and `prometheus:receive-remote-write` endpoints
- Deploy [`cos-proxy-gagent-bundle`](cos-proxy-gagent-bundle.yaml) in a new lxd model.

### Verify
- Make sure rule files are available in prometheus:
  - relation data: `juju show-unit prometheus/0`
  - on disk: `juju ssh --container prometheus prometheus/0 ls /etc/prometheus/rules`
  - via http api: `curl x.x.x.x:9090/api/v1/rules | jq`
- Verify that dashboards are available in grafana:
  - relation data: `juju show-unit grafana/0`
  - via grafana dashboard: `x.x.x.x/cos-grafana/dashboards`
