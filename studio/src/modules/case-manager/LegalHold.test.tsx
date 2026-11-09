/**
 * Legal holds — what the screen offers to whom.
 *
 * The server decides; what is pinned here is that the screen follows it:
 *
 *  * a case reader with no legal privilege sees THAT the case is held and
 *    nothing to act on — no form, no matter, no Release;
 *  * Place needs a matter and a ten-character reason before it is live;
 *  * a per-case refusal from the server is shown, not swallowed;
 *  * the Compliance panel groups by matter and offers one release for it.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    getCaseLegalHold: vi.fn(),
    listLegalHolds: vi.fn(),
    placeLegalHold: vi.fn(),
    releaseLegalHold: vi.fn(async () => ({})),
    releaseLegalMatter: vi.fn(async () => ({})),
    approveLegalRelease: vi.fn(async () => ({})),
    rejectLegalRelease: vi.fn(async () => ({})),
    getLegalHoldSettings: vi.fn(async () => { throw new Error("older server"); }),
    setLegalHoldApprover: vi.fn(),
  };
});

import LegalHoldsPanel, { LegalHoldBanner, LegalHoldTab } from "./LegalHold";
import * as client from "@shared/api/client";

const hold = {
  id: "h1", case_id: "c1", case_number: "HLX-1", tenant_id: "t", matter_ref: "Claim 114",
  reason: "Preserve everything.", status: "active" as const, placed_by: "alice",
  placed_at: "2026-10-11T10:00:00Z", released_by: null, released_at: null, release_reason: null,
};
const status = (over: Record<string, unknown>) => ({
  case_id: "c1", held: false, frozen: [], can_read: false, can_place: false,
  can_release: false, ...over,
});

beforeEach(() => { vi.clearAllMocks(); vi.spyOn(window, "confirm").mockReturnValue(true); });

describe("On a case", () => {
  it("tells a case worker it is held, and offers nothing", () => {
    render(<LegalHoldTab caseId="c1" onChanged={() => {}}
                         status={status({ held: true, frozen: ["It cannot be deleted."] }) as never} />);
    expect(screen.getByText("Under a legal hold")).toBeTruthy();
    expect(screen.getByText("It cannot be deleted.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Place legal hold/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Release/ })).toBeNull();
    expect(screen.queryByText("Claim 114")).toBeNull();
  });

  it("shows the banner only while held", () => {
    const { container, rerender } = render(<LegalHoldBanner status={null} />);
    expect(container.textContent).toBe("");
    rerender(<LegalHoldBanner status={status({}) as never} />);
    expect(container.textContent).toBe("");
    rerender(<LegalHoldBanner status={status({ held: true }) as never} />);
    expect(screen.getByText("This case is under a legal hold")).toBeTruthy();
  });

  it("keeps Place disabled until there is a matter and a real reason", async () => {
    vi.mocked(client.placeLegalHold).mockResolvedValue(
      { placed: 1, requested: 1, results: [{ case_id: "c1", ok: true }] } as never);
    const changed = vi.fn();
    const user = userEvent.setup();
    render(<LegalHoldTab caseId="c1" onChanged={changed}
                         status={status({ can_read: true, can_place: true }) as never} />);
    const button = screen.getByRole("button", { name: "Place legal hold" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByLabelText("Matter"), "Claim 114");
    await user.type(screen.getByLabelText("Reason"), "short");
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByLabelText("Reason"), " — now long enough");
    await user.click(button);
    await waitFor(() => expect(changed).toHaveBeenCalled());
    expect(client.placeLegalHold).toHaveBeenCalledWith(["c1"], "Claim 114", "short — now long enough");
  });

  it("shows the server's refusal for the case", async () => {
    vi.mocked(client.placeLegalHold).mockResolvedValue({ placed: 0, requested: 1, results: [
      { case_id: "c1", ok: false, error: "This case is already under hold for matter 'Claim 114'." },
    ] } as never);
    const changed = vi.fn();
    const user = userEvent.setup();
    render(<LegalHoldTab caseId="c1" onChanged={changed}
                         status={status({ can_place: true }) as never} />);
    await user.type(screen.getByLabelText("Matter"), "Claim 114");
    await user.type(screen.getByLabelText("Reason"), "Preserve everything.");
    await user.click(screen.getByRole("button", { name: "Place legal hold" }));
    expect(await screen.findByText(/already under hold/)).toBeTruthy();
    expect(changed).not.toHaveBeenCalled();
  });

  it("offers Release only to whoever the server says may, and needs a reason", async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    const { rerender } = render(<LegalHoldTab caseId="c1" onChanged={changed} status={status({
      held: true, can_read: true, holds: [hold] }) as never} />);
    expect(screen.getByText("Claim 114")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Release/ })).toBeNull();

    rerender(<LegalHoldTab caseId="c1" onChanged={changed} status={status({
      held: true, can_read: true, can_release: true, holds: [hold] }) as never} />);
    await user.click(screen.getByRole("button", { name: "Release…" }));
    const go = screen.getByRole("button", { name: "Release hold" });
    expect((go as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByLabelText("Reason for release"), "Matter settled today.");
    await user.click(go);
    await waitFor(() => expect(client.releaseLegalHold).toHaveBeenCalledWith("h1", "Matter settled today."));
    expect(changed).toHaveBeenCalled();
  });
});

describe("Compliance → Legal Holds", () => {
  it("groups holds by matter and releases a matter in one go", async () => {
    vi.mocked(client.listLegalHolds).mockResolvedValue({ count: 3, can_release: true, frozen: ["It cannot be deleted."], holds: [
      hold,
      { ...hold, id: "h2", case_id: "c2", case_number: "HLX-2" },
      { ...hold, id: "h3", case_id: "c3", case_number: "HLX-3", matter_ref: "Inquiry 7" },
    ] } as never);
    const user = userEvent.setup();
    render(<LegalHoldsPanel />);
    await screen.findByText("HLX-3");
    expect(client.listLegalHolds).toHaveBeenCalledWith({ status: "active" });
    // one Release per hold, plus one for the matter that spans two cases
    const releases = screen.getAllByRole("button", { name: "Release…" });
    expect(releases).toHaveLength(4);
    await user.click(releases[0]);
    await user.type(screen.getByLabelText("Reason for release"), "Matter settled today.");
    await user.click(screen.getByRole("button", { name: "Release hold" }));
    await waitFor(() => expect(client.releaseLegalMatter).toHaveBeenCalledWith(
      "Claim 114", "Matter settled today."));
  });

  it("offers no Release to a reader who may not release", async () => {
    vi.mocked(client.listLegalHolds).mockResolvedValue(
      { count: 1, can_release: false, frozen: [], holds: [hold] } as never);
    render(<LegalHoldsPanel />);
    await screen.findByText("HLX-1");
    expect(screen.queryByRole("button", { name: "Release…" })).toBeNull();
  });

  it("says so when nothing is held, and shows the server's refusal when it may not be read", async () => {
    vi.mocked(client.listLegalHolds).mockResolvedValueOnce({ count: 0, frozen: [], holds: [] } as never);
    const { unmount } = render(<LegalHoldsPanel />);
    expect(await screen.findByText("No case is under a legal hold")).toBeTruthy();
    unmount();
    vi.mocked(client.listLegalHolds).mockRejectedValueOnce(new Error("Not permitted"));
    render(<LegalHoldsPanel />);
    expect(await screen.findByText("Not permitted")).toBeTruthy();
    expect(screen.queryByText("No case is under a legal hold")).toBeNull();
  });
});

// ── Release approval (mig 218) ───────────────────────────────────────────────

const req = { id: "r1", status: "pending", reason: "Matter settled today.",
              requested_by: "alice", requested_at: "2026-10-14T10:00:00Z" };

describe("Release approval", () => {
  it("asks rather than releases when the tenant names an approving group", async () => {
    const user = userEvent.setup();
    render(<LegalHoldTab caseId="c1" onChanged={() => {}} status={status({
      held: true, can_read: true, can_release: true,
      holds: [{ ...hold, approval_required: true }] }) as never} />);
    await user.click(screen.getByRole("button", { name: "Ask to release…" }));
    await user.type(screen.getByLabelText("Reason for release"), "Matter settled today.");
    await user.click(screen.getByRole("button", { name: "Ask for release" }));
    await waitFor(() => expect(client.releaseLegalHold).toHaveBeenCalledWith("h1", "Matter settled today."));
  });

  it("offers an approver Approve and Refuse, and no second Release", async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    render(<LegalHoldTab caseId="c1" onChanged={changed} status={status({
      held: true, can_read: true, can_release: true,
      holds: [{ ...hold, approval_required: true, pending_release: req, can_approve: true }] }) as never} />);
    expect(screen.getByText("release awaiting approval")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Ask to release/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Approve release" }));
    await waitFor(() => expect(client.approveLegalRelease).toHaveBeenCalledWith("h1"));
    expect(changed).toHaveBeenCalled();
  });

  it("lets the asker withdraw, with a reason, but not approve", async () => {
    const user = userEvent.setup();
    render(<LegalHoldTab caseId="c1" onChanged={() => {}} status={status({
      held: true, can_read: true, can_release: true,
      holds: [{ ...hold, approval_required: true, pending_release: req, can_withdraw: true }] }) as never} />);
    expect(screen.queryByRole("button", { name: "Approve release" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Withdraw request…" }));
    const go = screen.getByRole("button", { name: "Withdraw" });
    expect((go as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByLabelText("Reason for withdrawing"), "Asked by mistake.");
    await user.click(go);
    await waitFor(() => expect(client.rejectLegalRelease).toHaveBeenCalledWith("h1", "Asked by mistake."));
  });

  it("tells anyone else who must approve, and offers them nothing", () => {
    render(<LegalHoldTab caseId="c1" onChanged={() => {}} status={status({
      held: true, can_read: true, can_release: true,
      holds: [{ ...hold, approval_required: true, pending_release: req }] }) as never} />);
    expect(screen.getByText(/must approve it/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Approve|Refuse|Withdraw|Release/ })).toBeNull();
  });

  it("names the group, and lets only whoever may change it", async () => {
    vi.mocked(client.listLegalHolds).mockResolvedValue({ count: 0, frozen: [], holds: [] } as never);
    const settings = {
      available: true, approver_group_id: "g1",
      approver_group: { id: "g1", name: "Compliance", is_active: true, members: 2 },
      approver_group_unusable: false, can_configure: true, can_change: false,
      groups: [{ id: "g1", name: "Compliance", is_active: true, members: 2 }],
    };
    vi.mocked(client.getLegalHoldSettings).mockResolvedValueOnce(settings as never);
    const { unmount } = render(<LegalHoldsPanel />);
    expect(await screen.findByText("Compliance")).toBeTruthy();
    expect(screen.queryByLabelText("Approving group")).toBeNull();
    expect(screen.getByText(/cannot switch it off/)).toBeTruthy();
    unmount();

    vi.mocked(client.getLegalHoldSettings).mockResolvedValueOnce(
      { ...settings, approver_group_id: null, approver_group: null, can_change: true } as never);
    vi.mocked(client.setLegalHoldApprover).mockResolvedValueOnce(settings as never);
    const user = userEvent.setup();
    render(<LegalHoldsPanel />);
    await user.selectOptions(await screen.findByLabelText("Approving group"), "g1");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(client.setLegalHoldApprover).toHaveBeenCalledWith("g1"));
  });

  it("warns when the named group can no longer approve", async () => {
    vi.mocked(client.listLegalHolds).mockResolvedValue({ count: 0, frozen: [], holds: [] } as never);
    vi.mocked(client.getLegalHoldSettings).mockResolvedValueOnce({
      available: true, approver_group_id: "g1", approver_group: null, approver_group_unusable: true,
      can_configure: false, can_change: false, groups: [] } as never);
    render(<LegalHoldsPanel />);
    expect(await screen.findByText("The approving group cannot approve")).toBeTruthy();
  });
});
