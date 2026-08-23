import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { kindOf } from "../../fileKind";
import { parentFolder } from "../../noteTree";
import type { VaultFile } from "../../types";
import { assetUrl } from "../../vaultAsset";
import { findWikiLinks, resolveVaultPath, wikiLinkToNoteName } from "../../wikilinks";

export interface MarkdownViewProps {
  text: string;
  /** The open note — embeds and relative image paths resolve against its
   *  folder, the way they would in any other markdown tool. */
  noteName: string;
  files: VaultFile[];
  vaultDir: string;
  onFollow: (target: string) => void;
}

/** A vault file rendered where it was referenced.
 *
 *  Images, video, audio and PDFs all point at the same `asset://` URL the
 *  preview pane uses; anything else stays a link, because a `.zip` has no
 *  inline form and pretending otherwise would just be a broken box. */
function Embed({
  name,
  files,
  vaultDir,
  onFollow,
}: {
  name: string;
  files: VaultFile[];
  vaultDir: string;
  onFollow: (target: string) => void;
}) {
  const meta = files.find((f) => f.name === name);
  const kind = kindOf(name);
  if (!vaultDir || !meta || kind === "markdown" || kind === "text" || kind === "binary") {
    // Transclusion of one note into another is a separate feature; until then
    // an embedded note is a link to it, which at least goes somewhere.
    return (
      <button
        type="button"
        onClick={() => onFollow(name)}
        className="mx-0.5 rounded px-1 text-[#4f8dff] transition hover:bg-[#4f8dff]/10 hover:underline"
      >
        {name}
      </button>
    );
  }
  const src = assetUrl(vaultDir, name, meta.modifiedAt);
  const frame = "my-2 max-h-[60vh] max-w-full rounded-md border border-white/10";
  if (kind === "image") return <img src={src} alt={name} className={frame} />;
  if (kind === "video") return <video src={src} controls className={`${frame} w-full bg-black`} />;
  if (kind === "audio") return <audio src={src} controls className="my-2 w-full" />;
  return <iframe src={src} title={name} className={`${frame} h-[60vh] w-full`} />;
}

/** Split note text on `[[wikilinks]]` so the plain stretches can go through
 *  ReactMarkdown untouched and the links become buttons. Markdown doesn't know
 *  the syntax — left alone it renders as literal brackets — and rewriting them
 *  into `[]()` links first would break any that appear inside a code fence. */
export function MarkdownView({ text, noteName, files, vaultDir, onFollow }: MarkdownViewProps) {
  const names = files.map((f) => f.name);
  const folder = parentFolder(noteName);

  /** `![](shot.png)` — ordinary markdown images, whose `src` is a vault path
   *  the webview cannot fetch on its own. Resolved the same way an embed is,
   *  so both syntaxes agree about which file they mean. */
  const components = {
    img: ({ src, alt }: { src?: string | Blob; alt?: string }) => {
      const raw = typeof src === "string" ? src : "";
      // Remote and inline images are already loadable; leave them be.
      if (/^(https?:|data:|blob:|asset:)/i.test(raw)) {
        return <img src={raw} alt={alt ?? ""} className="my-2 max-h-[60vh] max-w-full rounded-md" />;
      }
      const resolved = resolveVaultPath(decodeURIComponent(raw), names, folder);
      const meta = resolved ? files.find((f) => f.name === resolved) : undefined;
      if (!resolved || !meta || !vaultDir) {
        return <span className="text-xs text-amber-400/80" title={raw}>missing image: {alt || raw}</span>;
      }
      return (
        <img
          src={assetUrl(vaultDir, resolved, meta.modifiedAt)}
          alt={alt ?? resolved}
          className="my-2 max-h-[60vh] max-w-full rounded-md border border-white/10"
        />
      );
    },
  };

  const links = findWikiLinks(text);
  if (links.length === 0) {
    return (
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    );
  }

  const parts: React.ReactNode[] = [];
  let cursor = 0;
  links.forEach((link, i) => {
    if (link.start > cursor) {
      parts.push(
        <ReactMarkdown key={`t${i}`} remarkPlugins={[remarkGfm]} components={components}>
          {text.slice(cursor, link.start)}
        </ReactMarkdown>,
      );
    }
    const resolved = resolveVaultPath(link.target, names, folder);
    if (link.embed && resolved) {
      parts.push(
        <Embed key={`e${i}`} name={resolved} files={files} vaultDir={vaultDir} onFollow={onFollow} />,
      );
    } else {
      parts.push(
        <button
          key={`l${i}`}
          type="button"
          onClick={() => onFollow(link.target)}
          title={resolved ? `open ${resolved}` : `create ${wikiLinkToNoteName(link.target)}`}
          className={`mx-0.5 rounded px-1 transition ${
            resolved
              ? "text-[#4f8dff] hover:bg-[#4f8dff]/10 hover:underline"
              : "text-amber-400/80 hover:bg-amber-400/10 hover:underline"
          }`}
        >
          {link.label}
          {!resolved && <span className="ml-0.5 text-[10px] opacity-70">+</span>}
        </button>,
      );
    }
    cursor = link.end;
  });
  if (cursor < text.length) {
    parts.push(
      <ReactMarkdown key="tail" remarkPlugins={[remarkGfm]} components={components}>
        {text.slice(cursor)}
      </ReactMarkdown>,
    );
  }
  return <>{parts}</>;
}
