/** Obsidian-style `[[wikilink]]` resolution.
 *
 *  Obsidian links notes by *name*, not by path: `[[meeting]]` finds
 *  `notes/meeting.md` wherever it lives, and the `.md` is implied. That's the
 *  whole reason the syntax is pleasant to type, so resolution has to do the
 *  same fuzzy matching rather than treating the target as a literal path.
 *
 *  Kept out of the component so the matching rules can be tested and reasoned
 *  about on their own — they're the part with real edge cases (aliases,
 *  headings, case, nested folders), not the rendering.
 */

/** A `[[target|alias]]` occurrence found in note text. */
export interface WikiLink {
  /** The link target as written, minus any alias or `#heading`. */
  target: string;
  /** What to display — the alias when given, else the target. */
  label: string;
  /** Character offsets of the whole `[[...]]` token in the source. */
  start: number;
  end: number;
}

// `[[target]]`, `[[target|alias]]`, `[[target#heading]]`. Non-greedy and
// newline-free so an unclosed `[[` can't swallow the rest of the note.
const WIKILINK_RE = /\[\[([^\]\n|#]+)(?:#([^\]\n|]+))?(?:\|([^\]\n]+))?\]\]/g;

export function findWikiLinks(text: string): WikiLink[] {
  const links: WikiLink[] = [];
  for (const match of text.matchAll(WIKILINK_RE)) {
    const target = match[1].trim();
    if (!target) continue;
    const alias = match[3]?.trim();
    const heading = match[2]?.trim();
    links.push({
      target,
      label: alias || (heading ? `${target} › ${heading}` : target),
      start: match.index,
      end: match.index + match[0].length,
    });
  }
  return links;
}

/** Strip directories and the `.md` suffix — `notes/Deep Work.md` → `deep work`.
 *  Lowercased because Obsidian's link matching is case-insensitive. */
function basenameKey(name: string): string {
  const base = name.slice(name.lastIndexOf("/") + 1);
  return base.replace(/\.md$/i, "").toLowerCase();
}

/**
 * Resolve a link target to an actual note name, or `null` when nothing matches
 * (Obsidian shows those as "unresolved" and creates the note on click).
 *
 * Tries, in order: exact path, path with `.md` appended, then a unique
 * basename match. Basename matching is last and only accepted when it is
 * unambiguous — with two `meeting.md` in different folders, silently picking
 * one would send the user somewhere they didn't ask for.
 */
export function resolveWikiLink(target: string, noteNames: string[]): string | null {
  const wanted = target.trim();
  if (!wanted) return null;

  const exact = noteNames.find((n) => n === wanted || n === `${wanted}.md`);
  if (exact) return exact;

  const key = basenameKey(wanted);
  const byBasename = noteNames.filter((n) => basenameKey(n) === key);
  return byBasename.length === 1 ? byBasename[0] : null;
}

/** The note name to create for an unresolved link. Bare targets land at the
 *  vault root; a target that already names a folder keeps it. */
export function wikiLinkToNoteName(target: string): string {
  const trimmed = target.trim();
  return /\.md$/i.test(trimmed) ? trimmed : `${trimmed}.md`;
}

/** Notes whose text links to `noteName` — the backlinks panel.
 *  `contents` maps note name → its text. */
export function findBacklinks(
  noteName: string,
  contents: Map<string, string>,
  noteNames: string[],
): string[] {
  const backlinks: string[] = [];
  for (const [name, text] of contents) {
    if (name === noteName) continue; // a note linking to itself isn't a backlink
    const hit = findWikiLinks(text).some(
      (link) => resolveWikiLink(link.target, noteNames) === noteName,
    );
    if (hit) backlinks.push(name);
  }
  return backlinks.sort();
}
