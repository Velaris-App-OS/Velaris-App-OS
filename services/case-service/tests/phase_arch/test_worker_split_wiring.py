"""R5e step 9 — the worker service, wired end to end (deploy side).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]


def test_the_worker_app_serves_no_api():
    from case_service.worker.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert paths == {"/health", "/ready", "/metrics"}


def test_new_installs_move_the_jobs_and_updates_never_do():
    setup = (ROOT / "setup-velaris.sh").read_text()
    assert '_write_if_missing VELARIS_CASE_RUN_JOBS          "false"' in setup
    for f in ("update-velaris.sh", "deploy/systemd/refresh-after-update.sh"):
        assert "VELARIS_CASE_RUN_JOBS" not in (ROOT / f).read_text(), f


@pytest.mark.parametrize("units, writes", [
    ((), True),                                                     # new install
    (("velaris-case-service",), False),     # re-run, systemd, no worker unit: jobs would stop
    (("velaris-case-service", "velaris-worker-service"), True),
])
def test_setup_rerun_never_leaves_the_jobs_unrun(tmp_path, units, writes):
    """Found in the step-9 audit: a re-run of setup over an existing systemd
    install installs no units (case-service's exists), so writing RUN_JOBS=false
    there left no process running the scheduler, mail poll or sealers."""
    setup = (ROOT / "setup-velaris.sh").read_text()
    block = re.search(r'^_velaris_units=.*?^fi$', setup, re.S | re.M).group(0)
    for u in units:
        (tmp_path / f"{u}.service").touch()
    env = tmp_path / "env"
    env.touch()
    script = (f'_write_if_missing() {{ echo "$1=$2" >> "{env}"; }}\n'
              f'VELARIS_UNIT_DIR="{tmp_path}"\n{block}\n')
    subprocess.run(["bash", "-c", script], check=True)
    assert ("VELARIS_CASE_RUN_JOBS=false" in env.read_text()) is writes


def test_update_checks_the_worker_only_where_it_runs():
    text = (ROOT / "update-velaris.sh").read_text()
    assert '_WORKER_PROBE="${VELARIS_WORKER_PORT:-8208}:worker-service"' in text
    assert "velaris-worker-service.service\" ]" in text
    assert text.index("_WORKER_PROBE=") < text.index("for _svc in $_SVC_PROBES $_WORKER_PROBE")


def test_systemd_unit_and_lists():
    unit = (ROOT / "deploy/systemd/velaris-worker-service.service").read_text()
    assert "uvicorn case_service.worker.main:app" in unit and "${VELARIS_WORKER_PORT}" in unit
    assert "--proxy-headers" not in unit                       # no gateway in front of it
    for f in ("deploy/systemd/install.sh", "deploy/systemd/refresh-after-update.sh"):
        assert "velaris-worker-service" in (ROOT / f).read_text(), f
    assert 'case_service.worker.main:app) _svc="worker-service"' in (ROOT / "deploy/systemd/velaris-exec.sh").read_text()


def test_internal_tls_knows_it_as_a_server_only_leaf():
    ca = (ROOT / "deploy/tls/make-internal-ca.sh").read_text()
    rot = (ROOT / "deploy/tls/rotate-internal-certs.sh").read_text()
    assert re.search(r"SERVICES=\([^)]*worker-service", ca)
    assert not re.search(r"CALLERS=\([^)]*worker-service", ca)
    assert re.search(r"LEAVES=\([^)]*worker-service", rot)
    assert "velaris-worker-service:8208" in rot


@pytest.mark.parametrize("db", ["Postgres", "MySQL", "MariaDB"])
def test_k8_scripts_keep_its_scale(db):
    assert re.search(r"WORKLOADS=\([^)]*\bjobs\b", (ROOT / f"K8/{db}/_common.sh").read_text())
    assert "[jobs]=jobs" in (ROOT / f"K8/{db}/update-velaris-k8s.sh").read_text()


def _helm(*sets):
    args = ["helm", "template", "t", str(ROOT / "deploy/helm/velaris")]
    for s in sets:
        args += ["--set", s]
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout + r.stderr


@pytest.mark.skipif(subprocess.run(["which", "helm"], capture_output=True).returncode != 0,
                    reason="helm not installed")
class TestHelm:
    def test_off_by_default_and_case_service_keeps_its_jobs(self):
        out = _helm()
        assert "t-helix-jobs" not in out and "VELARIS_CASE_RUN_JOBS" not in out

    def test_on_moves_them(self):
        out = _helm("jobs.enabled=true", "networkPolicy.enabled=true", "podDisruptionBudget.enabled=true")
        assert out.count("name: VELARIS_CASE_RUN_JOBS") == 1
        assert '"uvicorn", "case_service.worker.main:app"' in out
        for kind in ("Deployment", "NetworkPolicy", "PodDisruptionBudget"):
            assert re.search(rf"kind: {kind}\nmetadata:\n  name: t-helix-jobs", out), kind
        assert "kind: Service\nmetadata:\n  name: t-helix-jobs" not in out

    def test_jobs_pod_gets_no_internal_secret(self):
        """It neither serves nor calls /internal/v1 — least privilege."""
        import yaml
        out = _helm("jobs.enabled=true")
        docs = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Deployment"]
        env = {d["metadata"]["name"]: {e["name"] for e in d["spec"]["template"]["spec"]["containers"][0].get("env", [])}
               for d in docs}
        assert "VELARIS_INTERNAL_SERVICE_SECRET" not in env["t-helix-jobs"]
        assert "VELARIS_INTERNAL_SERVICE_SECRET" in env["t-helix-case-service"]


def test_uninstall_stops_and_removes_only_this_installs_units(tmp_path):
    """Found in the step-9 audit: uninstall only pkill'ed processes, and every
    unit (the worker's too) has Restart=always — systemd started them again
    against the database the script then deleted."""
    text = (ROOT / "uninstall-velaris.sh").read_text()
    block = re.search(r'^_unit_dir="\$\{VELARIS_UNIT_DIR.*?(?=^for pattern in)', text, re.S | re.M).group(0)
    units = tmp_path / "units"
    units.mkdir()
    for name, body in (("velaris-worker-service.service", "WorkingDirectory=/opt/this"),
                       ("velaris-update.service", "WorkingDirectory=/opt/this"),
                       ("velaris-update.timer", "OnCalendar=hourly"),
                       ("velaris-backup.timer", "ExecStart=/opt/this/backup"),
                       ("velaris-other.service", "WorkingDirectory=/opt/other"),
                       ("velaris-other.timer", "OnCalendar=daily")):
        (units / name).write_text(body)
    log = tmp_path / "log"
    script = (f'sudo() {{ if [ "$1" = systemctl ]; then echo "$*" >> "{log}"; return 0; fi; "$@"; }}\n'
              'green() { :; }; yellow() { :; }\n'
              f'VELARIS_DIR=/opt/this; VELARIS_UNIT_DIR="{units}"; _stopped_any=false\n{block}')
    subprocess.run(["bash", "-c", script], check=True)
    assert sorted(p.name for p in units.iterdir()) == ["velaris-other.service", "velaris-other.timer"]
    assert "disable --now" in log.read_text() and "velaris-worker-service.service" in log.read_text()
