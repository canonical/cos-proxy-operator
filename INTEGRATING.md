## Topology

Below diagrams illustrate how to integrate `cos-proxy` with `cos-lite` charms to monitor legacy (LMA) charms.

### Integration with COS Proxy

In the topology diagrams:

1. **Direct Integration**: The first diagram shows `cos-proxy` directly interacting with offered SAAS relations from `cos-lite`.


2. **Over the COS Agent Interface**: The second diagram demonstrates integration via the grafana-agent's `cos-agent` interface, which receives metrics and dashboards configurations from `cos-proxy`. `grafana-agent` then sends remote writes to `Prometheus` and dashboards configuration data to `Grafana` via the offered SAAS relations from `cos-lite`.

**Important Note**: Applications should not be directly linked to `cos-proxy` via the `juju-info` relation. Instead, they should interact with a charm similar to`telegraf` for metric collection, which is then handled by `cos-proxy`.

## Relating directly to cos-proxy

```mermaid
graph TD

    %% COS Model Subgraph
    subgraph cos-k8s["COS K8s Model"]
        direction TB
        prometheus["Prometheus-k8s"]
        grafana["Grafana-k8s"]
    end



    %% Reactive Model Subgraph
    subgraph reactive-model["Machine Model"]
        direction TB
        ubuntu["Ubuntu"]
        telegraf["Telegraf"]
        cos_proxy["COS Proxy"]


        %% Ubuntu and Telegraf Relation
        ubuntu --- |juju-info| telegraf

        %% Telegraf to cos-proxy Connections
        telegraf --> |prometheus-client| cos_proxy
        telegraf --> |prometheus-rules| cos_proxy
        telegraf --> |dashboards| cos_proxy

        %% SAAS Subgraph
        subgraph SAAS["SAAS"]
            GSAAS["Grafana"]
            PSAAS["Prometheus"]

            %% SAAS to COS Model Connections
            GSAAS --- |grafana-dashboard| grafana
            PSAAS --- |metrics-endpoint| prometheus
        end

        %% cos-proxy to SAAS Connections
        cos_proxy --> |downstream-prometheus-scrape| PSAAS
        cos_proxy --> |downstream-grafana-dashboard| GSAAS

    end
```

## Relating over the cos-agent interface

```mermaid
graph TD
    %% COS Lite K8s Subgraph
    subgraph cos-k8s["COS K8s Model"]
        prometheus["Prometheus"]
        grafana["Grafana"]
        

    end

    %% Machine Model Subgraph
    subgraph reactive-model["Machine Model"]
        ubuntu["Ubuntu"]
        telegraf["Telegraf"]
        cos_proxy["COS Proxy"]
        grafana-agent["Grafana Agent"]

        %% Connections
        ubuntu --> |juju_info| telegraf
        telegraf --> |prometheus_client| cos_proxy
        telegraf --> |prometheus_rules| cos_proxy
        telegraf --> |dashboards| cos_proxy

        cos_proxy --> |cos_agent| grafana-agent

        %% cos-proxy to SAAS Connections
        grafana-agent --> |send-remote-write| PSAAS
        grafana-agent --> |grafana-cloud-config| GSAAS

        %% SAAS Subgraph
        subgraph SAAS["SAAS"]
            GSAAS["Grafana"]
            PSAAS["Prometheus"]

            %% SAAS to COS Model Connections
            GSAAS --- |grafana-dashboard| grafana
            PSAAS --- |metrics-endpoint| prometheus
        end
    end
```