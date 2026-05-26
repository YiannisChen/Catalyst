import { describe, expect, it } from "vitest";

describe("test harness", () => {
  it("loads test environment", () => {
    expect(globalThis.fetch).toBeDefined();
  });
});
