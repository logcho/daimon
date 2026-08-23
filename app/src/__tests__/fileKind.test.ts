import { describe, expect, it } from "vitest";
import { extensionOf, isEditable, isMedia, kindOf } from "../fileKind";

describe("extensionOf", () => {
  it("reads the extension, lowercased", () => {
    expect(extensionOf("Shot.PNG")).toBe("png");
    expect(extensionOf("notes/deep work.md")).toBe("md");
  });

  it("treats a dotfile as having no extension", () => {
    // `.gitignore` is a name that starts with a dot, not a file of type
    // "gitignore" — reading it as an extension would file every dotfile under
    // a type of its own.
    expect(extensionOf(".gitignore")).toBe("");
    expect(extensionOf("sub/.env")).toBe("");
  });

  it("has no extension when there is no dot", () => {
    expect(extensionOf("Makefile")).toBe("");
    expect(extensionOf("notes/README")).toBe("");
  });

  it("takes only the last segment, so a dotted folder is not an extension", () => {
    expect(extensionOf("my.notes/kickoff")).toBe("");
  });
});

describe("kindOf", () => {
  it("classifies each family", () => {
    expect(kindOf("a.md")).toBe("markdown");
    expect(kindOf("a.png")).toBe("image");
    expect(kindOf("a.pdf")).toBe("pdf");
    expect(kindOf("a.mp3")).toBe("audio");
    expect(kindOf("a.mov")).toBe("video");
    expect(kindOf("a.py")).toBe("text");
  });

  it("falls back to binary for anything unrecognised", () => {
    expect(kindOf("a.sketch")).toBe("binary");
    expect(kindOf("Makefile")).toBe("binary");
  });

  it("reads an SVG as text, since that is what it is and how it edits", () => {
    expect(kindOf("logo.svg")).toBe("text");
  });
});

describe("isEditable / isMedia", () => {
  it("opens the editor for markdown and text only", () => {
    expect(isEditable(kindOf("a.md"))).toBe(true);
    expect(isEditable(kindOf("a.json"))).toBe(true);
    expect(isEditable(kindOf("a.png"))).toBe(false);
    expect(isEditable(kindOf("a.sketch"))).toBe(false);
  });

  it("streams media, and only media", () => {
    expect(isMedia(kindOf("a.mp4"))).toBe(true);
    expect(isMedia(kindOf("a.pdf"))).toBe(true);
    expect(isMedia(kindOf("a.md"))).toBe(false);
  });
});
