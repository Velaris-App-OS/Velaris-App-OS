# HELIX Helm Chart

```bash
# Install dependencies
helm dep update deploy/helm/helix

# Deploy
helm install helix deploy/helm/helix -n helix --create-namespace

# Upgrade
helm upgrade helix deploy/helm/helix -n helix

# Uninstall
helm uninstall helix -n helix
```

## KEDA autoscaling (optional)

To scale the Temporal worker on queue depth:

```bash
# 1. Install KEDA (once per cluster)
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda -n keda --create-namespace

# 2. Enable in values.yaml
worker.keda.enabled: true
```
