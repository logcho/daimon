import { describe, expect, it } from "vitest";
import { candidatePaths } from "../mobile/vaultImage";

describe("candidatePaths", () => {
  it("tries the note's own folder before the vault root", () => {
    // `![](shot.png)` inside trips/japan.md means the one next to it first.
    expect(candidatePaths("shot.png", "trips")).toEqual(["trips/shot.png", "shot.png"]);
  });

  it("is just the root for a note at the top level", () => {
    // The real case: `![cat](cat.jpeg)` in Test.md at the vault root.
    expect(candidatePaths("cat.jpeg", "")).toEqual(["cat.jpeg"]);
  });

  it("walks up with ../", () => {
    expect(candidatePaths("../cat.jpeg", "notes")).toEqual(["cat.jpeg", "../cat.jpeg"]);
  });

  it("normalises a leading ./", () => {
    expect(candidatePaths("./shot.png", "trips")).toEqual(["trips/shot.png", "shot.png"]);
  });

  it("decodes percent-escapes, so a spaced filename resolves", () => {
    expect(candidatePaths("my%20shot.png", "")).toEqual(["my shot.png"]);
  });

  it("does not offer the same path twice", () => {
    expect(candidatePaths("cat.jpeg", "")).toHaveLength(1);
  });
});
