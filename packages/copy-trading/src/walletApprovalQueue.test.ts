import { describe, expect, it } from "vitest";
import { shouldQueueApprovalRequest, APPROVAL_COOLDOWN_DAYS } from "./walletApprovalQueue";

describe("shouldQueueApprovalRequest", () => {
  it("queues a brand-new candidate with no history at all", () => {
    expect(shouldQueueApprovalRequest("track", null, [])).toBe(true);
  });

  it("queues a candidate currently at a different status (e.g. 'watch')", () => {
    expect(shouldQueueApprovalRequest("track", "watch", [])).toBe(true);
  });

  it("does not queue if the wallet is already at the requested tier", () => {
    expect(shouldQueueApprovalRequest("track", "track", [])).toBe(false);
  });

  it("does not queue a 'bench' request if the wallet is already 'bench'", () => {
    expect(shouldQueueApprovalRequest("bench", "bench", [])).toBe(false);
  });

  it("does not queue if a pending request for this tier already exists", () => {
    const existing = [{ status: "pending", resolvedAt: null }];
    expect(shouldQueueApprovalRequest("track", "watch", existing)).toBe(false);
  });

  it("does not queue within the cooldown window after a rejection", () => {
    const now = new Date("2026-08-01T00:00:00Z");
    const resolvedAt = new Date("2026-07-25T00:00:00Z"); // 7 days ago
    const existing = [{ status: "rejected", resolvedAt }];
    expect(shouldQueueApprovalRequest("track", "watch", existing, now)).toBe(false);
  });

  it("queues again once the cooldown window has fully elapsed", () => {
    const now = new Date("2026-08-01T00:00:00Z");
    const resolvedAt = new Date("2026-07-01T00:00:00Z"); // 31 days ago
    const existing = [{ status: "rejected", resolvedAt }];
    expect(shouldQueueApprovalRequest("track", "watch", existing, now)).toBe(true);
  });

  it("queues exactly at the cooldown boundary edge (just under it)", () => {
    const now = new Date("2026-08-01T00:00:00Z");
    const resolvedAt = new Date(now.getTime() - (APPROVAL_COOLDOWN_DAYS * 86400 * 1000 + 1000));
    const existing = [{ status: "rejected", resolvedAt }];
    expect(shouldQueueApprovalRequest("track", "watch", existing, now)).toBe(true);
  });

  it("uses only the MOST RECENT rejection, not an old one that's already outside cooldown", () => {
    const now = new Date("2026-08-01T00:00:00Z");
    const existing = [
      { status: "rejected", resolvedAt: new Date("2026-01-01T00:00:00Z") }, // long expired
      { status: "rejected", resolvedAt: new Date("2026-07-30T00:00:00Z") }, // 2 days ago — still cooling down
    ];
    expect(shouldQueueApprovalRequest("track", "watch", existing, now)).toBe(false);
  });

  it("ignores an approved-then-later-demoted history — only pending/rejected gate re-queuing", () => {
    const now = new Date("2026-08-01T00:00:00Z");
    const existing = [{ status: "approved", resolvedAt: new Date("2026-07-01T00:00:00Z") }];
    // wallet is 'watch' now (e.g. demoted after approval), status history shouldn't block a fresh ask
    expect(shouldQueueApprovalRequest("track", "watch", existing, now)).toBe(true);
  });

  it("respects a custom cooldownDays override", () => {
    const now = new Date("2026-08-01T00:00:00Z");
    const resolvedAt = new Date("2026-07-30T00:00:00Z"); // 2 days ago
    const existing = [{ status: "rejected", resolvedAt }];
    expect(shouldQueueApprovalRequest("track", "watch", existing, now, 1)).toBe(true);
    expect(shouldQueueApprovalRequest("track", "watch", existing, now, 3)).toBe(false);
  });
});
