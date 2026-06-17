# OpenBao agent — one-shot render of .env from secret/velaris/env (Group K).
# Run by scripts/secrets-render.sh in a throwaway container sharing the
# server container's network namespace, so 127.0.0.1:8200 is the server.

pid_file        = "/openbao/agent/out/pidfile"
exit_after_auth = true
disable_mlock   = true   # render container runs as the host user, no IPC_LOCK
log_level       = "warn"

vault {
  address = "http://127.0.0.1:8200"
}

auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/openbao/agent/role_id"
      secret_id_file_path                 = "/openbao/agent/secret_id"
      remove_secret_id_file_after_reading = false
    }
  }
}

template {
  destination = "/openbao/agent/out/.env"
  perms       = "0600"
  error_on_missing_key = true
  contents    = <<EOT
# Rendered by OpenBao agent — do not edit by hand.
# Change secrets with ./scripts/secrets-push.sh, then re-render.
{{ with secret "secret/data/velaris/env" }}{{ range $k, $v := .Data.data }}{{ $k }}={{ $v }}
{{ end }}{{ end }}
EOT
}
