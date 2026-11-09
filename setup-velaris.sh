#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS One-Time Setup Script
#  Run once after a fresh OS install. Safe to re-run (idempotent).
#
#  Usage:  chmod +x setup-velaris.sh && ./setup-velaris.sh
#          ./setup-velaris.sh --verbose   # stream all build output
#          ./setup-velaris.sh --with-key  # prompt for a product key during setup
#
#          ./setup-velaris.sh --docker-data-root /home/velaris/docker-data
#          ./setup-velaris.sh --docker-data-root default   # keep /var/lib/docker
#            Where Docker stores images/volumes. Step 2b offers this
#            interactively; the flag (or VELARIS_DOCKER_DATA_ROOT) answers it
#            up front for unattended installs.
#
#  Build output is captured to ~/.local/state/velaris/velaris-setup.log (previous run kept as
#  .log.prev) and the terminal shows a progress bar. On failure the tail of
#  that log is printed automatically.
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# R5c: a server linked as a STANDBY runs only its two replica databases
# (standby-velaris.sh). Starting the platform here would start a second
# PostgreSQL on the replicas' own data. standby.json exists only while linked.
if [ -f "$VELARIS_DIR/.standby/standby.json" ]; then
  echo "✗ This server is linked as a Velaris standby ($VELARIS_DIR/.standby/standby.json)." >&2
  echo "  The platform must not run here while it is one. To use it on its own, unlink it" >&2
  echo "  first: ./standby-velaris.sh unlink   (on the main server)" >&2
  exit 1
fi
# An old main whose standby was promoted: running it again means two servers
# taking writes. demoted.json stays until it is linked as the new main's standby.
if [ -f "$VELARIS_DIR/.standby/demoted.json" ]; then
  echo "✗ This server was replaced by its standby ($VELARIS_DIR/.standby/demoted.json)." >&2
  echo "  Starting it would make two main servers. To bring it back as a standby:" >&2
  echo "  ./standby-velaris.sh retire   then link it from the new main." >&2
  exit 1
fi
# Step 12 (automatic failover): this main stopped itself after losing both the
# witness and its standby — the standby may have taken over. Its agent lifts
# this by itself once the witness confirms this server still holds the lease.
if [ -f "$VELARIS_DIR/.standby/fenced.json" ]; then
  echo "✗ This server stopped itself: it lost both the failover witness and its standby" >&2
  echo "  ($VELARIS_DIR/.standby/fenced.json). The standby may have taken over." >&2
  echo "  Check: ./standby-velaris.sh failover status   (it restarts by itself if it still holds the lease)" >&2
  exit 1
fi
# Scratch files and error logs go to a directory only this user can write. A
# fixed name under /tmp can be pre-created, or pointed somewhere else, by any
# other local user before this script writes to it.
VELARIS_SCRATCH="${VELARIS_SCRATCH:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/velaris}"
if ! { mkdir -p "$VELARIS_SCRATCH" && chmod 700 "$VELARIS_SCRATCH" && [ -w "$VELARIS_SCRATCH" ]; } 2>/dev/null; then
  VELARIS_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/velaris.XXXXXX")"
fi
ERRORS=0

# uv lives in ~/.local/bin. It is added to .bashrc during install, but that
# does not affect THIS shell (or a re-run in the same session), so put it on
# PATH up front — a previously-installed uv was being reported as missing.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac

# ── CLI flags (DB SDK — optional; enable non-interactive DB setup) ────────────
# --database <postgresql|mysql|mariadb>  pick the backend without the interactive prompt
# --db-host/-port/-name/-user/-password   MySQL (BYO) connection details
# --non-interactive               never prompt; use flags + defaults only
DB_BACKEND_ARG=""; DB_HOST_ARG=""; DB_PORT_ARG=""; DB_NAME_ARG=""; DB_USER_ARG=""; DB_PASSWORD_ARG=""; NONINTERACTIVE=0
# --docker-data-root <path|default>  answer Step 2b without prompting
DOCKER_DATA_ROOT_ARG="${VELARIS_DOCKER_DATA_ROOT:-}"
# --backup-recipient <age1…>  encrypt backups to this public key; no key pair is
#                             generated and nothing private is written (unattended installs)
BACKUP_RECIPIENT_ARG=""
# The parse loop below consumes $@, so keep a copy: it is needed for the
# docker-group re-exec and for the --with-key check near the end.
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --database)        DB_BACKEND_ARG="${2:-}"; shift 2 ;;
    --database=*)      DB_BACKEND_ARG="${1#*=}"; shift ;;
    --db-host)         DB_HOST_ARG="${2:-}"; shift 2 ;;
    --db-host=*)       DB_HOST_ARG="${1#*=}"; shift ;;
    --db-port)         DB_PORT_ARG="${2:-}"; shift 2 ;;
    --db-port=*)       DB_PORT_ARG="${1#*=}"; shift ;;
    --db-name)         DB_NAME_ARG="${2:-}"; shift 2 ;;
    --db-name=*)       DB_NAME_ARG="${1#*=}"; shift ;;
    --db-user)         DB_USER_ARG="${2:-}"; shift 2 ;;
    --db-user=*)       DB_USER_ARG="${1#*=}"; shift ;;
    --db-password)     DB_PASSWORD_ARG="${2:-}"; shift 2 ;;
    --db-password=*)   DB_PASSWORD_ARG="${1#*=}"; shift ;;
    --non-interactive) NONINTERACTIVE=1; shift ;;
    --docker-data-root)   DOCKER_DATA_ROOT_ARG="${2:-}"; shift 2 ;;
    --docker-data-root=*) DOCKER_DATA_ROOT_ARG="${1#*=}"; shift ;;
    --backup-recipient)   BACKUP_RECIPIENT_ARG="${2:-}"; shift 2 ;;
    --backup-recipient=*) BACKUP_RECIPIENT_ARG="${1#*=}"; shift ;;
    *) shift ;;
  esac
done
# MySQL-only selections (kept empty for postgresql so `set -u` is satisfied everywhere)
SYSTEMD_MANAGED=0
SEL_BACKEND=""; SEL_DB_HOST=""; SEL_DB_PORT=""; SEL_DB_NAME=""; SEL_DB_USER=""; SEL_DB_PASSWORD=""

red()    { echo -e "\033[0;31m$*\033[0m"; }
green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
bold()   { echo -e "\033[1m$*\033[0m"; }

fail()   { red "  ✗ $*"; ERRORS=$((ERRORS + 1)); }
ok()     { green "  ✓ $*"; }
warn()   { yellow "  ⚠ $*"; }

# ── Progress display ──────────────────────────────────────────────
# Long build output (uv, npm, docker) goes to a log; the terminal keeps a bar.
# apt is deliberately left VISIBLE with its own fancy progress: it is the part
# an operator most expects to watch, and it draws its own bar anyway.
# --verbose streams everything to the terminal as before.
VERBOSE=0
for _a in ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}; do
  case "$_a" in -v|--verbose) VERBOSE=1 ;; esac
done
SETUP_LOG="${VELARIS_SETUP_LOG:-$VELARIS_SCRATCH/velaris-setup.log}"
# Keep the previous run: a log truncated on every start is a log that is empty
# exactly when someone needs it.
[ -f "$SETUP_LOG" ] && mv -f "$SETUP_LOG" "$SETUP_LOG.prev" 2>/dev/null || true
: > "$SETUP_LOG" 2>/dev/null || SETUP_LOG=/dev/null
echo "Velaris setup — $(date -Is)" >> "$SETUP_LOG"

BAR_WIDTH=28
_bar_active=0
draw_bar() {                       # draw_bar <current> <total> <label>
  [ "$VERBOSE" = "1" ] && return 0
  [ -t 1 ] || return 0             # no bar when piped to a file or CI
  local cur="$1" total="$2" label="$3" filled empty pct
  [ "$total" -gt 0 ] 2>/dev/null || return 0
  pct=$(( cur * 100 / total ))
  filled=$(( cur * BAR_WIDTH / total ))
  empty=$(( BAR_WIDTH - filled ))
  printf "\r\033[K\033[0;36m[%s%s]\033[0m %3d%%  %s" \
    "$(printf '█%.0s' $(seq 1 $filled 2>/dev/null))" \
    "$(printf '░%.0s' $(seq 1 $empty 2>/dev/null))" \
    "$pct" "$label"
  _bar_active=1
}
clear_bar() { [ "$_bar_active" = "1" ] && { printf "\r\033[K"; _bar_active=0; }; return 0; }

# step "Step 4/9 — Doing the thing..." — the bar reads the counter from the label.
step() {
  clear_bar
  echo ""
  bold "▶ $*"
  local cur total
  if [[ "$*" =~ Step[[:space:]]+([0-9]+)[a-z]?/([0-9]+) ]]; then
    cur="${BASH_REMATCH[1]}"; total="${BASH_REMATCH[2]}"
    draw_bar "$cur" "$total" "${*#Step */* — }"
  fi
}

# quiet <label> <command...> — run with output captured unless --verbose.
# The command's exit status is preserved so callers can still branch on it.
quiet() {
  local label="$1"; shift
  if [ "$VERBOSE" = "1" ]; then
    "$@"
    return $?
  fi
  echo "--- $label: $* ---" >> "$SETUP_LOG"
  "$@" >> "$SETUP_LOG" 2>&1
}

# Anything that dies unexpectedly should hand over the output it hid.
on_error() {
  local code=$?
  clear_bar
  [ "$code" -eq 0 ] && return 0
  red ""
  red "  ✗ Setup failed (exit $code) at line ${1:-?}."
  if [ "$VERBOSE" != "1" ] && [ -s "$SETUP_LOG" ]; then
    yellow "  ── last 25 lines of $SETUP_LOG ─────────────────────"
    tail -25 "$SETUP_LOG" | sed 's/^/  /'
    yellow "  ────────────────────────────────────────────────────"
    echo   "  Full log: $SETUP_LOG   ·   re-run with --verbose to watch live"
    # Only captured commands reach the log. If the step that failed was not one
    # of them, the tail above belongs to an EARLIER step — say so rather than
    # letting it look like the cause.
    yellow "  (If the lines above look unrelated, the failing command was not a"
    yellow "   captured build step — check the line number and re-run --verbose.)"
  fi
}
trap 'on_error $LINENO' ERR
trap clear_bar EXIT

# apt draws its own progress bar; let it, and keep it on screen.
APT_PROGRESS=(-o Dpkg::Progress-Fancy=1)

echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS One-Time Setup           ║"
echo "╚══════════════════════════════════════════╝"

# ── Step 1: Prerequisites ─────────────────────────────────────────
step "Step 1/9 — Checking prerequisites..."

sudo apt-get "${APT_PROGRESS[@]}" update

APT_PACKAGES=()

require_cmd() {
  local cmd="$1"
  local apt_pkg="$2"
  local label="${3:-$cmd}"

  if command -v "$cmd" &>/dev/null; then
    ok "$label found: $(command -v "$cmd")"
  else
    warn "$label not found — will install"
    APT_PACKAGES+=("$apt_pkg")
  fi
}

# Core packages, from the one list `update-velaris.sh` also checks. Two copies
# of this list is how an update ends up demanding a package the installer never
# installs — or the reverse. Step 2b moves Docker's storage with rsync -aHAX,
# which is priority:standard and therefore present on a normal Ubuntu install
# but NOT on minimal or cloud images, so it is in that list too.
# shellcheck source=/dev/null
. "$VELARIS_DIR/deploy/deps/velaris-deps.sh"
while IFS= read -r _dep; do
  [ -n "$_dep" ] || continue
  _cmd="${_dep%%:*}"; _rest="${_dep#*:}"; _pkg="${_rest%%:*}"; _label="${_rest#*:}"
  # Docker is handled by its own block below (engine, compose plugin, daemon and
  # group membership are four separate things), so do not queue it here.
  [ "$_cmd" = "docker" ] && continue
  require_cmd "$_cmd" "$_pkg" "$_label"
done <<SETUP_DEPS_EOF
$VELARIS_REQUIRED_COMMANDS
SETUP_DEPS_EOF

# ask "question" → 0 = yes. --non-interactive and a missing tty both answer yes,
# so an unattended install still completes instead of stalling on a prompt.
ask() {
  if [ "$NONINTERACTIVE" = "1" ] || [ ! -t 0 ]; then
    warn "$1 — yes (non-interactive)"
    return 0
  fi
  local reply
  read -rp "$(yellow "  ? $1 [Y/n] ")" reply || reply=""
  [ -z "$reply" ] || [[ "$reply" =~ ^[Yy] ]]
}

# ── Ports: ask before anything binds ─────────────────────────────
#
# A client's machine is not ours, and every port here was a literal until
# 2026-08-24. The failure without this check is late and confusing: the install
# completes, a service cannot bind, and the operator is left reading logs to
# find out that something unrelated already had 8202.
#
# Checked here — before Docker, the database or any file is touched — so a clash
# costs a question rather than a half-installed platform. Ports already held by
# Velaris are fine: this is the second run of an installer, not a conflict.
step "Step 1b/9 — Checking ports..."
# shellcheck source=/dev/null
. "$VELARIS_DIR/deploy/ports/velaris-ports.sh"

_setup_choose_ports() {
  local attempt
  for attempt in 1 2 3; do
    _velaris_ports_preflight && return 0
    echo ""
    if [ "$NONINTERACTIVE" = "1" ] || [ ! -t 0 ]; then
      # Never silently pick a different port for someone who is not watching:
      # they would end up with a platform on ports their firewall and their
      # documentation do not know about.
      err "Ports above are taken. Set them in .env and re-run, e.g."
      err "  echo 'VELARIS_ANALYTICS_PORT=9202' >> .env"
      return 1
    fi
    local line name var port reply owner
    while IFS= read -r line; do
      name="${line%%:*}"; var="${line##*:}"
      eval "port=\${$var}"
      owner=$(_velaris_port_owner "$port")
      case "$owner" in free|velaris) continue ;; esac
      read -rp "$(yellow "  ? Port for ${name} [${port} is taken, enter another]: ")" reply || reply=""
      [ -n "$reply" ] && eval "$var=\$reply" && export "${var?}"
    done <<PORTS_EOF
$VELARIS_PORT_TABLE
PORTS_EOF
  done
  err "Ports are still not free after three attempts."
  return 1
}

if _setup_choose_ports; then
  ok "Ports available"
else
  exit 1
fi

# ── Docker: engine, compose plugin, daemon, group membership ─────
# All four have to be true before anything else works, and each one used to
# fail separately with a different confusing message.
if command -v docker &>/dev/null; then
  ok "Docker found: $(command -v docker)"
else
  warn "Docker is not installed."
  if ask "Install Docker now (apt: docker.io)?"; then
    sudo apt-get "${APT_PROGRESS[@]}" install -y docker.io || true
    command -v docker &>/dev/null || { warn "Falling back to Docker's official installer..."; curl -fsSL https://get.docker.com | sudo sh; }
    command -v docker &>/dev/null && ok "Docker installed" || fail "Docker install failed"
  else
    fail "Docker is required — https://docs.docker.com/engine/install/ubuntu/"
  fi
fi

if docker compose version &>/dev/null 2>&1; then
  ok "Docker Compose found"
elif command -v docker-compose &>/dev/null; then
  ok "docker-compose (v1) found"
else
  warn "Docker Compose plugin is not installed."
  if ask "Install the Docker Compose plugin now?"; then
    sudo apt-get install -y docker-compose-v2 2>/dev/null \
      || sudo apt-get install -y docker-compose-plugin 2>/dev/null || true
    docker compose version &>/dev/null 2>&1 && ok "Docker Compose installed" || fail "Compose install failed"
  else
    fail "Docker Compose is required"
  fi
fi

if command -v docker &>/dev/null && ! sudo docker info &>/dev/null 2>&1; then
  warn "Docker daemon is not running."
  ask "Start and enable the Docker service?" && sudo systemctl enable --now docker || true
fi

# usermod does not affect the running shell, so re-exec inside the new group
# rather than ending the install with "log out and back in".
if command -v docker &>/dev/null && ! docker info &>/dev/null 2>&1; then
  warn "$USER cannot use the Docker socket (not in the 'docker' group)."
  if ask "Add $USER to the 'docker' group and continue?"; then
    sudo usermod -aG docker "$USER"
    if [ "${VELARIS_SETUP_REEXEC:-0}" != "1" ]; then
      warn "Re-running setup inside the new group..."
      export VELARIS_SETUP_REEXEC=1
      exec sg docker -c "$(printf '%q ' "$0" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"})"
    fi
    docker info &>/dev/null 2>&1 || fail "Still no Docker access — log out and back in, then re-run."
  else
    fail "Docker group membership is required"
  fi
fi

# Install missing apt packages
if [ "${#APT_PACKAGES[@]}" -gt 0 ]; then
  step "Installing missing apt packages..."
  sudo apt-get "${APT_PROGRESS[@]}" install -y "${APT_PACKAGES[@]}"
  ok "Missing apt packages installed"
fi

# Studio is built with Vite, which needs Node 18+. The distro package can be
# older than that, and the resulting build failure names neither Node nor the
# version, so check explicitly.
if command -v node &>/dev/null; then
  NODE_MAJOR="$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1)"
  if [ -n "$NODE_MAJOR" ] && [ "$NODE_MAJOR" -lt 18 ] 2>/dev/null; then
    warn "Node $(node -v) is too old for the Studio build (needs 18+)."
    if ask "Install Node 20 LTS from NodeSource?"; then
      curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null
      sudo apt-get "${APT_PROGRESS[@]}" install -y nodejs
      ok "Node $(node -v) installed"
    fi
  fi
fi

# Install uv if missing
if command -v uv &>/dev/null; then
  ok "uv found: $(command -v uv)"
else
  step "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh

  export PATH="$HOME/.local/bin:$PATH"

  # Persist PATH for future shells
  if ! grep -q 'HOME/.local/bin' "$HOME/.bashrc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  fi

  if command -v uv &>/dev/null; then
    ok "uv installed successfully"
  else
    fail "uv installation failed"
  fi
fi

# Final verification
echo ""
bold "Dependency summary:"

check_cmd() {
  local cmd="$1"
  local label="${2:-$1}"

  if command -v "$cmd" &>/dev/null; then
    ok "$label ready"
  else
    fail "$label still missing"
  fi
}

check_cmd uv       "uv"
check_cmd node     "Node.js"
check_cmd npm      "npm"
check_cmd docker   "Docker"
check_cmd nc       "netcat (nc)"
check_cmd fuser    "fuser (psmisc)"
check_cmd curl     "curl"
check_cmd git      "git"
check_cmd pg_isready "postgresql-client"

# Python 3.12 via uv
if command -v uv &>/dev/null; then
  if uv python find 3.12 &>/dev/null; then
    ok "Python 3.12 available via uv"
  else
    step "Installing Python 3.12 via uv..."
    quiet "uv python install" uv python install 3.12
    ok "Python 3.12 installed"
  fi
fi

[ "$ERRORS" -gt 0 ] && {
  red "Fix the above errors and re-run."
  exit 1
}
# ── Step 1b: Generate infrastructure credentials ──────────────────
step "Generating infrastructure credentials..."

ENV_FILE="$VELARIS_DIR/.env"

# Create .env from .env.example if it doesn't exist yet. Creating it is what
# marks a NEW install: only then may setup switch features on (CLAUDE.md —
# "turning something ON is an operator's decision, never an update's").
NEW_ENV=false
if [ ! -f "$ENV_FILE" ]; then
  cp "$VELARIS_DIR/.env.example" "$ENV_FILE"
  ok "Created .env from .env.example"
  NEW_ENV=true
fi

# ── Database backend selection (DB SDK; set-once at install) ─────────────────
# Source of truth for the runner = velaris.yaml `database:`; the app reads the URL
# from .env. The DB engine is chosen ONCE here — switching later is a data migration
# (HxDBMigrate), never a setup re-run. "Already configured" = .env DATABASE_URL has
# been customized away from the placeholder (the same signal the URL block uses).
_yaml_get() {
  grep -E "^\s*${1}:" "$VELARIS_DIR/velaris.yaml" 2>/dev/null \
    | head -1 | sed -E "s/.*${1}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" | xargs || true
}
DB_CONFIGURED=0
if grep -q "^VELARIS_CASE_DATABASE_URL=" "$ENV_FILE" 2>/dev/null \
   && ! grep -qE "^VELARIS_CASE_DATABASE_URL=\s*$" "$ENV_FILE" 2>/dev/null \
   && ! grep -q   "^VELARIS_CASE_DATABASE_URL=.*REPLACE" "$ENV_FILE" 2>/dev/null; then
  DB_CONFIGURED=1
fi

SEL_BACKEND="$(_yaml_get database)"; SEL_BACKEND="${SEL_BACKEND:-postgresql}"
if [ "$DB_CONFIGURED" = "1" ]; then
  ok "Database already configured ($SEL_BACKEND) — keeping it"
  warn "Switching database engines after install is a migration (HxDBMigrate), not a setup re-run"
  # Normalise DB_FAMILY even on a re-run — the migration/superadmin steps below test it.
  case "$SEL_BACKEND" in
    postgresql) DB_FAMILY=postgresql ;;
    mysql|mariadb) DB_FAMILY=mysql ;;
    *) fail "Unsupported database backend '$SEL_BACKEND' (allowed: postgresql, mysql, mariadb)"; exit 1 ;;
  esac
else
  if [ -n "$DB_BACKEND_ARG" ]; then
    SEL_BACKEND="$DB_BACKEND_ARG"
  elif [ "$NONINTERACTIVE" = "0" ] && [ -t 0 ]; then
    echo ""; bold "  Select the database backend (set once — switching later = HxDBMigrate):"
    echo "    1) postgresql  — bundled, default"
    echo "    2) mysql       — bring-your-own MySQL 8"
    echo "    3) mariadb     — bring-your-own MariaDB 10.6+ / 11.x"
    read -rp "  Choice [1]: " _choice || _choice=""
    case "$_choice" in
      2|mysql|MySQL)     SEL_BACKEND="mysql" ;;
      3|mariadb|MariaDB) SEL_BACKEND="mariadb" ;;
      *)                 SEL_BACKEND="postgresql" ;;
    esac
  fi
  # DB_FAMILY normalises the setup path: MariaDB reuses the entire MySQL path and
  # differs only in the client image. All MySQL-path branches below test DB_FAMILY.
  case "$SEL_BACKEND" in
    postgresql) DB_FAMILY=postgresql ;;
    mysql|mariadb) DB_FAMILY=mysql ;;
    *) fail "Unsupported database backend '$SEL_BACKEND' (allowed: postgresql, mysql, mariadb)"; exit 1 ;;
  esac

  if [ "$DB_FAMILY" = "mysql" ]; then
    _ask() {  # _ask VAR "prompt" "default" "flag-value"
      local __var="$1" __prompt="$2" __def="$3" __flag="$4" __v=""
      if [ -n "$__flag" ]; then __v="$__flag"
      elif [ "$NONINTERACTIVE" = "0" ] && [ -t 0 ]; then read -rp "  $__prompt [$__def]: " __v || __v=""; __v="${__v:-$__def}"
      else __v="$__def"; fi
      printf -v "$__var" '%s' "$__v"
    }
    _ask SEL_DB_HOST "MySQL host"          "127.0.0.1" "$DB_HOST_ARG"
    _ask SEL_DB_PORT "MySQL port"          "3306"      "$DB_PORT_ARG"
    _ask SEL_DB_NAME "MySQL database name" "velaris"   "$DB_NAME_ARG"
    _ask SEL_DB_USER "MySQL user"          "velaris"   "$DB_USER_ARG"
    if [ -n "$DB_PASSWORD_ARG" ]; then
      SEL_DB_PASSWORD="$DB_PASSWORD_ARG"
    elif [ "$NONINTERACTIVE" = "0" ] && [ -t 0 ]; then
      read -rsp "  MySQL password: " SEL_DB_PASSWORD || SEL_DB_PASSWORD=""; echo ""
    fi
    ok "MySQL selected: ${SEL_DB_USER}@${SEL_DB_HOST}:${SEL_DB_PORT}/${SEL_DB_NAME}"
  fi
fi

_write_if_missing() {
  local key="$1" val="$2"
  if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null || grep -q "^${key}=$" "$ENV_FILE"; then
    # Remove any empty placeholder line first, then append
    sed -i "/^${key}=$/d" "$ENV_FILE"
    echo "${key}=${val}" >> "$ENV_FILE"
    ok "Generated ${key}"
  else
    ok "${key} already set — keeping existing value"
  fi
}

# Treat the publicly-known dev default as unset so a real secret is generated
# (older .env files created from .env.example may carry it as a non-empty value)
sed -i "s|^VELARIS_CASE_AUTH_SECRET=velaris-dev-secret-change-in-production$|VELARIS_CASE_AUTH_SECRET=|" "$ENV_FILE"

# PostgreSQL (bundled): generate a random DB password. MySQL (BYO): store the
# operator-provided password verbatim — both the runner's mysql client and the app
# authenticate with it. Password lives only in .env/OpenBao, never in velaris.yaml.
if [ "$DB_CONFIGURED" = "0" ] && [ "$DB_FAMILY" = "mysql" ]; then
  sed -i "/^VELARIS_DB_PASSWORD=/d" "$ENV_FILE"
  echo "VELARIS_DB_PASSWORD=${SEL_DB_PASSWORD}" >> "$ENV_FILE"
  ok "VELARIS_DB_PASSWORD set (MySQL, operator-provided)"
else
  _write_if_missing VELARIS_DB_PASSWORD            "$(openssl rand -hex 16)"
fi
# The bundled MinIO's password — only for a host that already runs it. A new
# install has no MinIO (v3.1.2) and gets no MinIO setting at all.
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'velaris-minio' \
   || docker volume ls --format '{{.Name}}' 2>/dev/null | grep -qE '(^|_)velaris-minio-data$'; then
  _write_if_missing VELARIS_MINIO_PASSWORD         "$(openssl rand -hex 24)"
fi
_write_if_missing VELARIS_ADMIN_PASSWORD         "$(openssl rand -hex 16)"
_write_if_missing VELARIS_CASE_AUTH_SECRET         "$(openssl rand -hex 32)"
_write_if_missing VELARIS_CASE_STORAGE_MASTER_KEY  "$(openssl rand -hex 32)"
# HxVault (#19) master KEK that wraps per-tenant DEKs — 32 bytes hex. When unset,
# the app derives a dev KEK from auth_secret (with a startup warning); production
# should use a dedicated key so crypto-shredding is independent of auth_secret.
_write_if_missing VELARIS_CASE_KEK               "$(openssl rand -hex 32)"
# Documents go to local disk on a NEW install (v3.1.2). It was "minio" until the
# MinIO images left Docker Hub and a new install could no longer pull them.
# `_write_if_missing` is the upgrade guarantee: an install that already says
# "minio" keeps saying it, and keeps its container (compose-profiles.sh).
# Object storage is opt-in: VELARIS_CASE_STORAGE_BACKEND=s3 and the
# VELARIS_CASE_S3_* endpoint settings, pointing at an S3 service you run.
_write_if_missing VELARIS_CASE_STORAGE_BACKEND     "local"

# The ports this install actually uses. Written whatever was chosen — including
# the defaults — so the units, the gateway template and the documentation all
# read one source instead of each repeating a literal.
_write_if_missing VELARIS_CASE_SERVICE_PORT        "$VELARIS_CASE_SERVICE_PORT"
_write_if_missing VELARIS_ENGINE_PORT              "$VELARIS_ENGINE_PORT"
_write_if_missing VELARIS_CASE_PORT                "$VELARIS_CASE_PORT"
_write_if_missing VELARIS_ANALYTICS_PORT           "$VELARIS_ANALYTICS_PORT"
_write_if_missing VELARIS_HXGRAPH_PORT             "$VELARIS_HXGRAPH_PORT"
_write_if_missing VELARIS_HXMIGRATE_PORT           "$VELARIS_HXMIGRATE_PORT"
_write_if_missing VELARIS_COMMERCE_PORT            "${VELARIS_COMMERCE_PORT:-8207}"
_write_if_missing VELARIS_PORTAL_PORT              "${VELARIS_PORTAL_PORT:-8205}"
_write_if_missing VELARIS_USER_PORT                "${VELARIS_USER_PORT:-8206}"
_write_if_missing VELARIS_WORKER_PORT              "${VELARIS_WORKER_PORT:-8208}"
# R5e step 9: a new install runs its background jobs in the worker service only.
# (An existing install never gets this line from an update: unset = case-service
# keeps running them, as before, and the job leases keep the two from doubling.)
# Not on a re-run over an existing systemd install without the worker's unit:
# setup installs units only when case-service's is missing, so there the line
# would leave NO process running the jobs — the scheduler, mail poll and sealers
# silently stopped. Everywhere else the worker does run: start-velaris.sh starts
# it, and a units install below includes its unit.
_velaris_units="${VELARIS_UNIT_DIR:-/etc/systemd/system}"
if [ ! -f "$_velaris_units/velaris-case-service.service" ] \
   || [ -f "$_velaris_units/velaris-worker-service.service" ]; then
  _write_if_missing VELARIS_CASE_RUN_JOBS          "false"
fi
_write_if_missing VELARIS_STUDIO_PORT              "$VELARIS_STUDIO_PORT"

# Workload identity (#18): the gateway injects this and every service refuses a
# request without it, so no other process on the host can reach a service port
# directly and go around the gateway. Generated for new installs; an existing
# install with the variable absent simply has the check off, which is why the
# services treat an empty value as "not configured" rather than failing closed.
_write_if_missing VELARIS_GATEWAY_IDENTITY_SECRET  "$(openssl rand -hex 32)"
# The internal service secret (R5e step 7): commerce-service calls case-service's
# /internal/v1 routes with it, and they refuse without it. Every install has one.
_write_if_missing VELARIS_INTERNAL_SERVICE_SECRET  "$(openssl rand -hex 32)"
# The event broker's password. Generated whether or not the event bus is on, so
# turning the bus on later needs no secret step; the bundled Kafka refuses to
# start without it (docker-compose.yml, `events`).
_write_if_missing VELARIS_CASE_EVENT_BUS_PASSWORD  "$(openssl rand -hex 24)"
# The shared cache (Valkey) — ON for a NEW install, with a password: rate limits
# shared by every service process, and live updates across processes. A re-run
# of setup on an existing install changes nothing here (cache stays as it was,
# with no password); start-velaris.sh adds the password if an operator turns
# the cache on later.
if [ "$NEW_ENV" = true ]; then
  _write_if_missing VELARIS_CASE_REDIS_PASSWORD     "$(openssl rand -hex 24)"
  _write_if_missing VELARIS_CASE_REDIS_ENABLED      "true"
  _write_if_missing VELARIS_CASE_RATE_LIMIT_BACKEND "redis"
fi
# Internal mTLS stays OFF (see deploy/tls/README.md): everything talks over host
# loopback behind a loopback-only gateway, so it would encrypt traffic nothing
# can reach in exchange for a certificate authority to operate.
_write_if_missing VELARIS_INTERNAL_TLS_ENABLED     "false"
_write_if_missing VELARIS_UPSTREAM_SCHEME          "http"

# Generate RSA-2048 key pair for JWT RS256 signing (idempotent — skipped if already set).
# The private key stays on this host only; the public key is used by all services to verify.
# Stored as double-quoted single-line values; python-dotenv expands \n back to real newlines.
if ! grep -q "^VELARIS_CASE_AUTH_RSA_PRIVATE_KEY=" "$ENV_FILE" 2>/dev/null || \
   grep -q "^VELARIS_CASE_AUTH_RSA_PRIVATE_KEY=$" "$ENV_FILE" 2>/dev/null; then

  echo -n "  Generating RSA-2048 key pair for JWT signing... "
  _TMP_DIR=$(mktemp -d)
  openssl genrsa -out "$_TMP_DIR/jwt_private.pem" 2048 2>/dev/null
  openssl rsa -in "$_TMP_DIR/jwt_private.pem" -pubout -out "$_TMP_DIR/jwt_public.pem" 2>/dev/null

  # Flatten PEM to single line with \n escapes so python-dotenv can store it in .env.
  # Double-quoted .env values: python-dotenv interprets \n as actual newlines on read.
  _PRIV_LINE=$(awk '{printf "%s\\n", $0}' "$_TMP_DIR/jwt_private.pem")
  _PUB_LINE=$(awk '{printf "%s\\n", $0}' "$_TMP_DIR/jwt_public.pem")

  # Remove any empty placeholder lines first, then append
  sed -i "/^VELARIS_CASE_AUTH_RSA_PRIVATE_KEY=$/d" "$ENV_FILE"
  sed -i "/^VELARIS_CASE_AUTH_RSA_PUBLIC_KEY=$/d"  "$ENV_FILE"
  echo "VELARIS_CASE_AUTH_RSA_PRIVATE_KEY=\"${_PRIV_LINE}\"" >> "$ENV_FILE"
  echo "VELARIS_CASE_AUTH_RSA_PUBLIC_KEY=\"${_PUB_LINE}\""   >> "$ENV_FILE"

  rm -rf "$_TMP_DIR"
  green "✓ RSA key pair generated and written to .env"
else
  ok "VELARIS_CASE_AUTH_RSA_PRIVATE_KEY already set — keeping existing key pair"
fi

# Update DATABASE_URL in .env to use the generated password.
# Only fill it in when it's still the .env.example placeholder ("REPLACE") or
# empty/missing — NEVER clobber a custom URL an operator set on their own
# server (e.g. a managed/remote Postgres host).
DB_PASS=$(grep "^VELARIS_DB_PASSWORD=" "$ENV_FILE" | cut -d= -f2)
if [ "$DB_CONFIGURED" = "1" ]; then
  ok "VELARIS_CASE_DATABASE_URL already customized — keeping existing value"
elif [ "$DB_FAMILY" = "mysql" ]; then
  # MySQL (BYO): write TYPED connection components (not a raw URL). The backend
  # builds the async URL and URL-encodes the credentials — so a password with
  # @ : / # special chars can't corrupt it (a raw embedded URL would). Aligns with
  # the security spine: no raw URL passthrough; password only in VELARIS_DB_PASSWORD.
  # An explicit empty DATABASE_URL overrides the Postgres default so the components
  # are used. Also record the backend + components in velaris.yaml for the runner.
  sed -i "/^VELARIS_CASE_DATABASE_URL=/d;/^VELARIS_CASE_DB_HOST=/d;/^VELARIS_CASE_DB_PORT=/d;/^VELARIS_CASE_DB_NAME=/d;/^VELARIS_CASE_DB_USER=/d" "$ENV_FILE"
  {
    echo "VELARIS_CASE_DATABASE_URL="
    echo "VELARIS_CASE_DB_HOST=${SEL_DB_HOST}"
    echo "VELARIS_CASE_DB_PORT=${SEL_DB_PORT}"
    echo "VELARIS_CASE_DB_NAME=${SEL_DB_NAME}"
    echo "VELARIS_CASE_DB_USER=${SEL_DB_USER}"
  } >> "$ENV_FILE"
  ok "Database connection set ($SEL_BACKEND, typed components)"
  # Edit velaris.yaml reliably (password is NEVER written here — only host/port/name/user).
  # The persisted `database:` is the REAL backend ($SEL_BACKEND = mysql | mariadb), not the
  # family — the runtime picks MysqlBackend vs MariadbBackend from it.
  python3 - "$VELARIS_DIR/velaris.yaml" "$SEL_BACKEND" "$SEL_DB_HOST" "$SEL_DB_PORT" "$SEL_DB_NAME" "$SEL_DB_USER" <<'PY'
import re, sys
path, backend, host, port, name, user = sys.argv[1:7]
lines = open(path).read().splitlines()
# Drop any previously-managed db_* lines so re-runs stay clean.
lines = [ln for ln in lines if not re.match(r'^\s*db_(host|port|name|user):', ln)]
out = []
for ln in lines:
    m = re.match(r'^(\s*)database:\s*.*$', ln)
    if m:
        ind = m.group(1)
        out.append(f"{ind}database: {backend}")
        out.append(f'{ind}db_host: "{host}"')
        out.append(f'{ind}db_port: {port}')
        out.append(f'{ind}db_name: "{name}"')
        out.append(f'{ind}db_user: "{user}"')
    else:
        out.append(ln)
open(path, "w").write("\n".join(out) + "\n")
PY
  ok "velaris.yaml updated (database: $SEL_BACKEND + connection components)"
else
  sed -i "/^VELARIS_CASE_DATABASE_URL=/d" "$ENV_FILE"
  echo "VELARIS_CASE_DATABASE_URL=postgresql+asyncpg://velaris:${DB_PASS}@localhost:5432/velaris" >> "$ENV_FILE"
  ok "DATABASE_URL set with generated password"
  # R5d 6a: a NEW install with the bundled PostgreSQL connects through PgBouncer
  # from day one (the `pooler` compose profile follows this setting). A line
  # already present — even empty, an operator's "no" — is left as it is. The
  # direct URL above stays for migrations, backups and admin scripts.
  if ! grep -q "^VELARIS_CASE_DATABASE_POOLER_URL=" "$ENV_FILE" 2>/dev/null; then
    echo "VELARIS_CASE_DATABASE_POOLER_URL=postgresql+asyncpg://velaris:${DB_PASS}@localhost:6432/velaris" >> "$ENV_FILE"
    ok "Connection pooler on (PgBouncer, 127.0.0.1:6432)"
    # R5e step 7: commerce-service gets its own pool on the same pooler, only
    # alongside the line above — never on an install that said no to pooling.
    if ! grep -q "^VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL=" "$ENV_FILE" 2>/dev/null; then
      echo "VELARIS_CASE_COMMERCE_DATABASE_POOLER_URL=postgresql+asyncpg://velaris:${DB_PASS}@localhost:6432/velaris_commerce" >> "$ENV_FILE"
    fi
    # R5e step 8: and portal-service its own, the same way.
    if ! grep -q "^VELARIS_CASE_PORTAL_DATABASE_POOLER_URL=" "$ENV_FILE" 2>/dev/null; then
      echo "VELARIS_CASE_PORTAL_DATABASE_POOLER_URL=postgresql+asyncpg://velaris:${DB_PASS}@localhost:6432/velaris_portal" >> "$ENV_FILE"
    fi
    # R5e step 10: and user-service its own, the same way.
    if ! grep -q "^VELARIS_CASE_USER_DATABASE_POOLER_URL=" "$ENV_FILE" 2>/dev/null; then
      echo "VELARIS_CASE_USER_DATABASE_POOLER_URL=postgresql+asyncpg://velaris:${DB_PASS}@localhost:6432/velaris_user" >> "$ENV_FILE"
    fi
    # The platform reaches the pooler over its UNIX socket (deploy/docker-compose/
    # pooler-socket, the owner's only), not 127.0.0.1:6432: a loopback port is
    # free while the pooler restarts, and whoever bound it then could ask for the
    # database password in clear. NEW installs only; an update never sets it.
    if ! grep -q "^VELARIS_CASE_DATABASE_POOLER_SOCKET=" "$ENV_FILE" 2>/dev/null; then
      echo "VELARIS_CASE_DATABASE_POOLER_SOCKET=true" >> "$ENV_FILE"
      ok "The platform reaches the pooler through its socket (owner only)"
    fi
  fi
  # Query statistics (pg_stat_statements) for HxDBManager's slow queries and
  # index advisor. NEW installs only; an update never sets it, and a line
  # already present — even empty — is left as it is (scripts/db-query-stats.sh).
  if ! grep -q "^VELARIS_DB_QUERY_STATS=" "$ENV_FILE" 2>/dev/null; then
    echo "VELARIS_DB_QUERY_STATS=true" >> "$ENV_FILE"
    ok "Query statistics on (pg_stat_statements)"
  fi
fi

# Document storage lives inside the install dir (no root, no /var/lib perms needed).
# Provision the folder and point .env at it — but never clobber an operator's own
# path (either the new VELARIS_CASE_ var or a legacy VELARIS_CASE_ one).
STORAGE_DIR="$VELARIS_DIR/data/documents"
# Local disk is the default document store from v3.1.2, so the folder is made
# private to this user — but only when it is created here. An existing folder
# keeps the permissions its operator gave it (a backup job may rely on them).
if [ ! -d "$STORAGE_DIR" ]; then
  mkdir -p "$STORAGE_DIR"
  chmod 700 "$STORAGE_DIR"
fi
if grep -qE "^(VELARIS|HELIX)_CASE_STORAGE_LOCAL_PATH=" "$ENV_FILE"; then
  ok "Document storage path already set in .env — keeping existing value"
else
  echo "VELARIS_CASE_STORAGE_LOCAL_PATH=$STORAGE_DIR" >> "$ENV_FILE"
  ok "Document storage provisioned at $STORAGE_DIR"
fi

# Load the generated vars into this shell so docker compose can use them
set -o allexport; source "$ENV_FILE"; set +o allexport
# Which optional compose services this install runs (the bundled MinIO is kept
# only where it already exists) — see the file for the rule.
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh" ] && . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"
# Database calls go through deploy/external-services.sh (sourced above with the
# compose profiles). Without that file — a partial copy — the bundled calls this
# script always made.
if ! type velaris_psql >/dev/null 2>&1; then
  velaris_is_external() { return 1; }
  velaris_psql()        { docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"; }
  velaris_pg_isready()  { docker exec "$DB_CONTAINER" pg_isready -h 127.0.0.1 -U "$DB_USER" -q; }
  velaris_pg_url()      { printf '%s' "${VELARIS_CASE_DATABASE_URL:-}"; }
fi

# ── Step 2: Docker group ──────────────────────────────────────────
step "Step 2/9 — Checking Docker group membership..."

if groups "$USER" | grep -qw docker; then
  ok "User '$USER' is already in the docker group"
elif docker ps &>/dev/null 2>&1; then
  ok "Docker accessible"
else
  echo "  Adding '$USER' to the docker group..."
  sudo usermod -aG docker "$USER"
  warn "Added to docker group. Log out and back in, then re-run setup."
  exit 0
fi

# ── Step 2b: Docker storage location + build-cache cap ────────────
# Runs BEFORE the image pull (Step 5) on purpose: setting data-root now means
# images land in the right place instead of being copied there afterwards.
#
# Two problems being fixed, both from Docker's defaults:
#   · data-root defaults to /var/lib/docker on the ROOT filesystem. Where /home
#     is a separate, larger partition that fills the small one first — and a
#     full root filesystem takes the whole OS down, not just Velaris.
#   · BuildKit never expires build cache. Left alone it grows without bound.
step "Step 2b — Docker storage location and build-cache cap..."

# shellcheck source=scripts/docker-storage.sh
if [ -f "$VELARIS_DIR/scripts/docker-storage.sh" ]; then
  . "$VELARIS_DIR/scripts/docker-storage.sh"
else
  warn "scripts/docker-storage.sh missing — skipping Docker storage setup."
fi

if ! type vds_apply >/dev/null 2>&1; then
  :                                           # helper absent; warned above
elif ! docker info &>/dev/null 2>&1; then
  warn "Docker not accessible — skipping. Re-run setup to configure storage."
else
  VDS_CHOICE=""
  _cur_root="$(vds_current_root)"
  _home_root="$HOME/docker-data"

  if [ -n "$DOCKER_DATA_ROOT_ARG" ]; then
    case "$DOCKER_DATA_ROOT_ARG" in
      default|DEFAULT|/var/lib/docker) VDS_CHOICE="" ;;
      *)                               VDS_CHOICE="$DOCKER_DATA_ROOT_ARG" ;;
    esac
    echo "  Using --docker-data-root: ${VDS_CHOICE:-/var/lib/docker (default)}"

  elif vds_same_fs / "$HOME"; then
    # One filesystem: moving data-root is the same disk and same free space.
    # Nothing to choose — just cap the cache.
    echo "  / and $HOME are on one filesystem ($(vds_total_gb /)GB, $(vds_free_gb /)GB free)."
    echo "  Moving Docker's data-root would not free anything, so keeping the default."
    VDS_CHOICE=""

  elif [ "$NONINTERACTIVE" = "1" ] || [ ! -t 0 ]; then
    warn "Non-interactive — keeping Docker's default data-root ($_cur_root)."
    VDS_CHOICE=""

  else
    echo "  Docker stores images, containers and volumes on disk."
    echo "  Velaris pulls ~6 GB of images, and databases grow on top of that."
    echo "  Choosing anything but the default moves BOTH Docker's data-root"
    echo "  (containers, volumes, build cache) and containerd's root (image"
    echo "  layers) — they are separate settings and only both together move"
    echo "  everything off the root filesystem."
    echo ""
    printf "    1) %-32s %5sGB total, %5sGB free  %s\n" \
      "/var/lib/docker" "$(vds_total_gb /)" "$(vds_free_gb /)" "← Docker default (root filesystem)"
    printf "    2) %-32s %5sGB total, %5sGB free  %s\n" \
      "$_home_root" "$(vds_total_gb "$HOME")" "$(vds_free_gb "$HOME")" "← separate filesystem"
    echo "    3) custom path"
    echo ""
    # Recommend by free space: a full root filesystem wedges the OS itself.
    _rec=1; [ "$(vds_free_gb "$HOME")" -gt "$(vds_free_gb /)" ] 2>/dev/null && _rec=2
    read -rp "$(yellow "  ? Docker storage location [1-3, Enter = $_rec]: ")" _dchoice || _dchoice=""
    case "${_dchoice:-$_rec}" in
      1) VDS_CHOICE="" ;;
      2) VDS_CHOICE="$_home_root" ;;
      3) read -rp "  Absolute path: " VDS_CHOICE || VDS_CHOICE=""
         case "$VDS_CHOICE" in
           /*) ;;
           *)  warn "Not an absolute path — keeping the default."; VDS_CHOICE="" ;;
         esac ;;
      *) warn "Unrecognised choice — keeping the default."; VDS_CHOICE="" ;;
    esac
  fi

  # Moving data-root restarts Docker. Harmless here (nothing of ours is up
  # yet), but on a re-run over a live install it would bounce the stack, so
  # say so rather than surprising the operator.
  if [ -n "$VDS_CHOICE" ] && [ "$VDS_CHOICE" != "$_cur_root" ] \
     && [ -n "$(docker ps -q 2>/dev/null)" ]; then
    warn "Containers are running — moving data-root will restart Docker."
  fi

  vds_apply "$VDS_CHOICE" || warn "Docker storage setup incomplete — see messages above."
  ok "Docker storage configured (data-root: $(vds_current_root))"
fi

# ── Step 3: uv workspace sync ─────────────────────────────────────
step "Step 3/9 — Syncing uv workspace..."
cd "$VELARIS_DIR"
# Re-running setup on an existing box must not prune extras it already uses
# (`uv sync` is exact). The event bus client is kept whenever the bus is on
# (compose-profiles.sh, sourced above) or the library is already present.
_SETUP_EXTRAS=""
uv run python -c "import faster_whisper" >/dev/null 2>&1 && _SETUP_EXTRAS="$_SETUP_EXTRAS --extra intelligence"
uv run python -c "import av" >/dev/null 2>&1 && _SETUP_EXTRAS="$_SETUP_EXTRAS --extra kyc"
# The MySQL/MariaDB driver (aiomysql + pymysql) is an extra: without it a
# MySQL/MariaDB install cannot reach its own database, and `uv sync` (exact)
# would remove one installed by hand. Fixed 2026-10-16 — it was never installed.
[ "${DB_FAMILY:-postgresql}" = "mysql" ] && _SETUP_EXTRAS="$_SETUP_EXTRAS --extra mysql"
if [ "${VELARIS_EVENT_BROKER_ACTIVE:-false}" = true ] \
   || uv run python -c "import aiokafka" >/dev/null 2>&1; then
  _SETUP_EXTRAS="$_SETUP_EXTRAS --extra events"
fi
# shellcheck disable=SC2086
quiet "uv sync" uv sync --all-packages $_SETUP_EXTRAS || uv sync --all-packages $_SETUP_EXTRAS 2>&1 | tail -5

# Backup key (docs/Future/Done/backup-restore-design.md §9). Every install gets one.
# Only the PUBLIC key goes to .env (pushed to OpenBao with the rest in Step 5b);
# a generated PRIVATE key goes only into velaris-install-report.md (0600), with
# instructions to copy it off this server and then run
# ./backup-velaris.sh --forget-private-key. --backup-recipient skips the pair.
_bk_args=(ensure-key --no-push)
[ -n "$BACKUP_RECIPIENT_ARG" ] && _bk_args+=(--recipient "$BACKUP_RECIPIENT_ARG")
if "$VELARIS_DIR/.venv/bin/python" "$VELARIS_DIR/scripts/backup/velaris_backup.py" "${_bk_args[@]}"; then
  ok "Backup key ready"
else
  warn "Backup key not set — backups stay database-only and unencrypted until: ./backup-velaris.sh --ensure-key"
fi
ok "uv workspace synced"

# ── Step 4: Studio npm install ────────────────────────────────────
step "Step 4/9 — Installing Studio npm dependencies..."
if [ -d "$VELARIS_DIR/studio" ]; then
  cd "$VELARIS_DIR/studio"
  quiet "npm install" npm install --prefer-offline || npm install --prefer-offline 2>&1 | tail -5
  ok "Studio dependencies installed"
else
  fail "studio/ directory not found"
fi
cd "$VELARIS_DIR"

# ── Gateway limits: validated before a container is started ───────
#
# The gateway's rate and connection ceilings reach nginx.conf.template from
# .env. nginx validates them when it STARTS, so a value that is not an nginx
# rate is a gateway that will not boot. Unset or empty is always fine — compose
# supplies the shipped default — so only a value somebody typed can be wrong,
# and this says so here rather than leaving a dead gateway to diagnose.
# The same check runs in update-velaris.sh before anything is touched.
for _k in VELARIS_GW_RATE_API VELARIS_GW_RATE_PORTAL; do
  eval "_v=\${$_k:-}"
  if [ -n "$_v" ] && ! printf '%s' "$_v" | grep -Eq '^[1-9][0-9]*r/[sm]$'; then
    fail "$_k=\"$_v\" is not an nginx rate (expected e.g. 20r/s or 30r/m) — the gateway would refuse to start"
  fi
done
if [ -n "${VELARIS_GW_WS_CONN_MAX:-}" ] && ! printf '%s' "$VELARIS_GW_WS_CONN_MAX" | grep -Eq '^[1-9][0-9]*$'; then
  fail "VELARIS_GW_WS_CONN_MAX=\"$VELARIS_GW_WS_CONN_MAX\" is not a positive whole number — the gateway would refuse to start"
fi

# ── Step 5: Pull Docker images ────────────────────────────────────
step "Step 5/9 — Pulling Docker images..."
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
if docker info &>/dev/null 2>&1; then
  # The bundled MinIO (a re-run on a host that already has it) is left out: its
  # images are gone from Docker Hub and one failed image cancels the whole pull.
  # Unreadable service list = pull everything, as before.
  _pull_services=()
  while IFS= read -r _svc; do if [ -n "$_svc" ]; then _pull_services+=("$_svc"); fi; done < <(
    docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" config --services 2>/dev/null | grep -vxE 'minio|minio-init' || true)
  docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" pull ${_pull_services[@]+"${_pull_services[@]}"} 2>&1 | grep -E "Pull|pull|already|Digest" || true
  ok "Docker images up to date"
else
  warn "Docker not accessible — skipping image pull."
fi

# ── Step 5b: OpenBao secrets bootstrap (Group K — only if enabled) ─
# Runs only when the sentinel deploy/openbao/enabled is present. .env was
# already populated in Step 1b (existing values kept, missing ones generated),
# so this just brings up OpenBao, initialises it on a fresh install (or unseals
# an existing one), then imports those .env secrets into the vault — OpenBao is
# the source of truth from here on. Every failure is non-fatal (warn, continue).
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ]; then
  step "Step 5b — Bootstrapping OpenBao secrets..."
  INIT_FILE="$VELARIS_DIR/deploy/openbao/.bao-init.json"

  # Every secrets-*.sh needs jq, and nothing installed it: on a machine without
  # it the init failed and setup went on with no vault (R5b drill, 2026-10-30).
  # Installed only here — OpenBao is optional, so jq is not in deploy/deps
  # (the updater halts on anything missing from that list).
  if ! command -v jq >/dev/null 2>&1; then
    sudo apt-get "${APT_PROGRESS[@]}" install -y jq || warn "Could not install jq — OpenBao's scripts need it"
  fi

  # One-time move of file storage to raft (OpenBao 2.7 has no file backend),
  # before OpenBao starts. If it fails, OpenBao comes up EMPTY — so a failed
  # migration must never be followed by an init, which would mint a second,
  # unrelated vault over the real one's place.
  _bao_migrate_ok=1
  if ! "$VELARIS_DIR/scripts/openbao-storage-migrate.sh"; then
    _bao_migrate_ok=0
    warn "OpenBao storage migration failed — OpenBao will not be initialised by this run"
  fi

  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d openbao 2>/dev/null \
    || warn "Could not start the OpenBao container"

  # Wait for OpenBao to answer (471=uninitialised, 472=sealed, 200/429=unsealed).
  BAO_HEALTH="000"
  for i in $(seq 1 15); do
    BAO_HEALTH=$(curl -s -m 2 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:8350/v1/sys/health?sealedcode=472&uninitcode=471" 2>/dev/null || true)
    [ "$BAO_HEALTH" != "000" ] && break
    sleep 1
  done

  if [ ! -f "$INIT_FILE" ] && [ "$BAO_HEALTH" = "471" ] && [ "$_bao_migrate_ok" = 1 ]; then
    # Fresh OpenBao — one-time init: writes the keyfile, unseals, enables KV + AppRole.
    if "$VELARIS_DIR/scripts/secrets-init.sh"; then ok "OpenBao initialised"; else warn "OpenBao init failed — continuing"; fi
  else
    # Already initialised — just make sure it's unsealed.
    if "$VELARIS_DIR/scripts/secrets-unseal.sh" >/dev/null 2>&1; then ok "OpenBao unsealed"; else warn "OpenBao still sealed — run ./scripts/secrets-unseal.sh"; fi
  fi

  # Import the secrets from .env into OpenBao (source of truth going forward).
  if "$VELARIS_DIR/scripts/secrets-push.sh" --sync-env >/dev/null 2>&1; then
    ok "Secrets synced from .env into OpenBao"
  else
    warn "secrets-push failed — run ./scripts/secrets-push.sh --sync-env once OpenBao is unsealed"
  fi
fi

# ── Step 6: Check migrations ──────────────────────────────────────
step "Step 6/9 — Checking migrations..."
MIGRATION_COUNT=$(find "$VELARIS_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | wc -l)
ok "Found $MIGRATION_COUNT migration files"

# ── Step 7: Start database temporarily for setup ─────────────────
if [ "$DB_FAMILY" = "mysql" ]; then
  # ── Step 7/8 (MySQL): BYO external DB — no bundled container to start ──
  # Resolve connection from this run's selection, falling back to velaris.yaml/.env
  # on a re-run (where the interactive selection was skipped).
  MY_HOST="${SEL_DB_HOST:-$(_yaml_get db_host)}"; MY_HOST="${MY_HOST:-127.0.0.1}"
  MY_PORT="${SEL_DB_PORT:-$(_yaml_get db_port)}"; MY_PORT="${MY_PORT:-3306}"
  MY_NAME="${SEL_DB_NAME:-$(_yaml_get db_name)}"; MY_NAME="${MY_NAME:-velaris}"
  MY_USER="${SEL_DB_USER:-$(_yaml_get db_user)}"; MY_USER="${MY_USER:-velaris}"
  MY_PASS=$(grep "^VELARIS_DB_PASSWORD=" "$ENV_FILE" | cut -d= -f2)
  # The mysql:8 client speaks to both MySQL 8 and MariaDB 10.6+/11 servers.
  # TLS when VELARIS_CASE_DATABASE_URL names ?ssl_ca= (deploy/external-services.sh).
  _MY_TLS_MOUNTS=(); _MY_TLS_FLAGS=()
  if type velaris_mysql_tls_mounts >/dev/null 2>&1; then
    mapfile -t _MY_TLS_MOUNTS < <(velaris_mysql_tls_mounts)
    mapfile -t _MY_TLS_FLAGS < <(velaris_mysql_tls_flags)
  fi
  mysql_client() {
    MYSQL_PWD="$MY_PASS" docker run --rm -i --network host -e MYSQL_PWD "${_MY_TLS_MOUNTS[@]}" mysql:8.4.11 \
      mysql --connect-timeout=10 -h "$MY_HOST" -P "$MY_PORT" -u "$MY_USER" "${_MY_TLS_FLAGS[@]}" "$@"
  }

  step "Step 7/9 — Connecting to MySQL ($MY_HOST:$MY_PORT)..."
  echo "  Bring-your-own MySQL — no bundled DB container is started."
  for i in $(seq 1 20); do
    if mysql_client -e "SELECT 1" >/dev/null 2>&1; then ok "MySQL reachable"; break; fi
    [ "$i" -eq 20 ] && { fail "MySQL not reachable at $MY_HOST:$MY_PORT — check the server, user, and password"; exit 1; }
    sleep 1
  done
  if mysql_client -e "CREATE DATABASE IF NOT EXISTS \`$MY_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"; then
    ok "Database '$MY_NAME' ready (utf8mb4)"
  else
    fail "Could not create/access database '$MY_NAME' (does the user have rights?)"; exit 1
  fi

  step "Step 8/9 — Running database migrations..."
  mysql_client "$MY_NAME" -e "CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   VARCHAR(255) PRIMARY KEY,
  applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
);"
  APPLIED=0; SKIPPED=0
  for migration_file in $(find "$VELARIS_DIR/migrations/mysql" -maxdepth 1 -name "*.sql" | sort); do
    filename="$(basename "$migration_file")"
    EXISTS=$(mysql_client "$MY_NAME" -N -e "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
    if [ "$EXISTS" = "1" ]; then
      SKIPPED=$((SKIPPED + 1)); continue
    fi
    echo -n "    apply $filename ... "
    if mysql_client "$MY_NAME" < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
      mysql_client "$MY_NAME" -e "INSERT IGNORE INTO schema_migrations(filename) VALUES('$filename');"
      green "✓"; APPLIED=$((APPLIED + 1))
    else
      red "FAILED"; cat "$VELARIS_SCRATCH/migration_err.log"
      red "  $filename was NOT recorded as applied; the database is part-migrated."
      red "  Fix the migration, then re-run: everything before it is already recorded."
      exit 1
    fi
  done
  green "  ✓ Migrations: $APPLIED applied, $SKIPPED skipped"
else
step "Step 7/9 — Starting database for setup..."
DB_CONTAINER="docker-compose-velaris-db-1"
DB_USER="velaris"
DB_NAME="velaris"

# Do NOT silence these — a failure here is the whole reason the next loop
# times out, and hiding it leaves the operator with "not ready" and no cause.
# deploy/tls/certs before Docker can create it as root (the gateway mounts it).
# shellcheck source=deploy/tls/certs-dir.sh
[ -f "$VELARIS_DIR/deploy/tls/certs-dir.sh" ] && . "$VELARIS_DIR/deploy/tls/certs-dir.sh" && velaris_tls_certs_dir "$VELARIS_DIR"
DB_UP_LOG=$(mktemp)
# A database on another server (VELARIS_EXTERNAL_SERVICES=database) is not ours
# to start, and must be secured: refused here, before anything is written to it.
if velaris_is_external database; then
  velaris_external_check || exit 1
elif ! docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d velaris-db > "$DB_UP_LOG" 2>&1; then
  docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d >> "$DB_UP_LOG" 2>&1 || true
fi

echo "  Waiting for PostgreSQL..."
for i in $(seq 1 20); do
  if velaris_pg_isready 2>/dev/null; then
    ok "PostgreSQL ready"; break
  fi
  if [ "$i" -eq 20 ]; then
    fail "PostgreSQL not ready after 20s."
    echo ""
    yellow "  ── docker compose output ─────────────────────────"
    sed 's/^/  /' "$DB_UP_LOG"
    yellow "  ── last 20 lines of $DB_CONTAINER ────────────────"
    docker logs "$DB_CONTAINER" --tail 20 2>&1 | sed 's/^/  /' || echo "  (container does not exist)"
    yellow "  ──────────────────────────────────────────────────"
    # Port 5432 already taken by a host Postgres is the usual cause.
    if ss -tlnp 2>/dev/null | grep -q ':5432 '; then
      warn "Something is already listening on 5432 — likely a host PostgreSQL."
      warn "  Free it with:  sudo systemctl disable --now postgresql"
    fi
    rm -f "$DB_UP_LOG"
    exit 1
  fi
  sleep 1
done
# Query statistics: loads pg_stat_statements when VELARIS_DB_QUERY_STATS says
# so (one database restart, now, before anything uses it). Unset = untouched.
"$VELARIS_DIR/scripts/db-query-stats.sh" || warn "Query statistics not applied — run ./scripts/db-query-stats.sh later"
rm -f "$DB_UP_LOG"

# Sync DB user password to match .env (prevents auth failures on TCP connections).
# Bundled database only: an external server's credentials are its owner's.
if ! velaris_is_external database; then
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
  -c "ALTER USER ${DB_USER} PASSWORD '${VELARIS_DB_PASSWORD}';" 2>/dev/null \
  && ok "DB user password synced" || warn "Could not sync DB password — continuing"
fi

# ── Step 8: Run migrations (including 071_superadmin) ─────────────
step "Step 8/9 — Running database migrations..."

velaris_psql -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0
RELEASE_SEED_NEW=false   # set when THIS run created 076's release rows (Step 8c)

# What is already applied, read ONCE: one query, not one per file — against a
# database on another server each query is a client container (~1 s).
_APPLIED_MIGRATIONS="$(velaris_psql -tAq -c "SELECT filename FROM schema_migrations;" </dev/null 2>/dev/null || true)"
for migration_file in $(find "$VELARIS_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  EXISTS=""; grep -qxF "$filename" <<<"$_APPLIED_MIGRATIONS" && EXISTS=1
  if [ "$EXISTS" = "1" ]; then
    SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  # ON_ERROR_STOP is what makes a failure a failure. Without it psql runs the
  # remaining statements, exits 0, and the file below is recorded as applied —
  # so a migration whose statements were rejected is marked done and never
  # runs again. Migration 080 did exactly that on every fresh install.
  if velaris_psql -q \
      -v ON_ERROR_STOP=1 < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
    velaris_psql -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;" </dev/null
    if [ "$filename" = "076_scheduled_releases.sql" ]; then RELEASE_SEED_NEW=true; fi
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"; cat "$VELARIS_SCRATCH/migration_err.log"
    red "  $filename was NOT recorded as applied; the database is part-migrated."
    red "  Fix the migration, then re-run: everything before it is already recorded."
    exit 1
  fi
done
green "  ✓ Migrations: $APPLIED applied, $SKIPPED skipped"

# ── Step 8c: Marketplace + Customer Accounts on for a NEW database ─
# Migration 076 seeds both as an unpublished draft, and the release manifest
# never switches on a row that exists and is off — so on every fresh PostgreSQL
# install Marketplace stayed hidden until a superadmin called the publish API,
# which no Studio screen offers. (MySQL/MariaDB have no seed row; their manifest
# inserts both released.)
#
# Only when THIS run applied 076: the rows are this install's own, created
# seconds ago. A re-run of setup, and every update, finds 076 already recorded
# and changes nothing — an existing install that never published keeps both
# off, exactly as before. The WHERE clause also requires the untouched seed
# state (draft, never released), so a row anyone has touched is left alone.
# The manifest then advances `enabled` to the shipped version on first start.
if [ "$RELEASE_SEED_NEW" = true ]; then
  if velaris_psql -q -v ON_ERROR_STOP=1 </dev/null -c "
      UPDATE scheduled_releases
         SET enabled = COALESCE(version, 'v1.0.0'), status = 'released',
             released_at = NOW(), updated_at = NOW()
       WHERE feature_key IN ('marketplace', 'customer_accounts')
         AND status = 'draft' AND enabled IS NULL AND released_at IS NULL;" \
      2>"$VELARIS_SCRATCH/release_seed_err.log"; then
    ok "Marketplace and Customer Accounts released for this new install"
  else
    warn "Could not release Marketplace/Customer Accounts — a superadmin can: POST /api/v1/releases/{id}/publish"
    head -3 "$VELARIS_SCRATCH/release_seed_err.log"
  fi
fi
fi

# ── Step 8b: AI provider ──────────────────────────────────────────
# Ollama is the default on purpose: it runs on this box, costs nothing, and
# needs no key, so an install that skips this step is still fully functional
# AND still sovereign. Everything else sends prompt content to a third party.
step "Step 8b — AI provider (Enter = keep local Ollama)..."

_set_env() {                       # _set_env KEY VALUE — replace or append
  local key="$1" value="$2"
  sed -i "/^${key}=/d" "$ENV_FILE"
  case "$value" in
    *[[:space:]]*|*"#"*) printf '%s="%s"\n' "$key" "$value" >> "$ENV_FILE" ;;
    *)                   printf '%s=%s\n'   "$key" "$value" >> "$ENV_FILE" ;;
  esac
}

if [ "$NONINTERACTIVE" = "1" ] || [ ! -t 0 ]; then
  warn "Non-interactive — leaving the AI provider as configured (default: ollama)."
else
  echo "  HxNexus, the NL builder, Scout AI and case Q&A all use this backend."
  echo ""
  echo "    1) ollama      local, free, no API key      ← default, nothing leaves this server"
  echo "    2) anthropic   Claude (native SDK)"
  echo "    3) openai      GPT-4o family"
  echo "    4) groq        fastest inference (llama/mixtral)"
  echo "    5) mistral     Mistral AI"
  echo "    6) gemini      Google Gemini (OpenAI-compatible endpoint)"
  echo "    7) together    Together AI (llama, qwen, …)"
  echo "    8) fireworks   Fireworks AI"
  echo "    9) perplexity  Perplexity AI"
  echo "   10) deepseek    DeepSeek"
  echo "   11) xai         xAI (Grok)"
  echo "   12) cohere      Cohere"
  echo "   13) custom      any OpenAI-compatible API (asks for a base URL)"
  echo ""
  read -rp "$(yellow "  ? Provider [1-13, Enter = 1 ollama]: ")" AI_CHOICE || AI_CHOICE=""

  # Defaults are STARTING POINTS, not guarantees — provider model names change
  # often, so each one is offered as an editable default rather than forced.
  AI_NAME=""; AI_DEFAULT_MODEL=""
  case "${AI_CHOICE:-1}" in
    1|"")  AI_NAME="ollama" ;;
    2)     AI_NAME="anthropic";  AI_DEFAULT_MODEL="claude-opus-5" ;;
    3)     AI_NAME="openai";     AI_DEFAULT_MODEL="gpt-4o-mini" ;;
    4)     AI_NAME="groq";       AI_DEFAULT_MODEL="llama-3.3-70b-versatile" ;;
    5)     AI_NAME="mistral";    AI_DEFAULT_MODEL="mistral-large-latest" ;;
    6)     AI_NAME="gemini";     AI_DEFAULT_MODEL="gemini-2.0-flash" ;;
    7)     AI_NAME="together";   AI_DEFAULT_MODEL="meta-llama/Llama-3.3-70B-Instruct-Turbo" ;;
    8)     AI_NAME="fireworks";  AI_DEFAULT_MODEL="accounts/fireworks/models/llama-v3p3-70b-instruct" ;;
    9)     AI_NAME="perplexity"; AI_DEFAULT_MODEL="sonar" ;;
    10)    AI_NAME="deepseek";   AI_DEFAULT_MODEL="deepseek-chat" ;;
    11)    AI_NAME="xai";        AI_DEFAULT_MODEL="grok-2-latest" ;;
    12)    AI_NAME="cohere";     AI_DEFAULT_MODEL="command-r-plus" ;;
    13)    AI_NAME="custom" ;;
    *)     warn "Unrecognised choice — keeping ollama."; AI_NAME="ollama" ;;
  esac

  if [ "$AI_NAME" = "ollama" ]; then
    _set_env VELARIS_CASE_AI_PROVIDER "ollama"
    ok "Using local Ollama — no API key needed, no prompt content leaves this server."
  else
    read -rsp "$(yellow "  ? ${AI_NAME} API key: ")" AI_KEY || AI_KEY=""; echo ""
    if [ -z "$AI_KEY" ]; then
      warn "No key entered — keeping ollama. Re-run this script to set a provider."
      _set_env VELARIS_CASE_AI_PROVIDER "ollama"
    else
      if [ "$AI_NAME" = "custom" ]; then
        read -rp "$(yellow "  ? Base URL (OpenAI-compatible, e.g. http://localhost:1234/v1): ")" AI_URL || AI_URL=""
        read -rp "$(yellow "  ? Model name: ")" AI_MODEL_IN || AI_MODEL_IN=""
        [ -n "$AI_URL" ] && _set_env VELARIS_CASE_AI_BASE_URL "$AI_URL"
      else
        read -rp "$(yellow "  ? Model [$AI_DEFAULT_MODEL]: ")" AI_MODEL_IN || AI_MODEL_IN=""
        AI_MODEL_IN="${AI_MODEL_IN:-$AI_DEFAULT_MODEL}"
      fi

      _set_env VELARIS_CASE_AI_PROVIDER "$AI_NAME"
      _set_env VELARIS_CASE_AI_API_KEY  "$AI_KEY"
      [ -n "$AI_MODEL_IN" ] && _set_env VELARIS_CASE_AI_MODEL "$AI_MODEL_IN"
      ok "AI provider set to ${AI_NAME}${AI_MODEL_IN:+ (${AI_MODEL_IN})}"
      echo "    Model names change often — verify it against the provider's docs if calls fail."

      # These two decide whether the key is ever actually used, and both have
      # bitten operators who set a key and saw "offline". Surface them here.
      echo ""
      warn "Prompt content will now be sent to ${AI_NAME}. Two gates still apply:"
      echo "    · VELARIS_CASE_AI_EGRESS_POLICY (default 'minimized' — completions go out,"
      echo "      embeddings stay local). 'local_only' ignores this provider entirely."
      echo "    · Case Q&A and voice form-fill additionally need the TENANT opted in"
      echo "      (ai.case_qa, external_allowed) before any case content leaves the box."
    fi
  fi
fi
echo ""

# ── Step 8c: Ollama default models (best-effort) ──────────────────
# A fresh Ollama container ships with NO models; every AI feature (HxNexus
# chat, case Q&A, session summaries) then fails soft with empty answers.
# Pull the configured defaults now — non-fatal, skipped when offline.
step "Step 8c — Pulling default Ollama models (best-effort)..."
# Only meaningful when Ollama is the active backend — pulling multi-GB weights
# for a box that talks to a hosted provider wastes bandwidth and disk.
_ACTIVE_AI=$(grep -E "^VELARIS_CASE_AI_PROVIDER=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | xargs || echo "ollama")
OLLAMA_CONTAINER=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i ollama | head -1 || true)
if [ -n "$_ACTIVE_AI" ] && [ "$_ACTIVE_AI" != "ollama" ]; then
  ok "Provider is '$_ACTIVE_AI' — skipping the local model download."
elif [ -n "$OLLAMA_CONTAINER" ]; then
  AI_MODEL="${VELARIS_CASE_AI_OLLAMA_MODEL:-llama3.2}"
  EMBED_MODEL="${VELARIS_CASE_AI_OLLAMA_EMBED_MODEL:-nomic-embed-text}"
  echo "  Models: $AI_MODEL + $EMBED_MODEL (a few GB, one-off download)."
  echo "  Declining is safe — AI features stay dark until the models exist, and"
  echo "  you can pull them any time with the commands printed below."
  if ask "Download the local AI models now?"; then
    for M in "$AI_MODEL" "$EMBED_MODEL"; do
      if docker exec "$OLLAMA_CONTAINER" ollama pull "$M" >/dev/null 2>&1; then
        green "  ✓ pulled $M"
      else
        warn "  Could not pull $M (offline?) — pull later: docker exec $OLLAMA_CONTAINER ollama pull $M"
      fi
    done
  else
    warn "Skipped. Pull them later with:"
    echo "    docker exec $OLLAMA_CONTAINER ollama pull $AI_MODEL"
    echo "    docker exec $OLLAMA_CONTAINER ollama pull $EMBED_MODEL"
  fi
else
  warn "  Ollama container not running — models will be pulled on demand (or via start-velaris.sh hint)."
fi
echo ""

# ── Step 9: Product key + superadmin creation ─────────────────────
step "Step 9/9 — Superadmin account (product key comes later)..."
echo ""
# A fresh box CANNOT have a key yet: the registration website issues one only
# after calling this platform's API to create the customer's user, and that API
# is not running until setup finishes. So setup creates the superadmin and
# leaves activation to ./activate-velaris.sh. --with-key restores the old
# key-first prompt for an operator who already holds one.
SUPERADMIN_KEY_MODE="--skip-key"
for _a in ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}; do
  [ "$_a" = "--with-key" ] && SUPERADMIN_KEY_MODE=""
done

# Skip if superadmin already exists
if [ "$DB_FAMILY" = "mysql" ]; then
  SUPERADMIN_EXISTS=$(mysql_client "$MY_NAME" -N \
    -e "SELECT COUNT(*) FROM velaris_users WHERE is_superadmin = TRUE;" 2>/dev/null || echo "0")
else
  SUPERADMIN_EXISTS=$(velaris_psql -tAq \
    -c "SELECT COUNT(*) FROM velaris_users WHERE is_superadmin = TRUE;" </dev/null 2>/dev/null || echo "0")
fi

if [ "$SUPERADMIN_EXISTS" = "1" ]; then
  warn "Superadmin already exists — skipping superadmin creation."
else
  if [ "$DB_FAMILY" = "mysql" ]; then
    # Install script dependencies (MySQL driver) + run the interactive setup.
    # Password via VELARIS_DB_PASSWORD env (not in the URL) so special chars are safe.
    quiet "uv pip install (mysql)" uv pip install bcrypt httpx pymysql cryptography --quiet
    VELARIS_DB_PASSWORD="$MY_PASS" \
    DATABASE_URL="mysql://${MY_USER}@${MY_HOST}:${MY_PORT}/${MY_NAME}" \
    uv run python "$VELARIS_DIR/scripts/setup_superadmin.py" $SUPERADMIN_KEY_MODE
  else
    # Install script dependencies
    quiet "uv pip install" uv pip install bcrypt httpx psycopg2-binary cryptography --quiet

    # Run the interactive Python setup. A database on another server: the
    # platform's own URL (TLS read from VELARIS_CASE_DATABASE_URL by the script).
    _SA_URL="postgresql://velaris:${VELARIS_DB_PASSWORD:-velaris}@localhost:5432/velaris"
    velaris_is_external database && _SA_URL="$(velaris_pg_url)"
    DATABASE_URL="$_SA_URL" \
    uv run python "$VELARIS_DIR/scripts/setup_superadmin.py" $SUPERADMIN_KEY_MODE
  fi
fi

# ── Step 9b: Firewall ─────────────────────────────────────────────
# Every container port in docker-compose.yml is bound to 127.0.0.1, so Docker
# cannot punch a hole around the firewall the way a bare `ports: ["5432:5432"]`
# would — that is how this box was cryptojacked once, and it is why the
# loopback prefixes are not cosmetic. What is still open to the world is what
# runs on the HOST: Studio on 5173, plus sshd. This step closes everything else.
#
# Deliberately NOT enabled silently: enabling a firewall over an SSH session
# with the wrong rules locks the operator out of their own server, so ssh is
# allowed FIRST and the whole thing is a question, not a default.
step "Step 9b — Firewall (recommended)..."
if ! command -v ufw >/dev/null 2>&1; then
  warn "ufw is not installed — skipping. Your server has NO firewall configured."
  echo "    Install with: sudo apt-get install -y ufw, then re-run this script."
elif sudo ufw status 2>/dev/null | grep -q "^Status: active"; then
  ok "ufw is already active — leaving your rules alone."
  echo "    Studio needs inbound 5173/tcp: sudo ufw allow 5173/tcp"
  sudo ufw status numbered 2>/dev/null | sed 's/^/    /'
elif ask "Enable the firewall (allow SSH + Studio 5173, deny everything else)?"; then
  # Order matters: allow ssh BEFORE the default-deny, and before enabling.
  sudo ufw allow OpenSSH >/dev/null 2>&1 || sudo ufw allow 22/tcp >/dev/null 2>&1 || true
  sudo ufw allow 5173/tcp >/dev/null 2>&1 || true   # Studio — the only public ingress
  sudo ufw default deny incoming  >/dev/null 2>&1 || true
  sudo ufw default allow outgoing >/dev/null 2>&1 || true
  if sudo ufw --force enable >/dev/null 2>&1; then
    ok "Firewall active — inbound SSH + 5173 only."
    echo "    Everything else (Postgres, Redis, Kafka, OpenBao, the API"
    echo "    gateway) is loopback-bound and was never reachable from outside."
    echo "    Adding TLS later: allow 443/tcp and put Studio behind it."
  else
    warn "Could not enable ufw — configure a firewall before exposing this host."
    ERRORS=$((ERRORS + 1))
  fi
else
  warn "No firewall. Port 5173 (Studio) and sshd are open to every network this"
  echo "    host is on. Enable one before this server touches a public IP:"
  echo "      sudo ufw allow OpenSSH && sudo ufw allow 5173/tcp"
  echo "      sudo ufw default deny incoming && sudo ufw enable"
fi

# The marker means "setup ran to completion", NOT "licensed" — verify_key.py
# treats a missing KEY as a warning and a missing MARKER as a hard error, so
# writing it unconditionally here is what lets an unactivated box start.
touch "$VELARIS_DIR/.velaris-setup-complete"

# ── Done ─────────────────────────────────────────────────────────
echo ""
if [ "$ERRORS" -gt 0 ]; then
  red "╔═══════════════════════════════════════════╗"
  red "║  Setup completed with $ERRORS error(s).         ║"
  red "╚═══════════════════════════════════════════╝"
  exit 1
else
  echo "╔═══════════════════════════════════════════╗"
  if [ "${SYSTEMD_MANAGED:-0}" = "1" ]; then
    green "║  Setup complete! Velaris is running.      ║"
  else
    green "║  Setup complete! Run ./start-velaris.sh   ║"
  fi
  echo "╚═══════════════════════════════════════════╝"
  echo ""
  # Boot persistence. Without it the platform only runs for as long as the
  # shell that launched it, so a reboot silently takes the site down.
  if [ ! -f /etc/systemd/system/velaris-case-service.service ] \
     && ask "Install systemd units so Velaris starts on boot?"; then
    if sudo "$VELARIS_DIR/deploy/systemd/install.sh"; then
      # install.sh only ENABLES the units (boot persistence). Start them here
      # too, otherwise the platform is "installed and enabled" but down until
      # the next reboot — and the operator reaches for start-velaris.sh, which
      # would then double-bind the ports at the next boot.
      # The units run the services, not the containers: until now only the
      # database had been started (Step 7), so the gateway, cache and pooler
      # never came up and every unit waited on them (found on a clean box,
      # 2026-10-19). Bring the stack up as start-velaris.sh does; after this,
      # `restart: unless-stopped` brings it back at every boot.
      # A list that cannot be read starts nothing (never "everything", which
      # would bring back containers of parts that run on other servers).
      _UP_SVCS="" _UP_OK=1
      if type velaris_compose_up_services >/dev/null 2>&1; then
        _UP_SVCS="$(velaris_compose_up_services "$COMPOSE_FILE" "$ENV_FILE")" || _UP_OK=0
      fi
      # Up to three tries: an image pull that drops mid-download (a slow or
      # flaky network) usually completes on the next. And if it still fails,
      # the command printed must be the one that works — with the profiles and
      # the services: without `--profile pooler` the pooler never starts and
      # every unit waits on it for ever (found on a clean box, 2026-10-22). Not
      # start-velaris.sh: the units are already installed and would collide.
      _UP_DONE=0
      if [ "$_UP_OK" = 1 ]; then
        for _try in 1 2 3; do
          # shellcheck disable=SC2086
          if docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d $_UP_SVCS >/dev/null 2>&1; then
            _UP_DONE=1; break
          fi
          [ "$_try" -lt 3 ] && { warn "Starting the containers failed (try $_try of 3) — retrying in 10s"; sleep 10; }
        done
      fi
      if [ "$_UP_DONE" != 1 ]; then
        warn "Containers not all started. Fix the cause (often the network), then run:"
        echo "      COMPOSE_PROFILES=\"${COMPOSE_PROFILES:-}\" docker compose -f $COMPOSE_FILE --env-file $ENV_FILE up -d $_UP_SVCS"
        echo "    The services start by themselves once the containers are up."
      fi
      sudo systemctl start velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service velaris-commerce-service velaris-portal-service velaris-user-service velaris-worker-service velaris-studio 2>/dev/null || true
      SYSTEMD_MANAGED=1
      _UNITS_INSTALLED_NOW=1
      ok "systemd units installed, enabled and started"
      echo "    Status:  sudo systemctl status velaris-case-service"
      echo "    Logs:    sudo journalctl -u velaris-case-service -f"
      warn "Manage with systemctl from now on — do NOT also run ./start-velaris.sh."
    else
      warn "systemd install failed — start-velaris.sh still works."
    fi
    echo ""
  fi

  # The update agent. "Update this environment" in Studio only RECORDS the
  # approval; velaris-update.timer is what acts on it. Without the timer the
  # approval waits for ever (found on a real server, 2026-10-12). With
  # `auto_update: false` — the default — the agent does nothing on its own: it
  # applies only what an administrator approved in Studio.
  #
  # Offered when the units were installed in THIS run (a new install) or when a
  # person is here to answer. Never on an unattended re-run of setup over an
  # existing install: `ask` answers yes without a terminal, and switching this
  # on there would be an update's decision, not an operator's.
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
     && [ ! -f /etc/systemd/system/velaris-update.timer ] \
     && [ "${DB_FAMILY:-postgresql}" != "mysql" ] \
     && { [ "${_UNITS_INSTALLED_NOW:-0}" = "1" ] || { [ "$NONINTERACTIVE" != "1" ] && [ -t 0 ]; }; }; then
    if ask "Install the update agent, so updates approved in Studio are applied (hourly check)?"; then
      if "$VELARIS_DIR/update-velaris.sh" --install-timer; then
        case "$(git -C "$VELARIS_DIR" remote get-url origin 2>/dev/null || true)" in
          git@*|ssh://*)
            warn "This install's git remote is SSH. The agent runs with no terminal: if the key"
            echo "    has a passphrase it cannot fetch. The repository is public — use HTTPS:"
            echo "    git remote set-url origin https://github.com/Velaris-App-OS/Velaris-App-OS.git" ;;
        esac
      else
        warn "Update agent not installed — run ./update-velaris.sh --install-timer later."
      fi
    else
      echo "    Skipped. Updates approved in Studio will wait until you run"
      echo "    ./update-velaris.sh --auto, or install the agent with --install-timer."
    fi
    echo ""
  fi

  # The daily backup timer (docs/Future/Done/backup-restore-design.md §5). Offered on
  # a NEW install (.env created by this run) or to a person at a terminal —
  # never on an unattended re-run over an existing install, and never by an
  # update: a schedule is the operator's decision.
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
     && [ ! -f /etc/systemd/system/velaris-backup.timer ] \
     && { [ "${NEW_ENV:-false}" = true ] || { [ "$NONINTERACTIVE" != "1" ] && [ -t 0 ]; }; }; then
    if ask "Back up this install every day at 02:00 (encrypted; see velaris-install-report.md)?"; then
      sudo "$VELARIS_DIR/deploy/systemd/install-backup-timer.sh" \
        || warn "Backup timer not installed — later: sudo ./deploy/systemd/install-backup-timer.sh"
    else
      echo "    Skipped. Back up by hand with ./backup-velaris.sh, or install the timer later:"
      echo "    sudo ./deploy/systemd/install-backup-timer.sh"
    fi
    echo ""
  fi

  if [ ! -f "$VELARIS_DIR/.velaris-key" ]; then
    yellow "  This install is not activated yet — that is expected on a fresh box."
    echo   "    1. ./start-velaris.sh          (the platform must be up first)"
    echo   "    2. register on your Velaris website to receive a product key"
    echo   "    3. ./activate-velaris.sh       (binds the key to this machine)"
    echo ""
  fi
  # Optional ML extras are left out of the default install on purpose: they
  # pull model weights a base install does not need. Every feature that uses
  # them fail-closes honestly (501 / hidden control) rather than degrading
  # silently, so this is an offer, not a warning.
  echo "  Optional add-ons (install only if you want them):"
  echo "    Speech-to-text — voice input, HxMeet live captions:"
  echo "      uv sync --all-packages --extra intelligence"
  echo "    Video-KYC passive checks:"
  echo "      uv sync --all-packages --extra kyc"
  echo ""
  echo "  Microphone features need https or localhost — browsers block mic"
  echo "  access over plain http on a LAN address."
fi
