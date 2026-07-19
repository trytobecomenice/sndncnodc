import { describe, expect, it } from "vitest";
import { checkStaleness, getStationLocalTime } from "./staleness";

describe("getStationLocalTime", () => {
  it("converts a UTC instant to the correct local date/hour for a positive-offset zone", () => {
    // 2026-07-21 02:00 UTC == 2026-07-21 11:00 Asia/Seoul (UTC+9)
    const result = getStationLocalTime("Asia/Seoul", new Date("2026-07-21T02:00:00Z"));
    expect(result.date).toBe("2026-07-21");
    expect(result.hour).toBe(11);
  });

  it("rolls the local calendar day forward across a UTC midnight boundary", () => {
    // 2026-07-21 20:00 UTC == 2026-07-22 05:00 Asia/Seoul (UTC+9) — next local day
    const result = getStationLocalTime("Asia/Seoul", new Date("2026-07-21T20:00:00Z"));
    expect(result.date).toBe("2026-07-22");
    expect(result.hour).toBe(5);
  });

  it("rolls the local calendar day backward for a negative-offset zone", () => {
    // 2026-07-21 02:00 UTC == 2026-07-20 22:00 America/New_York (UTC-4 in July, DST)
    const result = getStationLocalTime("America/New_York", new Date("2026-07-21T02:00:00Z"));
    expect(result.date).toBe("2026-07-20");
    expect(result.hour).toBe(22);
  });
});

describe("checkStaleness", () => {
  it("is not stale for a forecast day strictly in the future", () => {
    const result = checkStaleness("2026-07-22", "Asia/Seoul", new Date("2026-07-21T02:00:00Z"));
    expect(result.isStale).toBe(false);
    expect(result.isSameDay).toBe(false);
  });

  it("is stale for a forecast day strictly in the past", () => {
    const result = checkStaleness("2026-07-19", "Asia/Seoul", new Date("2026-07-21T02:00:00Z"));
    expect(result.isStale).toBe(true);
    expect(result.isSameDay).toBe(false);
  });

  it("allows same-day trading before the 18:00 station-local cutoff", () => {
    // 11:00 local Seoul time, same day as the forecast
    const result = checkStaleness("2026-07-21", "Asia/Seoul", new Date("2026-07-21T02:00:00Z"));
    expect(result.isStale).toBe(false);
    expect(result.isSameDay).toBe(true);
  });

  it("marks same-day as stale once the station-local clock passes 18:00", () => {
    // 2026-07-21 09:30 UTC == 2026-07-21 18:30 Asia/Seoul — past the cutoff
    const result = checkStaleness("2026-07-21", "Asia/Seoul", new Date("2026-07-21T09:30:00Z"));
    expect(result.isStale).toBe(true);
    expect(result.isSameDay).toBe(true);
  });

  it("treats exactly 18:00 station-local as already stale (cutoff is inclusive)", () => {
    // 2026-07-21 09:00 UTC == 2026-07-21 18:00 Asia/Seoul exactly
    const result = checkStaleness("2026-07-21", "Asia/Seoul", new Date("2026-07-21T09:00:00Z"));
    expect(result.isStale).toBe(true);
  });
});
