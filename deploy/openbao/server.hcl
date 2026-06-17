# OpenBao server config — Group K Tier 1 (single node, file storage).
# Listens on 8200 INSIDE the container; the host maps it to 8300
# (case-service owns host 8200). TLS is terminated by the deployment's
# reverse proxy in production; on a single host the listener is loopback-
# only via the compose port binding (127.0.0.1:8300).

ui = false

storage "file" {
  path = "/openbao/data"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

# Tier 2: replace file storage + manual keyfile unseal with raft + KMS
# auto-unseal (seal "awskms" / "azurekeyvault" / "gcpckms").
