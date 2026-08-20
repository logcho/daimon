---
name: daimon-design-system
description: Use before writing or reviewing any UI/frontend code in this repo — the Tauri app (src/), the landing site (website/), or any new surface. Covers Daimon's established colors, typography, spacing, motion, and component conventions so new work matches what's already built instead of drifting. Trigger on "design", "UI", "styling", "landing page", "component", "layout", "Tailwind", "hero", "css".
---

# Daimon design system

Daimon has one visual language shared by the desktop app and the landing site: dark, minimal, one sharp accent color. Don't invent a new palette or type scale per-surface — extend this one.

## Color

- Background: near-black obsidian, not pure `#000`. App uses `neutral-950`; website uses `#050505`.
- Accent: `#4f8dff` (blue). This is the **only** saturated color in the system — status states borrow from Tailwind's `emerald-400` (done/success) and `red-400` (error) only, never a second brand hue.
- Everything else is grayscale: `neutral-100` through `neutral-600`, plus glassmorphism surfaces built from `white` at low opacity (`border-white/10`, `bg-white/5`, `bg-white/[0.03]`) rather than solid grays — this is what gives the "premium glass" look instead of flat panels.
- Never introduce a second accent color (no orange, no purple/pink gradients) — one sharp accent against near-black is the whole point.

## Typography

- System font stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif`) — not a custom webfont. Keep it that way; distinctiveness here comes from scale and color, not typeface.
- Headlines: `font-semibold`, `tracking-tight`, and go big — the landing site's hero is `text-5xl` to `text-7xl` with `leading-[1.05]`. Don't undersize a hero headline; timid type reads as a template, not a product.
- Kicker/eyebrow labels above headings: `text-xs font-medium tracking-[0.25em] uppercase text-[#4f8dff]` (or `tracking-widest` for smaller in-page labels).
- Body copy: `text-neutral-400` on dark backgrounds, `leading-relaxed` for anything more than one line.

## Spacing & shape

- Section rhythm on the landing site: `py-28` vertically, `px-6` horizontally, content capped at `max-w-5xl` (prose-heavy sections use `max-w-2xl`/`max-w-xl`).
- Section separators are a **centered fading gradient line** (`bg-gradient-to-r from-transparent via-white/10 to-transparent`, see `website/src/components/Divider.astro`), never a flat `border-t` — a hard rule reads as a template default.
- Corner radii: `rounded-2xl` for cards, `rounded-[24px]`/`rounded-[28px]` for larger panels/shells. Nothing sharp-cornered in this system.
- The app's pill widget is `rounded-full` — preserve full-circle/pill shapes for anything "ambient status" flavored (badges, the widget itself).

## Motion

- Section entrances on the landing site use a shared `[data-reveal]` + `IntersectionObserver` pattern (see `website/src/layouts/Base.astro` + `global.css`) — fade + `translateY(18px)`, `0.7s cubic-bezier(0.16, 1, 0.3, 1)`, respects `prefers-reduced-motion`. Reuse this instead of a new animation approach per section.
- Cards get `hover:-translate-y-1` + a border/glow color shift, `transition duration-300` — a lift, not a scale.
- Interactive controls (buttons) get press feedback via `active:scale-90`/`active:scale-95`, not just a color change — this project treats that as a stand-in for haptics on a trackpad-driven UI.
- One well-placed animated element beats several — the pulsing status dot and the spinning step-icon in the app are the only continuously-animating elements; don't add more ambient motion than that per screen.

## Backgrounds / atmosphere

- Radial gradient glows behind hero-type content: `bg-[radial-gradient(ellipse_NN%_NN%_at_X%_Y%,rgba(79,141,255,0.15-0.2),transparent)]` as an absolutely-positioned `-z-10` layer.
- The landing site has an original ASCII/glyph texture (`website/src/components/AsciiField.astro`) — a build-time-generated field of faint monospace characters, masked to fade at top/bottom so it's visible across the full width rather than hidden behind centered content. Reuse this component rather than re-deriving the pattern; if extending it, keep the mask edge-based (not radial-centered) — a radial mask centered behind text hides the pattern exactly where content sits.

## A real bug worth knowing about: glow clipping

Any `box-shadow`/glow on an element that fills its entire container (a full-bleed card, the app's pill window) gets **hard-clipped square** at the container's edge instead of fading out — this bit both the app's pill widget and the landing site's chat panel earlier in this project. Fix: give the glow room by sizing the container/window larger than the visible content and padding the content inside it (see `src/components/Pill.tsx`'s `p-5` wrapper inside a window sized larger than the visible circle, or `website/src/components/HeroMockup.astro`). Don't just increase blur radius — that makes the clipping worse, not better.

## Icons

- Feature/step icons are small (`h-9 w-9`–`h-10 w-10`) rounded-square badges: `border-[#4f8dff]/20 bg-[#4f8dff]/10` container, `stroke="#4f8dff"` `stroke-width="1.5"` line-style SVG inside — never filled icons, always the same stroke weight.
