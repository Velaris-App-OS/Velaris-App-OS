# OpenBao server config — single node, integrated (raft) storage.
# Listens on 8200 INSIDE the container; the host maps it to 8350
# (case-service owns host 8200). TLS is terminated by the deployment's
# reverse proxy in production; on a single host the listener is loopback-
# only via the compose port binding (127.0.0.1:8350).
#
# Storage moved from "file" to "raft" in the November 2026 release: OpenBao
# 2.7 no longer has the file backend. An install that still holds file storage
# is migrated once, offline, before OpenBao starts
# (scripts/openbao-storage-migrate.sh). The raft data lives in the
# `openbao-file` volume, mounted at /openbao/file, which the image's entrypoint
# keeps owned by the openbao user.

ui = false

storage "raft" {
  path    = "/openbao/file"
  node_id = "velaris-1"
}

# Required by raft. Inside the container only; neither port 8201 nor the
# cluster listener is published.
api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"

# OpenBao recommends disabling mlock with integrated storage: raft memory-maps
# its database, and mlock would pin that whole file in RAM.
disable_mlock = true

listener "tcp" {
  address     = "0.0.0.0:8200"
  # Raft opens a cluster listener (file storage never did). One node needs no
  # peers, so it is bound to the container's own loopback: nothing on the
  # compose network can reach it.
  cluster_address = "127.0.0.1:8201"
  tls_disable = true
}

# Later: KMS auto-unseal (seal "awskms" / "azurekeyvault" / "gcpckms") instead
# of the local keyfile.
