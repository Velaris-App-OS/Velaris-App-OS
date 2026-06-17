#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  Velaris platform dependency security scan (Group F / Tier-1 item 10)
#  Local mirror of .github/workflows/security-scan.yml — run before
#  shipping a platform release. Exit code 1 on CRITICAL findings.
#  (Unrelated to HxDeploy, which promotes Studio-built artifacts.)
#
#  Usage:  ./scripts/security-scan.sh [--sbom]
#    --sbom   also generate a CycloneDX SBOM into sbom/ (requires syft)
#
#  Tools (auto-detected, graceful skip when missing):
#    trivy  — severity-graded scan of uv.lock + package-lock.json (preferred)
#    uvx pip-audit — Python fallback when trivy is absent (informational)
#    npm    — npm audit at the critical level
#    syft   — SBOM generation (--sbom)
# ═══════════════════════════════════════════════════════════════════
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

bold()  { echo -e "\033[1m$*\033[0m"; }
green() { echo -e "\033[0;32m$*\033[0m"; }
red()   { echo -e "\033[0;31m$*\033[0m"; }

FAILED=0
WANT_SBOM=false
[[ "${1:-}" == "--sbom" ]] && WANT_SBOM=true

# ── 1. Severity-graded scan (trivy) ────────────────────────────────
if command -v trivy >/dev/null 2>&1; then
  bold "▶ Trivy — full HIGH/CRITICAL report (informational)"
  trivy fs --scanners vuln --severity HIGH,CRITICAL --exit-code 0 . || true

  bold "▶ Trivy — CRITICAL gate"
  if trivy fs --scanners vuln --severity CRITICAL --ignore-unfixed --exit-code 1 -q .; then
    green "  ✓ no fixable CRITICAL vulnerabilities"
  else
    red "  ✗ CRITICAL vulnerabilities found"
    FAILED=1
  fi
else
  # ── Fallback: pip-audit (no severity grading — informational only) ──
  bold "▶ pip-audit (trivy not installed — Python deps, informational)"
  if command -v uv >/dev/null 2>&1; then
    if uv export --format requirements-txt --no-emit-project > /tmp/velaris-req.txt 2>/dev/null \
       && uvx pip-audit -r /tmp/velaris-req.txt 2>&1; then
      green "  ✓ no known Python vulnerabilities"
    else
      echo "  ⚠ vulnerabilities reported above — review them (not graded, so not failing)"
    fi
  else
    echo "  uv not installed — skipped"
  fi
fi

# ── 2. npm audit — critical gate ───────────────────────────────────
bold "▶ npm audit — CRITICAL gate"
if command -v npm >/dev/null 2>&1 && [[ -f package-lock.json ]]; then
  if npm audit --audit-level=critical; then
    green "  ✓ no critical npm vulnerabilities"
  else
    red "  ✗ critical npm vulnerabilities found"
    FAILED=1
  fi
else
  echo "  npm or package-lock.json missing — skipped"
fi

# ── 3. SBOM (optional) ─────────────────────────────────────────────
if [[ "$WANT_SBOM" == true ]]; then
  bold "▶ SBOM (CycloneDX)"
  if command -v syft >/dev/null 2>&1; then
    mkdir -p sbom
    syft dir:. -o cyclonedx-json=sbom/velaris-sbom.cdx.json -q
    green "  ✓ sbom/velaris-sbom.cdx.json"
  else
    echo "  syft not installed — skipped (install: https://github.com/anchore/syft)"
  fi
fi

echo ""
if [[ "$FAILED" -eq 1 ]]; then
  red "Security scan FAILED — critical vulnerabilities present"
  exit 1
fi
green "Security scan passed"
