# LMA Proxy charm

This Juju machine charm that provides a single integration point in the machine world with the Kubernetes-based [LMA bundle](https://charmhub.io/lma-light).

This charm is designed to be easy to integrate in bundles and Juju-driven appliances, and reduce the amount of setup needed to integrate with the Kubernetes-based LMA to just connect the LMA Proxy charm with it.

## Deployment

For example, we have two models.  One, named 'scrapeme', hosting machine charms
running on OpenStack.  There is a Telegraf application, cs:telegraf, collecting
metrics from units, and we wish to relate that to Prometheus running in another
model named lma, running Kubernetes.

First, we deploy the lma-proxy charm in a new machine unit on the target model:

```
juju deploy lma-proxy
juju relate telegraf:prometheus-client lma-proxy:prometheus-target

```

We then offer the cross model relation from Prometheus:

```
juju switch lma
juju offer prometheus-k8s:monitoring
```

Then we consume the relation on the target model side:

```
juju switch scrapeme
juju consume lma.prometheus-k8s
juju relate prometheus-k8s lma-proxy:upstream-prometheus-scrape
```
