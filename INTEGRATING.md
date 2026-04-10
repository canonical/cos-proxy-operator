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

    %% Machine Model Subgraph
    subgraph machine-model["Machine model"]
        direction TD
        %% SAAS Subgraph
        subgraph SAAS["SAAS"]
            GSAAS["grafana-k8s"]
            PSAAS["prometheus-k8s"]
        end

        subgraph machine-1["Machine 1"]
            ubuntu["ubuntu/0"]
            
            subgraph subordinates-1["Subordinates"]
                nrpe["nrpe/0"]
                telegraf["telegraf/0"]
            end
        end
    
        %% cos-proxy Subgraph
        subgraph machine-0["Machine 0"]
            cos_proxy["cos-proxy"]
        end
    end

    %% COS Lite K8s Subgraph
    subgraph cos-k8s["COS K8s Model"]
        prometheus["prometheus-k8s"]
        grafana["grafana-k8s"]
    end

    %% Connections
    ubuntu --- |general-info| nrpe
    ubuntu --- |juju-info| telegraf
    telegraf --- |prometheus_client| cos_proxy
    telegraf --- |prometheus_rules| cos_proxy
    telegraf --- |dashboards| cos_proxy
    nrpe --- |monitors| cos_proxy

    %% cos-proxy to SAAS Connections
    cos_proxy --- |downstream-prometheus-scrape| PSAAS
    cos_proxy --- |downstream-grafana-dashboard| GSAAS

    %% SAAS to COS Model Connections
    GSAAS --- |grafana-dashboard| grafana
    PSAAS --- |metrics-endpoint| prometheus
```

## Relating over the cos-agent interface

In this topology, integration is achieved via the `grafana-agent`'s `cos-agent` interface. Here, `cos-proxy` sends metrics and dashboard configurations to `grafana-agent`, which then forwards these to `Prometheus` and `Grafana` via the offered SAAS relations from `cos-lite`.


```mermaid
graph TD

    %% Machine Model Subgraph
    subgraph machine-model["Machine model"]
        direction TD
        %% SAAS Subgraph
        subgraph SAAS["SAAS"]
            GSAAS["grafana-k8s"]
            PSAAS["prometheus-k8s"]
        end

        subgraph machine-1["Machine 1"]
            ubuntu["ubuntu/0"]
            
            subgraph subordinates-1["Subordinates"]
                nrpe["nrpe/0"]
                telegraf["telegraf/0"]
            end
        end
    
        %% cos-proxy Subgraph
        subgraph machine-0["Machine 0"]
            cos_proxy["cos-proxy"]

            subgraph subordinates-0["Subordinates"]
                direction LR
                grafana-agent["otelcol
                (or grafana-agent)"]
            end
        end
    end

    %% COS Lite K8s Subgraph
    subgraph cos-k8s["COS K8s Model"]
        prometheus["prometheus-k8s"]
        grafana["grafana-k8s"]
    end

    %% Connections
    ubuntu --- |general-info| nrpe
    ubuntu --- |juju-info| telegraf
    telegraf --- |prometheus_client| cos_proxy
    telegraf --- |prometheus_rules| cos_proxy
    telegraf --- |dashboards| cos_proxy
    nrpe --- |monitors| cos_proxy

    cos_proxy --- |cos_agent| grafana-agent

    %% cos-proxy to SAAS Connections
    grafana-agent --- |send-remote-write| PSAAS
    grafana-agent --- |grafana-dashboards-provider| GSAAS

    %% SAAS to COS Model Connections
    GSAAS --- |grafana-dashboard| grafana
    PSAAS --- |receive-remote-write| prometheus
```