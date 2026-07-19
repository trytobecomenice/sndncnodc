import { describe, expect, it } from "vitest";
import { computeHitRate } from "./calculateProbability";

describe("computeHitRate", () => {
  it("computes a bounded range hit rate (e.g. an 80-81F Polymarket bucket)", () => {
    const values = [78, 79, 80, 80.5, 81, 82, 83];
    const result = computeHitRate(values, { min: 80, max: 81 });
    expect(result.hitCount).toBe(3); // 80, 80.5, 81
    expect(result.totalCount).toBe(7);
    expect(result.probability).toBeCloseTo(3 / 7);
  });

  it("computes an open-ended upper bucket (e.g. '31C or higher')", () => {
    const values = [28, 29, 30, 31, 32];
    const result = computeHitRate(values, { min: 31, max: null });
    expect(result.hitCount).toBe(2); // 31, 32
    expect(result.probability).toBeCloseTo(0.4);
  });

  it("computes an open-ended lower bucket (e.g. '21C or below')", () => {
    const values = [19, 20, 21, 22, 23];
    const result = computeHitRate(values, { min: null, max: 21 });
    expect(result.hitCount).toBe(3); // 19, 20, 21
    expect(result.probability).toBeCloseTo(0.6);
  });

  it("returns 0 probability, 0 totalCount for an empty ensemble (not silently 0% confident)", () => {
    const result = computeHitRate([], { min: 80, max: null });
    expect(result.totalCount).toBe(0);
    expect(result.probability).toBe(0);
  });

  it("is inclusive at both bounds", () => {
    const result = computeHitRate([80, 81], { min: 80, max: 81 });
    expect(result.hitCount).toBe(2);
  });

  it("reproduces this session's real RKSI finding: ecmwf ~22%, gfs ~3% for >=80F", () => {
    // 11 of 51 ecmwf members, 1 of 31 gfs members, matching the live values observed.
    const ecmwfHits = Array(11).fill(82);
    const ecmwfMisses = Array(40).fill(70);
    const ecmwf = computeHitRate([...ecmwfHits, ...ecmwfMisses], { min: 80, max: null });
    expect(ecmwf.probability).toBeCloseTo(11 / 51, 2);

    const gfsHits = Array(1).fill(82);
    const gfsMisses = Array(30).fill(70);
    const gfs = computeHitRate([...gfsHits, ...gfsMisses], { min: 80, max: null });
    expect(gfs.probability).toBeCloseTo(1 / 31, 2);
  });
});
