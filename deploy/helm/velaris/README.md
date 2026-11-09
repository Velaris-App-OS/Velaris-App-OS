# Velaris Helm Chart

```bash
# Install dependencies
helm dep update deploy/helm/velaris

# Deploy
helm install helix deploy/helm/velaris -n helix --create-namespace

# Upgrade
helm upgrade helix deploy/helm/velaris -n helix

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

## Ingress access logs — mask `token=`

Browsers cannot set headers on a WebSocket, so Studio's live sockets
(`/api/v1/realtime/ws`, `/api/v1/hxstream/ws`) carry the access token in the
query string, and a portal document download carries a signed `grant=`. The
bundled gateway blanks these in its log (`log_format velaris_combined`); on
Kubernetes the ingress controller writes the log, and its default format
records the full request line. With ingress-nginx, set a `log-format-upstream`
in the controller's ConfigMap that logs `$uri` (no query string) instead of
`$request`.

## Token signing secret

The chart generates `VELARIS_CASE_AUTH_SECRET` once at install (Secret
`<release>-helix-auth`, kept across upgrades and `helm uninstall`) and gives it to
every service. To manage it yourself, create a Secret holding it under
`auth-secret` and set `authSecret.existingSecret`. Never change it on a running
install: everyone is signed out and every encrypted field becomes unreadable.
