import { describe, expect, it } from "vitest";
import { buildClimatologyWindowDates } from "./calculateClimatology";

describe("buildClimatologyWindowDates", () => {
  it("builds a ±7 day window around the target date for one year", () => {
    const dates = buildClimatologyWindowDates("2026-07-21", [2026]);
    expect(dates).toHaveLength(15); // 7 before + target + 7 after
    expect(dates).toContain("2026-07-21");
    expect(dates).toContain("2026-07-14");
    expect(dates).toContain("2026-07-28");
    expect(dates).not.toContain("2026-07-13");
    expect(dates).not.toContain("2026-07-29");
  });

  it("combines windows across every year given, deduplicated", () => {
    const dates = buildClimatologyWindowDates("2026-07-21", [2024, 2025]);
    expect(dates).toHaveLength(30); // 15 per year, no overlap between different years
    expect(dates).toContain("2024-07-21");
    expect(dates).toContain("2025-07-21");
    expect(dates).not.toContain("2026-07-21");
  });

  it("handles a month boundary correctly (real Date arithmetic, not string matching)", () => {
    const dates = buildClimatologyWindowDates("2026-08-02", [2026]);
    expect(dates).toContain("2026-07-26"); // 7 days before Aug 2
    expect(dates).toContain("2026-08-09"); // 7 days after
  });

  it("handles a year boundary correctly", () => {
    const dates = buildClimatologyWindowDates("2026-01-02", [2026]);
    expect(dates).toContain("2025-12-26");
    expect(dates).toContain("2026-01-09");
  });

  it("handles a leap-year February correctly", () => {
    // 2024 is a leap year — Feb 29 exists and must not be silently skipped/miscounted.
    const dates = buildClimatologyWindowDates("2024-03-02", [2024]);
    expect(dates).toContain("2024-02-29");
    expect(dates).toContain("2024-02-24");
    expect(dates).toContain("2024-03-09");
  });
});
