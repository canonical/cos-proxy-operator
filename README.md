# LMA Proxy charm

This Juju machine charm that provides a single integration point in the machine world with the Kubernetes-based [LMA bundle](https://charmhub.io/lma-light).

This charm is designed to be easy to integrate in bundles and Juju-driven appliances, and reduce the amount of setup needed to integrate with the Kubernetes-based LMA to just connect the LMA Proxy charm with it.

## Deployment

The lma-proxy charm is used as a connector between a Juju model hosting
applications on machines, and LMA charms running within Kubernetes.
In the following example our machine charms will be running on an OpenStack
cloud, and the Kubernetes is Microk8s running on a separate host.  There must be
network connectivity from each of the endpoints to the Juju controller.

For example, we have two models.  One, named 'reactive', hosting machine charms
running on OpenStack.  There is a Telegraf application, cs:telegraf, collecting
metrics from units, and we wish to relate that to Prometheus and Grafana running
in another model named lma, running Kubernetes.

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
juju add-model lma microk8s-cluster
```

Next we'll deploy an example application on the `reactive` model:

```
juju deploy -m reactive cs:ubuntu --series focal -n 3
juju deploy -m reactive cs:telegraf
juju relate -m reactive telegraf:juju-info ubuntu:juju-info
```

Deploy Prometheus and Grafana into the Kubernetes model, in this case I specify an
inbound IP address for the ingress because I deployed microk8s with ingress and metallb
enabled, and added the extra IP address to my host:

```
juju deploy -m lma prometheus-k8s prometheus
juju deploy -m lma grafana-k8s grafana
juju deploy -m lma nginx-ingress-integrator
juju config -m lma nginx-ingress-integrator kubernetes-service-loadbalancer-ip=10.5.21.1
juju config -m lma nginx-ingress-integrator juju-external-hostname=prometheus-k8s.juju
juju relate -m lma prometheus:ingress nginx-ingress-integrator:ingress
```

To relate Telegraf to Prometheus in order to add scrape targets and alerting
rules, we must use a cross model relation.

Offer the relation in the lma model:

```
juju offer lma.prometheus:prometheus_scrape
```

Deploy the lma-proxy charm in a new machine unit on the target model:

```
juju deploy -m reactive lma-proxy  # or ./lma-proxy_ubuntu-20.04-amd64.charm
juju relate -m reactive telegraf:prometheus-client lma-proxy:prometheus-target
juju relate -m reactive telegraf:prometheus-rules lma-proxy:prometheus-rules
```

Add the cross model relation:

```
juju consume -m reactive lma.prometheus
juju relate -m reactive prometheus lma-proxy:downstream-prometheus-scrape
```

Now we can do the same for Grafana

```
juju offer lma.grafana:grafana_dashboard
```

juju relate -m reactive telegraf:dashboards lma-proxy:dashboards
```

Add the cross model relation:

```
juju consume -m reactive lma.prometheus
juju consume -m reactive lma.grafana
juju relate -m reactive prometheus lma-proxy:downstream-prometheus-scrape
juju relate -m reactive grafana lma-proxy:downstream-grafana-dashboards
```

## Appendix: notes for microk8s deployments

After installing microk8s and setting it up per the documentation, to allow
external hosts to access the Prometheus service there are a couple of extra
steps.  These are in the microk8s documentation but a very brief summary is
provided here for convenience.  Run these steps before creating the lma model.

The IP address example used here is 10.5.21.1/32 which is accessible on ens3.

```
$ export netdevice=ens3
$ export ipaddress=10.5.21.1/32
$ sudo ip a add $ipaddress $netdevice
$ microk8s enable ingress
$ microk8s enable metallb

Enabling MetalLB
Enter each IP address range delimited by comma (e.g. '10.64.140.43-10.64.140.49,192.168.0.105-192.168.0.111'): 10.5.21.1-10.5.21.250
Applying Metallb manifest
namespace/metallb-system created
secret/memberlist created
Warning: policy/v1beta1 PodSecurityPolicy is deprecated in v1.21+, unavailable in v1.25+
podsecuritypolicy.policy/controller created
podsecuritypolicy.policy/speaker created
serviceaccount/controller created
serviceaccount/speaker created
clusterrole.rbac.authorization.k8s.io/metallb-system:controller created
clusterrole.rbac.authorization.k8s.io/metallb-system:speaker created
role.rbac.authorization.k8s.io/config-watcher created
role.rbac.authorization.k8s.io/pod-lister created
clusterrolebinding.rbac.authorization.k8s.io/metallb-system:controller created
clusterrolebinding.rbac.authorization.k8s.io/metallb-system:speaker created
rolebinding.rbac.authorization.k8s.io/config-watcher created
rolebinding.rbac.authorization.k8s.io/pod-lister created
daemonset.apps/speaker created
deployment.apps/controller created
configmap/config created
MetalLB is enabled

$ cat > ingress-service.yaml <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ingress
  namespace: ingress
spec:
  selector:
    name: nginx-ingress-microk8s
  type: LoadBalancer
  # loadBalancerIP is optional. MetalLB will automatically allocate an IP
  # from its pool if not specified. You can also specify one manually.
  loadBalancerIP: 10.5.21.1
  ports:
    - name: http
      protocol: TCP
      port: 80
      targetPort: 80
    - name: https
      protocol: TCP
      port: 443
      targetPort: 443
EOF

$ kubectl apply -f ingress-service.yaml
```
