import { describe, expect, it } from "vitest";
import { resolveTimezone } from "./stationReconciliation";

describe("resolveTimezone", () => {
  it("resolves RKSI's real coordinates to Asia/Seoul", async () => {
    expect(await resolveTimezone(37.469, 126.451)).toBe("Asia/Seoul");
  });

  it("resolves KLGA's real coordinates to America/New_York", async () => {
    expect(await resolveTimezone(40.777, -73.873)).toBe("America/New_York");
  });

  it("resolves an arbitrary city not previously hardcoded anywhere (London)", async () => {
    expect(await resolveTimezone(51.4706, -0.4619)).toBe("Europe/London");
  });

  it("resolves an arbitrary city not previously hardcoded anywhere (Tokyo/Narita)", async () => {
    expect(await resolveTimezone(35.7647, 140.3864)).toBe("Asia/Tokyo");
  });
});
