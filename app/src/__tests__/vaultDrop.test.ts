import { describe, expect, it } from "vitest";
import { uniqueName } from "../vaultDrop";

describe("uniqueName", () => {
  it("leaves a free name alone", () => {
    expect(uniqueName("shot.png", new Set())).toBe("shot.png");
  });

  it("numbers a collision before the extension, Finder-style", () => {
    expect(uniqueName("shot.png", new Set(["shot.png"]))).toBe("shot 1.png");
  });

  it("keeps counting past an existing numbered copy", () => {
    expect(uniqueName("shot.png", new Set(["shot.png", "shot 1.png"]))).toBe("shot 2.png");
  });

  it("keeps the folder and only renames the file", () => {
    expect(uniqueName("trips/shot.png", new Set(["trips/shot.png"]))).toBe("trips/shot 1.png");
  });

  it("handles a name with no extension", () => {
    expect(uniqueName("Makefile", new Set(["Makefile"]))).toBe("Makefile 1");
  });

  it("does not treat a dotfile's dot as an extension", () => {
    expect(uniqueName(".env", new Set([".env"]))).toBe(".env 1");
  });
});
