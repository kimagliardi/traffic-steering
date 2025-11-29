# Prometheus (minimal)

This chart deploys a minimal Prometheus server configured to scrape the Kubernetes API proxy for node cAdvisor metrics.

It is intentionally small and intended for development/test use. By default it binds the Prometheus ServiceAccount to cluster-admin for convenience in this environment — change the RBAC to a finer role for production.

To install into the `free5gc` namespace:

```bash
cd free5gc-helm/charts/prometheus
microk8s helm3 install prometheus . -n free5gc --create-namespace
```

The chart ships a `prometheus.yml` with a single `kubernetes-nodes-cadvisor` scrape config matching the project request.
