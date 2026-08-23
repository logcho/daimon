import type { FileKind } from "../../fileKind";

/** One glyph per file kind, in the same hand-rolled style as the tree's
 *  chevron — 24px box, stroked, no fill, currentColor. The app carries no icon
 *  library and this is not the feature to add one for. */
export function FileIcon({ kind, className = "h-3.5 w-3.5" }: { kind: FileKind; className?: string }) {
  const common = {
    viewBox: "0 0 24 24",
    className: `${className} shrink-0`,
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };

  switch (kind) {
    case "image":
      return (
        <svg {...common}>
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <circle cx="8.5" cy="9.5" r="1.5" />
          <path d="M21 16l-5-5-6 6-3-3-4 4" />
        </svg>
      );
    case "pdf":
      return (
        <svg {...common}>
          <path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" />
          <path d="M14 3v5h5" />
          <path d="M9 15h1.5a1.25 1.25 0 000-2.5H9V18" />
        </svg>
      );
    case "audio":
      return (
        <svg {...common}>
          <path d="M9 18V5l10-2v13" />
          <circle cx="6.5" cy="18" r="2.5" />
          <circle cx="16.5" cy="16" r="2.5" />
        </svg>
      );
    case "video":
      return (
        <svg {...common}>
          <rect x="3" y="5" width="18" height="14" rx="2" />
          <path d="M10 9.5l5 2.5-5 2.5z" />
        </svg>
      );
    case "text":
      return (
        <svg {...common}>
          <path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" />
          <path d="M14 3v5h5" />
          <path d="M9 13h6M9 16.5h4" />
        </svg>
      );
    case "markdown":
      return (
        <svg {...common}>
          <path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" />
          <path d="M14 3v5h5" />
          <path d="M8 17v-4l2 2 2-2v4" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" />
          <path d="M14 3v5h5" />
        </svg>
      );
  }
}
