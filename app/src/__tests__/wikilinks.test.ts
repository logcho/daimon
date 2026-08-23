import { describe, expect, it } from "vitest";
import { findWikiLinks, resolveVaultPath, resolveWikiLink } from "../wikilinks";

describe("findWikiLinks", () => {
  it("reads targets, aliases and headings", () => {
    const [link] = findWikiLinks("see [[notes/kickoff|the kickoff]] today");
    expect(link.target).toBe("notes/kickoff");
    expect(link.label).toBe("the kickoff");
    expect(link.embed).toBe(false);
  });

  it("marks an embed and swallows its leading bang", () => {
    // The `!` used to fall outside the match and render as a stray character
    // next to the image.
    const text = "before ![[shot.png]] after";
    const [link] = findWikiLinks(text);
    expect(link.embed).toBe(true);
    expect(link.target).toBe("shot.png");
    expect(text.slice(link.start, link.end)).toBe("![[shot.png]]");
  });

  it("does not mistake a plain link for an embed", () => {
    expect(findWikiLinks("[[shot.png]]")[0].embed).toBe(false);
  });
});

describe("resolveWikiLink", () => {
  const names = ["notes/kickoff.md", "images/shot.png", "archive/kickoff.md"];

  it("matches an exact path, with or without .md", () => {
    expect(resolveWikiLink("notes/kickoff.md", names)).toBe("notes/kickoff.md");
    expect(resolveWikiLink("notes/kickoff", names)).toBe("notes/kickoff.md");
  });

  it("matches a unique basename regardless of extension", () => {
    expect(resolveWikiLink("shot.png", names)).toBe("images/shot.png");
    expect(resolveWikiLink("shot", names)).toBe("images/shot.png");
  });

  it("refuses an ambiguous basename rather than guessing", () => {
    // Two kickoff.md in different folders — picking one silently would send
    // the reader somewhere they did not ask for.
    expect(resolveWikiLink("kickoff", names)).toBeNull();
  });
});

describe("resolveVaultPath", () => {
  const names = ["trips/japan.md", "trips/shot.png", "images/shot.png", "top.md"];

  it("prefers the referring note's own folder", () => {
    // Written inside trips/japan.md, `shot.png` means the one next to it —
    // not the one in images/, which a vault-wide basename match would find.
    expect(resolveVaultPath("shot.png", names, "trips")).toBe("trips/shot.png");
  });

  it("walks up with ../", () => {
    expect(resolveVaultPath("../images/shot.png", names, "trips")).toBe("images/shot.png");
  });

  it("falls back to the vault-wide rules from the root", () => {
    expect(resolveVaultPath("top", names, "")).toBe("top.md");
  });

  it("is null when nothing matches", () => {
    expect(resolveVaultPath("nope.png", names, "trips")).toBeNull();
  });
});
