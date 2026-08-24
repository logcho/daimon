import { describe, expect, it } from "vitest";
import { classifyLink } from "../links";

describe("classifyLink", () => {
  it("sends web pages to the in-app viewer", () => {
    expect(classifyLink("https://example.com/a?b=c")).toEqual({
      kind: "view",
      url: "https://example.com/a?b=c",
    });
    expect(classifyLink("HTTP://example.com").kind).toBe("view");
  });

  it("hands mail and phone links to the OS", () => {
    // Nothing to frame, so these never reach the viewer.
    expect(classifyLink("mailto:a@b.com").kind).toBe("external");
    expect(classifyLink("tel:+15551234").kind).toBe("external");
  });

  it("reads a scheme-less target as a path in the vault", () => {
    expect(classifyLink("other.md")).toEqual({ kind: "vault", target: "other.md" });
    expect(classifyLink("../trips/japan.md").kind).toBe("vault");
    // A colon inside a filename is not a scheme — the character before it has
    // to be part of one unbroken scheme, and `/` breaks it.
    expect(classifyLink("notes/a:b.md")).toEqual({ kind: "vault", target: "notes/a:b.md" });
  });

  it("ignores an in-page anchor", () => {
    // Moving within the page the reader is already on is not a navigation away,
    // so there is nothing to intercept.
    expect(classifyLink("#section").kind).toBe("ignore");
    expect(classifyLink("").kind).toBe("ignore");
  });

  it("refuses every scheme it was not told to allow", () => {
    // The deny-by-default case. Note text is written by the agent, from
    // whatever the agent read, so this is the one that matters.
    expect(classifyLink("javascript:alert(1)").kind).toBe("ignore");
    expect(classifyLink("file:///etc/passwd").kind).toBe("ignore");
    expect(classifyLink("data:text/html,<h1>hi").kind).toBe("ignore");
    expect(classifyLink("asset://localhost/x").kind).toBe("ignore");
  });

  it("gives a protocol-relative link a scheme that exists", () => {
    // `//host` inherits the page's scheme, which is `tauri:` here — a dead end.
    expect(classifyLink("//example.com/x")).toEqual({
      kind: "view",
      url: "https://example.com/x",
    });
  });
});
