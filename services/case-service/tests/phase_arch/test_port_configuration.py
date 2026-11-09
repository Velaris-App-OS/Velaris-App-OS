"""Ports are configuration, not literals — and the check runs before we bind.

**Why this exists.** Every port the platform uses was spelled out in three
places at once: the gateway's upstreams, the launch commands and the systemd
units. A client whose machine already used 8202 had no way to move it and no
warning before the bind failed, and an update that adds a component could
discover the clash only after the pull, the migrations and the restart.

**And one service lied about it.** `vite preview` finds its port busy and
quietly takes the next one, so the unit says 5173, the browser needs 5174, and
nothing reports a problem. Every other service fails loudly. `--strictPort`
makes Studio fail loudly too.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
PORTS_SH = ROOT / "deploy" / "ports" / "velaris-ports.sh"
NGINX = ROOT / "deploy" / "docker-compose" / "gateway" / "nginx.conf.template"
COMPOSE = ROOT / "deploy" / "docker-compose" / "docker-compose.yml"
START = ROOT / "start-velaris.sh"
UNITS = ROOT / "deploy" / "systemd"

#: variable → the historical value it must still default to. Changing one of
#: these silently moves an existing install's port on upgrade.
DEFAULTS = {
    "VELARIS_CASE_SERVICE_PORT": "8200",
    "VELARIS_ENGINE_PORT": "8100",
    "VELARIS_CASE_PORT": "8201",
    "VELARIS_ANALYTICS_PORT": "8202",
    "VELARIS_HXGRAPH_PORT": "8203",
    "VELARIS_HXMIGRATE_PORT": "8204",
    "VELARIS_COMMERCE_PORT": "8207",
    "VELARIS_PORTAL_PORT": "8205",
    "VELARIS_USER_PORT": "8206",
    "VELARIS_WORKER_PORT": "8208",
    "VELARIS_STUDIO_PORT": "5173",
}


def test_the_port_table_exists():
    assert PORTS_SH.is_file(), f"{PORTS_SH} is the one place ports are named"


@pytest.mark.parametrize("var,default", sorted(DEFAULTS.items()))
def test_defaults_are_the_historical_values(var, default):
    """An upgrade that sets nothing must keep the ports it already had."""
    text = PORTS_SH.read_text()
    assert re.search(rf'^{var}="\$\{{{var}:-{default}\}}"', text, re.M), (
        f"{var} must default to {default}. A different default moves the port "
        "of every install that never set it — on an upgrade, unattended.")


def test_the_gateway_upstreams_are_not_literals():
    """The upstream is what a moved port has to change; a literal cannot."""
    text = NGINX.read_text()
    offenders = [
        line.strip() for line in text.splitlines()
        if line.strip().startswith("upstream ") and re.search(r":\d{4};", line)
    ]
    assert not offenders, (
        f"gateway upstreams still hardcode a port: {offenders}. Use the "
        "${VELARIS_*_PORT} variables so .env can move them.")


def test_substituted_ports_are_in_the_envsubst_filter():
    """A variable nginx never substitutes renders as an empty upstream."""
    used = set(re.findall(r"\$\{(VELARIS_[A-Z_]*PORT)\}", NGINX.read_text()))
    filt = re.search(r'NGINX_ENVSUBST_FILTER:\s*"([^"]+)"', COMPOSE.read_text())
    assert filt, "the gateway must declare NGINX_ENVSUBST_FILTER"
    allowed = set(filt.group(1).split("|"))
    missing = used - allowed
    assert not missing, (
        f"{sorted(missing)} appear in nginx.conf.template but are not in "
        "NGINX_ENVSUBST_FILTER, so they substitute to nothing.")


def test_compose_passes_every_substituted_port_with_its_default():
    text = COMPOSE.read_text()
    used = set(re.findall(r"\$\{(VELARIS_[A-Z_]*PORT)\}", NGINX.read_text()))
    for var in used:
        assert re.search(rf'{var}:\s*"\$\{{{var}:-{DEFAULTS[var]}\}}"', text), (
            f"the gateway service must pass {var} with its {DEFAULTS[var]} "
            "default, or an install that sets nothing renders an empty port.")


def test_the_launch_script_uses_the_variables():
    text = START.read_text()
    literals = re.findall(r"--port (\d{4})", text)
    assert not literals, (
        f"start-velaris.sh still launches on literal port(s) {literals}.")


@pytest.mark.parametrize("unit,var", [
    ("velaris-engine.service", "VELARIS_ENGINE_PORT"),
    ("velaris-case-service.service", "VELARIS_CASE_PORT"),
    ("velaris-analytics-service.service", "VELARIS_ANALYTICS_PORT"),
    ("velaris-hxgraph-service.service", "VELARIS_HXGRAPH_PORT"),
    ("velaris-hxmigrate-service.service", "VELARIS_HXMIGRATE_PORT"),
    ("velaris-commerce-service.service", "VELARIS_COMMERCE_PORT"),
    ("velaris-portal-service.service", "VELARIS_PORTAL_PORT"),
    ("velaris-worker-service.service", "VELARIS_WORKER_PORT"),
    ("velaris-studio.service", "VELARIS_STUDIO_PORT"),
])
def test_units_take_their_port_from_the_environment(unit, var):
    path = UNITS / unit
    if not path.exists():
        pytest.skip(f"{unit} not present")
    text = path.read_text()
    exec_lines = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
    assert exec_lines, f"{unit} has no ExecStart"
    assert f"${{{var}}}" in exec_lines[0], (
        f"{unit} must launch on ${{{var}}}, not a literal.")
    # The fallback has to be readable BEFORE the file that overrides it, or the
    # unit's own default would win over the operator's .env.
    env_line = text.index(f"Environment={var}=")
    file_line = text.index("EnvironmentFile=")
    assert env_line < file_line, (
        f"{unit}: Environment={var} must come before EnvironmentFile, so .env "
        "overrides the default rather than the other way round.")


def test_studio_refuses_to_drift_to_another_port():
    """The whole reason this file exists: vite is the one that moves quietly."""
    path = UNITS / "velaris-studio.service"
    if not path.exists():
        pytest.skip("studio unit not present")
    exec_line = next(ln for ln in path.read_text().splitlines()
                     if ln.startswith("ExecStart="))
    assert "--strictPort" in exec_line, (
        "vite preview without --strictPort takes the next free port when its "
        "own is busy: the unit says one number, the browser needs another, and "
        "nothing reports a failure.")


def test_the_installer_and_the_updater_both_preflight():
    """A check only the installer runs cannot protect an upgrade."""
    for script, needs in (("setup-velaris.sh", "_velaris_ports_preflight"),
                          ("update-velaris.sh", "_velaris_ports_preflight")):
        text = (ROOT / script).read_text()
        assert needs in text, f"{script} does not check ports before binding them"


def test_both_scripts_stop_a_migration_at_its_first_error():
    """A migration that fails must FAIL, on an upgrade as much as on an install.

    Without `-v ON_ERROR_STOP=1`, psql runs the statements after a rejected one,
    exits 0, and the caller records the file in `schema_migrations` as applied —
    a half-applied migration that will never run again. `setup-velaris.sh`
    learned this from migration 080 and carries the flag with a comment; the
    UPDATE path, which is the one that runs unattended on somebody else's
    machine, did not have it until 2026-09-10.
    """
    for script in ("setup-velaris.sh", "update-velaris.sh"):
        text = (ROOT / script).read_text()
        assert "migration_file" in text, f"{script} no longer applies migrations"
        assert "ON_ERROR_STOP=1" in text, (
            f"{script} applies migrations without ON_ERROR_STOP — a rejected "
            f"statement would be recorded as a successful migration")


def test_the_updater_never_prompts_for_a_port():
    """It runs on a timer; a question there hangs forever, and a port chosen by
    a script at 3am is a port nobody knows about."""
    text = (ROOT / "update-velaris.sh").read_text()
    start = text.index("_velaris_ports_preflight")
    window = text[start - 1200:start + 600]
    assert "read -r" not in window, (
        "update-velaris.sh must not prompt around its port check — it runs "
        "unattended.")


# ── System dependencies ───────────────────────────────────────────────────────
#
# Same failure shape as the ports, one layer down: the update syncs Python and
# Node dependencies and pulls images, but cannot install an apt package. A
# release that starts using a new system tool installs cleanly on a NEW machine
# and breaks every existing one at runtime.

DEPS_SH = ROOT / "deploy" / "deps" / "velaris-deps.sh"


def test_the_dependency_list_exists():
    assert DEPS_SH.is_file(), f"{DEPS_SH} is the one place system binaries are named"


def test_setup_and_update_read_the_same_list():
    """Two copies of this list is how an update demands a package the installer
    never installs — or the reverse."""
    for script in ("setup-velaris.sh", "update-velaris.sh"):
        text = (ROOT / script).read_text()
        assert "deploy/deps/velaris-deps.sh" in text, (
            f"{script} does not read the shared dependency list")


def test_setup_does_not_keep_its_own_copy():
    """`require_cmd` calls with literal package names are the old, drifting form."""
    text = (ROOT / "setup-velaris.sh").read_text()
    literals = re.findall(r'^require_cmd (\w[\w-]*) ', text, re.M)
    assert not literals, (
        f"setup-velaris.sh still hardcodes required commands {literals} instead "
        "of reading deploy/deps/velaris-deps.sh.")


def test_the_updater_halts_rather_than_installing():
    """apt needs root and the updater runs on a timer — it must not install."""
    text = (ROOT / "update-velaris.sh").read_text()
    start = text.index("_velaris_deps_missing")
    window = text[start:start + 900]
    assert "exit 1" in window, "update-velaris.sh must halt when a package is missing"
    assert not re.search(r"^\s*sudo apt-get install", window, re.M), (
        "update-velaris.sh must print the install command, never run it.")


# ── Gateway rate limiting (roadmap #24) ──────────────────────────────────────
#
# §2.5 proposed doing this at a CDN edge, which is the wrong shape for a
# self-hosted product and was never built — so until 2026-08-24 the gateway had
# no rate limiting at all, while the in-process limiter it left the job to was
# per-service (four buckets for one attacker since 13c-e) and reset on restart.

def test_the_gateway_rate_limits():
    conf = NGINX.read_text()
    assert "limit_req_zone" in conf, (
        "the gateway declares no rate limiting, so the only ceiling on abuse is "
        "an in-process limiter that resets on restart and is per service.")
    assert "limit_req_status 429" in conf


def test_the_public_portal_has_its_own_tighter_zone():
    """The anonymous surface must not share a budget with authenticated Studio
    traffic — one of them bursts legitimately and the other is the attack."""
    conf = NGINX.read_text()
    # The rate itself is substituted (see the configurability test below), so
    # what is pinned here is that the portal has a zone of its OWN — sharing
    # the API zone is the defect, whatever number is in it.
    assert re.search(r"zone=velaris_portal:\S+\s+rate=\S+", conf), (
        "the portal needs its own zone")
    assert "VELARIS_GW_RATE_PORTAL" in conf, (
        "the portal rate must be separately settable from the API rate, or an "
        "operator raising one raises the anonymous surface too")
    portal_block = conf[conf.index("location /api/v1/portal"):]
    portal_block = portal_block[:portal_block.index("}")]
    assert "limit_req zone=velaris_portal" in portal_block


def test_the_limit_is_not_keyed_on_a_client_controlled_header():
    """`X-Forwarded-For` is set by the caller. Keying a rate limit on it is a
    rate-limit bypass with extra steps; behind a real proxy the answer is
    `set_real_ip_from`, not trust.

    Since R6 (2026-10-30) the key is `$velaris_client`: the address Studio's own
    server names in X-Velaris-Client, believed ONLY from a loopback peer, and the
    peer's address otherwise — a remote caller's header is never used."""
    conf = NGINX.read_text()
    for m in re.finditer(r"limit_req_zone\s+(\S+)", conf):
        assert m.group(1) == "$velaris_client", (
            f"rate-limit key {m.group(1)} is client-controlled")
    assert re.search(r"map \$remote_addr \$velaris_loopback_peer \{ 127\.0\.0\.1 1; default 0; \}", conf)
    block = conf[conf.index('map "$velaris_loopback_peer|'):]
    block = block[:block.index("\n    }")]
    assert '"~^1\\|' in block and "default $remote_addr;" in block


def _no_placeholders(conf: str) -> str:
    """`${VELARIS_...}` contains a closing brace, so a naive "block ends at the
    first }" scan stops INSIDE a proxy_pass and reads half a location. Three
    locations were silently skipped by exactly that before this existed."""
    return re.sub(r"\$\{[^}]*\}", "X", conf)


def _main_server_block(conf: str) -> str:
    """The public listener, up to the apps-network edge listener below it.

    The edge listener is deliberately unlimited (its every request arrives via
    one broker container, so one bucket would be shared by every bot), so it is
    excluded here rather than asserted about.
    """
    start = conf.index("server {")
    end = conf.index("# ── Apps-network edge listener")
    return conf[start:end]


def test_every_proxying_location_is_limited_not_just_the_catch_all():
    """A limit on some paths is a limit on none — the attacker picks the rest.

    This is the test that should have existed on 2026-08-24 and did not. The
    limit was declared in `location /` alone, and **nginx does not add an
    enclosing limit to a location that has none of its own** — it inherits only
    when the enclosing level declares one, and neither http nor server did. So
    eleven named prefixes (analytics, process-mining, graph, hxmigrate,
    importer, scout, scout-ai, orchestrator, realtime, hxstream, meet) proxied
    with no rate limit whatsoever, while the test below passed.
    """
    conf = _no_placeholders(NGINX.read_text())
    server = _main_server_block(conf)
    # A server-level declaration covers every location that declares none.
    # Read the block's own directives — everything before the first `location`
    # DIRECTIVE, not before the first time the word appears in a comment.
    head = []
    for line in server.splitlines():
        if re.match(r"\s*location\s", line):
            break
        head.append(line)
    inherited = any(
        re.match(r"\s*limit_req zone=", line) for line in head)

    unlimited = []
    for m in re.finditer(r"location [^\n]*\{", server):
        block = server[m.start():]
        block = block[:block.index("}") + 1]
        # Only the locations that actually forward traffic need a limit; one
        # that returns a literal response or a 404 reaches no service.
        if "proxy_pass" not in block:
            continue
        if "limit_req zone=" in block or inherited:
            continue
        unlimited.append(block.splitlines()[0].strip())
    assert not unlimited, (
        f"location(s) proxy without any rate limit: {unlimited}")


def test_the_rates_are_configurable_but_default_to_what_ships():
    """An operator raising a ceiling must not have to edit a repository file an
    update overwrites — and an install that sets nothing must render exactly
    the limits it renders today."""
    conf = NGINX.read_text()
    compose = COMPOSE.read_text()
    for var, shipped in (
        ("VELARIS_GW_RATE_API", "20r/s"),
        ("VELARIS_GW_RATE_PORTAL", "30r/m"),
        ("VELARIS_GW_WS_CONN_MAX", "256"),
    ):
        assert "${%s}" % var in conf, f"{var} is not substituted into the gateway config"
        assert f'{var}: "${{{var}:-{shipped}}}"' in compose, (
            f"{var} must default to {shipped} in compose — `:-` also covers a "
            "key that is present but EMPTY, which is how an unset value took "
            "the gateway down on 2026-08-24.")
        assert var in re.search(r"NGINX_ENVSUBST_FILTER: \"([^\"]+)\"", compose).group(1), (
            f"{var} is not in NGINX_ENVSUBST_FILTER, so it renders as the "
            "literal text ${%s} and nginx refuses to start" % var)


def test_a_bad_limit_in_env_is_refused_before_anything_is_touched():
    """nginx validates a rate when it STARTS. A typo is therefore not a bad
    ceiling, it is a gateway that will not boot — the same shape as the empty
    identity secret that took the platform down on 2026-08-24. Both scripts
    check it while nothing has been changed yet."""
    for script in ("update-velaris.sh", "setup-velaris.sh"):
        text = (ROOT / script).read_text()
        assert "VELARIS_GW_RATE_API" in text, (
            f"{script} does not validate the gateway limits before starting nginx")
        assert "[1-9][0-9]*r/[sm]" in text, (
            f"{script} does not check the rate SHAPE, only its presence")


def test_the_long_lived_paths_have_a_connection_cap():
    """A request limit does not bound a websocket: one request, then the
    connection is held for up to an hour. Without limit_conn one host can hold
    every worker connection and no rate limit says a word."""
    conf = _no_placeholders(NGINX.read_text())
    assert "limit_conn_zone" in conf
    server = _main_server_block(conf)
    for path in ("/api/v1/realtime", "/api/v1/hxstream", "/api/v1/meet"):
        block = server[server.index(f"location {path} {{"):]
        block = block[:block.index("}") + 1]
        assert "limit_conn velaris_ws" in block, (
            f"{path} holds a connection for proxy_read_timeout and has no cap")

def test_the_gateway_accepts_a_body_as_large_as_the_app_does():
    """nginx's DEFAULT `client_max_body_size` is 1 MB, and this file never set
    one, while `config.py::max_upload_bytes` allows 25 MB. The two disagreed by
    25x, so every document over 1 MB was refused at the door with a bare nginx
    413 — no code, no reason — by a platform that believed it accepted it.
    Proven against the running gateway on 2026-09-09: a 2 MB body → 413.

    A scanned PDF, which is exactly what the OCR path exists to read, is almost
    always over 1 MB."""
    conf = NGINX.read_text()
    match = re.search(r"client_max_body_size\s+(\d+)([kKmMgG]?);", conf)
    assert match, (
        "the gateway sets no body ceiling, so nginx's 1 MB default silently "
        "overrides the application's 25 MB limit")
    size = int(match.group(1))
    unit = (match.group(2) or "").lower()
    as_bytes = size * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit]

    config_py = (ROOT / "services" / "_shared" / "velaris_shared" / "config.py").read_text()
    app_limit = re.search(r"max_upload_bytes:\s*int\s*=\s*(\d+)\s*\*\s*(\d+)\s*\*\s*(\d+)",
                          config_py)
    assert app_limit, "max_upload_bytes moved — this test needs updating with it"
    app_bytes = int(app_limit.group(1)) * int(app_limit.group(2)) * int(app_limit.group(3))
    assert as_bytes >= app_bytes, (
        f"the gateway caps bodies at {as_bytes} bytes while the application "
        f"accepts {app_bytes} — the app must be what refuses, with a coded "
        f"error, not nginx with a bare 413")
