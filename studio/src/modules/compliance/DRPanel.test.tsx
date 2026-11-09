/**
 * HxDR panel.
 *
 * One assertion matters more than the rest: an untested backup must never
 * render as OK. That is the exact failure disaster recovery exists to prevent,
 * and a UI that rounds amber up to green reintroduces it after the backend
 * carefully avoided it.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DRPanel from "./DRPanel";

function posture(regions: unknown[], extra: Record<string, unknown> = {}) {
  return {
    rpo_hours: 24, rto_hours: 4, restore_test_valid_days: 90,
    regions,
    all_regions_ok: false,
    regions_at_risk: [],
    ...extra,
  };
}

function stub(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response),
  ));
}

const UNTESTED = {
  region: "eu-west", region_id: "r1", is_primary: true,
  state: "untested",
  detail: "never restore-tested — an untested backup is a hope",
  last_backup_at: "2026-08-04T08:00:00Z", last_restore_test_at: null,
};

describe("DRPanel — untested is not OK", () => {
  it("renders UNTESTED for a recent backup that was never restored", async () => {
    stub(posture([UNTESTED]));
    render(<DRPanel />);

    await waitFor(() => expect(screen.getByText("UNTESTED")).toBeInTheDocument());
    expect(screen.queryByText("OK")).not.toBeInTheDocument();
  });

  it("shows why, not just the state", async () => {
    stub(posture([UNTESTED]));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/an untested backup is a hope/i)).toBeInTheDocument(),
    );
  });
});

describe("DRPanel — no single green light", () => {
  it("names the regions at risk rather than one aggregate status", async () => {
    stub(posture(
      [
        UNTESTED,
        { region: "us-east", region_id: "r2", is_primary: false, state: "ok",
          detail: "recent and demonstrably restorable",
          last_backup_at: "2026-08-04T08:00:00Z",
          last_restore_test_at: "2026-07-20T08:00:00Z" },
      ],
      { regions_at_risk: ["eu-west"] },
    ));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/At risk: eu-west/i)).toBeInTheDocument(),
    );
  });

  it("confirms all-clear only when every region is ok", async () => {
    stub(posture(
      [{ region: "us-east", region_id: "r2", is_primary: true, state: "ok",
         detail: "recent and demonstrably restorable",
         last_backup_at: "2026-08-04T08:00:00Z",
         last_restore_test_at: "2026-07-20T08:00:00Z" }],
      { all_regions_ok: true },
    ));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/demonstrably restorable backup/i)).toBeInTheDocument(),
    );
  });
});

describe("DRPanel — nothing registered", () => {
  it("explains the invisible-backup trap instead of showing an empty table", async () => {
    stub(posture([]));
    render(<DRPanel />);

    await waitFor(() =>
      expect(screen.getByText(/No regions are registered/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/stored but never shown/i)).toBeInTheDocument();
  });

  it("offers no restore-drill buttons for a region with no backup", async () => {
    stub(posture([{
      region: "eu-west", region_id: "r1", is_primary: false,
      state: "no_backup", detail: "no successful backup recorded",
      last_backup_at: null, last_restore_test_at: null,
    }]));
    render(<DRPanel />);

    await waitFor(() => expect(screen.getByText("NO BACKUP")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^passed$/i })).not.toBeInTheDocument();
  });
});

describe("DRPanel — backups on this server (R5a step c)", () => {
  it("shows every finding, and a private key left on the server as YES", async () => {
    stub(posture([UNTESTED], {
      backup_operations: {
        available: true,
        facts: { encrypted: true, timer_installed: false, private_key_on_server: true,
                 last_offbox: {}, last_outcome: "ok", last_good_at: "2026-10-16T02:00:00Z" },
        findings: [
          { code: "private_key_on_server", severity: "high",
            message: "The backup private key is still in velaris-install-report.md on this server." },
          { code: "no_backup_timer", severity: "medium", message: "No daily backup timer" },
        ],
      },
    }));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText("Backups on this server")).toBeInTheDocument());
    expect(screen.getByText(/private key is still in velaris-install-report.md/)).toBeInTheDocument();
    expect(screen.getByText(/No daily backup timer/)).toBeInTheDocument();
    expect(screen.getByText("YES")).toBeInTheDocument();
    expect(screen.queryByText(/proven to restore\.$/)).not.toBeInTheDocument();
  });

  it("says nothing about server backups on a server that does not report them", async () => {
    stub(posture([UNTESTED]));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText("UNTESTED")).toBeInTheDocument());
    expect(screen.queryByText("Backups on this server")).not.toBeInTheDocument();
  });
});

describe("DRPanel — standby server (R5c)", () => {
  it("offers a standby without warning when none is linked", async () => {
    stub(posture([UNTESTED], { standby: { linked: false, role: null, facts: { linked: false }, findings: [], events: [] } }));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText("Standby server")).toBeTruthy());
    const card = within(screen.getByText("Standby server").parentElement as HTMLElement);
    expect(card.getByText(/No standby is linked/)).toBeTruthy();
    expect(card.queryByText(/✗|⚠/)).toBeNull();
  });

  it("shows a silent standby as a failure, never as healthy", async () => {
    stub(posture([UNTESTED], { standby: {
      linked: true, role: "main",
      facts: { linked: true, standby: "s2.example", state: "streaming", version: "3.2.0", standby_version: "3.2.0",
               databases: { velaris: { state: "streaming", replay_lag_seconds: 0 } }, parts: { docs: "ok" } },
      findings: [{ code: "standby_silent", severity: "high", message: "No report from the standby s2.example since …" }],
      events: [{ event: "link", peer: "s2.example", occurred_at: "2026-10-22T10:00:00Z", detail: {} }],
    } }));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText(/No report from the standby/)).toBeTruthy());
    expect(screen.queryByText(/receives every change/)).toBeNull();
    expect(screen.getByText("link")).toBeTruthy();
  });

  it("is absent on a server older than R5c", async () => {
    stub(posture([UNTESTED]));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText("Per region")).toBeTruthy());
    expect(screen.queryByText("Standby server")).toBeNull();
  });
});

describe("DRPanel — automatic failover (step 12)", () => {
  const base = {
    linked: true, role: "main" as const,
    facts: { linked: true, standby: "s2.example", state: "streaming", version: "3.2.0", standby_version: "3.2.0",
             databases: { velaris: { state: "streaming", replay_lag_seconds: 0 } }, parts: { docs: "ok" } },
    events: [],
  };

  it("shows who holds the lease and a live agent", async () => {
    stub(posture([UNTESTED], { standby: { ...base, findings: [], failover: {
      witness: "op@w.example", node: "a", term: 3, holding: true, lease_seconds: 30,
      agent_at: new Date().toISOString(), agent_role: "main", fenced_at: null } } }));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText("Automatic failover")).toBeTruthy());
    expect(screen.getByText("op@w.example")).toBeTruthy();
    expect(screen.getByText("held here (term 3)")).toBeTruthy();
    expect(screen.getByText("running")).toBeTruthy();
  });

  it("puts a self-stopped server and a dead agent in front, never as healthy", async () => {
    stub(posture([UNTESTED], { standby: { ...base, failover: {
      witness: "op@w.example", term: 1, holding: true, agent_at: null, fenced_at: "2026-10-29T20:00:00Z" },
      findings: [
        { code: "failover_fenced_here", severity: "high", message: "This server STOPPED ITSELF at …" },
        { code: "failover_agent_down", severity: "high", message: "Automatic failover is enabled but its agent is not running here" },
      ] } }));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText(/STOPPED ITSELF/)).toBeTruthy());
    expect(screen.getByText("not running")).toBeTruthy();
    expect(screen.getByText(/stopped itself/)).toBeTruthy();
    expect(screen.queryByText(/receives every change/)).toBeNull();
  });

  it("is absent when failover is not enabled", async () => {
    stub(posture([UNTESTED], { standby: { ...base, findings: [] } }));
    render(<DRPanel />);
    await waitFor(() => expect(screen.getByText("Standby server")).toBeTruthy());
    expect(screen.queryByText("Automatic failover")).toBeNull();
  });
});
