## Background

The diagrams below illustrate how to integrate `cos-proxy` with `cos-lite` charms to monitor legacy (LMA) charms. `cos-proxy` serves as an intermediary solution, helping to bridge the gap between legacy charms and modern `cos-lite`. However, it's essential to note that `cos-proxy` is a **transitional** solution, and whenever possible, you should directly use `grafana-agent`. For more information on integrating with `grafana-agent`, refer to the [gagent INTEGRATING documentation](https://github.com/canonical/grafana-agent-operator/blob/main/INTEGRATING.md).

Please refer to the [`juju exported bundles`](tests/manual/topologies/README.md) to verify the deployment of the below topologies in a test environment.

**Important Note**: Applications should not be directly linked to `cos-proxy` via the `juju-info` relation. Instead, they should interact with a charm similar to`telegraf` for metric collection, which is then handled by `cos-proxy`.

## Relating directly to cos-proxy

Diagram below shows `cos-proxy` directly interacting with offered SAAS relations from `cos-lite`.

This topology is **discouraged** in favor of a more modern approach. Whenever possible, use the `grafana-agent` charm if your application supports the `cos-agent` interface. `grafana-agent` provides a more direct integration path.

If your charm does not support the `cos-agent` interface, consider relating `cos-proxy` to `grafana-agent` as explained in the second graph. 

```mermaid
graph TD

    %% COS Model Subgraph
    subgraph cos-k8s["COS K8s Model"]
        direction TB
        prometheus["prometheus-k8s"]
        grafana["grafana-k8s"]
    end



    %% Reactive Model Subgraph
    subgraph reactive-model["Machine Model"]
        direction TB
        ubuntu["ubuntu"]
        telegraf["telegraf"]
        cos_proxy["cos-proxy"]


        %% Ubuntu and Telegraf Relation
        ubuntu --- |juju-info| telegraf

        %% Telegraf to cos-proxy Connections
        telegraf --> |prometheus-client| cos_proxy
        telegraf --> |prometheus-rules| cos_proxy
        telegraf --> |dashboards| cos_proxy

        %% SAAS Subgraph
        subgraph SAAS["SAAS"]
            GSAAS["grafana-k8s"]
            PSAAS["prometheus-k8s"]

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

In this topology, integration is achieved via the `grafana-agent`'s `cos-agent` interface. Here, `cos-proxy` sends metrics and dashboard configurations to `grafana-agent`, which then forwards these to `Prometheus` and `Grafana` via the offered SAAS relations from `cos-lite`.


```mermaid
graph TD
    %% COS Lite K8s Subgraph
    subgraph cos-k8s["COS K8s Model"]
        prometheus["prometheus-k8s"]
        grafana["grafana-k8s"]
        

    end

    %% Machine Model Subgraph
    subgraph reactive-model["Machine Model"]
        ubuntu["ubuntu"]
        telegraf["telegraf"]
        cos_proxy["cos-proxy"]
        grafana-agent["grafana-agent"]

        %% Connections
        ubuntu --> |juju_info| telegraf
        telegraf --> |prometheus_client| cos_proxy
        telegraf --> |prometheus_rules| cos_proxy
        telegraf --> |dashboards| cos_proxy

        cos_proxy --> |cos_agent| grafana-agent

        %% cos-proxy to SAAS Connections
        grafana-agent --> |send-remote-write| PSAAS
        grafana-agent --> |grafana-dashboards-provider| GSAAS

        %% SAAS Subgraph
        subgraph SAAS["SAAS"]
            GSAAS["grafana-k8s"]
            PSAAS["prometheus-k8s"]

            %% SAAS to COS Model Connections
            GSAAS --- |grafana-dashboard| grafana
            PSAAS --- |receive-remote-write| prometheus
        end
    end
```