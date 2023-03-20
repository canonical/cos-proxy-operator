# COS Proxy charm

This Juju machine charm that provides a single integration point in the machine world
with the Kubernetes-based [COS bundle](https://charmhub.io/cos-lite).

This charm is designed to be easy to integrate in bundles and Juju-driven appliances,
and reduce the amount of setup needed to integrate with the Kubernetes-based COS to
just connect the COS Proxy charm with it.

Proxying support is provided for:

* Prometheus
* Grafana Dashboards
* NRPE (through [nrpe_exporter](https://github.com/canonical/nrpe_exporter), which sends
  NRPE results to Prometheus)

## Deployment

The cos-proxy charm is used as a connector between a Juju model hosting
applications on machines, and COS charms running within Kubernetes.
In the following example our machine charms will be running on an OpenStack
cloud, and the Kubernetes is Microk8s running on a separate host.  There must be
network connectivity from each of the endpoints to the Juju controller.

For example, we have two models.  One, named 'reactive', hosting machine charms
running on OpenStack.  There is a Telegraf application, cs:telegraf, collecting
metrics from units, and we wish to relate that to Prometheus and Grafana running
in another model named cos, running Kubernetes.

If you already have a working COS Lite deployment, you can skip creating another
one, as well as the steps where you would deploy the COS Lite components one by one.

Here's the steps to create the models:

```
$ juju clouds
Only clouds with registered credentials are shown.
There are more clouds, use --all to see them.

Clouds available on the controller:
Cloud             Regions  Default      Type
microk8s-cluster  1        localhost    k8s
serverstack       1        serverstack  openstack

juju add-model reactive serverstack
juju add-model cos microk8s-cluster
```

Next we'll deploy an example application on the `reactive` model, which should be present
on a single Juju controller which manages both microk8s and a reactive model:

```
juju deploy -m cos prometheus-k8s
juju deploy -m cos grafana-k8s
juju deploy -m reactive cs:ubuntu --series focal -n 3
juju deploy -m reactive cs:telegraf
juju relate -m reactive telegraf:juju-info ubuntu:juju-info
```

To relate Telegraf to Prometheus in order to add scrape targets and alerting
rules, we must use a cross model relation.

Offer the relation in the cos model:

```
juju offer microk8s-cluster:cos.prometheus-k8s:metrics-endpoint
```

Deploy the cos-proxy charm in a new machine unit on the target model:

```
juju deploy -m reactive cos-proxy  # or ./cos-proxy_ubuntu-20.04-amd64.charm
juju relate -m reactive telegraf:prometheus-client cos-proxy:prometheus-target
juju relate -m reactive telegraf:prometheus-rules cos-proxy:prometheus-rules
```

Add the cross model relation:

```
juju consume -m reactive microk8s-cluster:cos.prometheus-k8s
juju relate -m reactive prometheus-k8s cos-proxy:downstream-prometheus-scrape
```

Now we can do the same for Grafana

```
juju offer microk8s-cluster:cos.grafana:grafana-dashboard
juju relate -m reactive telegraf:dashboards cos-proxy:dashboards
```

Add the cross model relation:

```
juju consume -m reactive microk8s-cluster:cos.prometheus-k8s
juju consume -m reactive microk8s-cluster:cos.grafana
juju relate -m reactive prometheus-k8s cos-proxy:downstream-prometheus-scrape
juju relate -m reactive grafana cos-proxy:downstream-grafana-dashboard
```

A complete set of relations on the from the consuming model will appear as:

```
Model       Controller        Cloud/Region         Version  SLA          Timestamp
reactive    overlord          localhost/localhost  3.0.3    unsupported  20:40:45+01:00

SAAS     Status  Store               URL
grafana  active  microk8s            admin/cos.grafana
loki     active  microk8s            admin/cos.loki
metrics  active  microk8s            admin/cos.metrics

App        Version  Status  Scale  Charm      Channel  Rev  Exposed  Message
cos-proxy  n/a      active      1  cos-proxy  edge      15  no       
filebeat   6.8.23   active      1  filebeat   stable    49  no       Filebeat ready.
nrpe                active      1  nrpe       stable    97  no       Ready
telegraf            active      1  telegraf   stable    65  no       Monitoring ubuntu/0 (source version/commit 23.01)
ubuntu     20.04    active      1  ubuntu     stable    21  no       

Unit           Workload  Agent  Machine  Public address  Ports          Message
cos-proxy/0*   active    idle   1        10.218.235.237                 
ubuntu/0*      active    idle   0        10.218.235.198                 
  filebeat/0*  active    idle            10.218.235.198                 Filebeat ready.
  nrpe/0*      active    idle            10.218.235.198  icmp,5666/tcp  Ready
  telegraf/0*  active    idle            10.218.235.198  9103/tcp       Monitoring ubuntu/0 (source version/commit 23.01)

Machine  State    Address         Inst id        Series  AZ  Message
0        started  10.218.235.198  juju-732594-0  focal       Running
1        started  10.218.235.237  juju-732594-1  focal       Running
```

## NRPE Exporting
NRPE targets may appear on multiple relations. To capture all jobs, `cos-proxy` should be related to
**BOTH** an existing reactive NRPE subordinate charm, as well as the application which that charm is subordinated to,
as the `monitors` interface may appear on either, with the principal charm providing "host-level" checks, and
the subordinate `nrpe` providing application-level ones.
