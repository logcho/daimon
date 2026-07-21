# Daimon — landing page

The public-facing landing page, built with [Astro](https://astro.build) + Tailwind CSS. Self-contained — its own `package.json`, independent of the desktop app in the rest of this repo.

## Development

```sh
npm install
npm run dev      # localhost:4321
```

## Build

```sh
npm run build     # outputs static files to dist/
npm run preview   # serve the production build locally
```

## Typecheck

```sh
npx astro check
```

## Deployment

Fully static output (`dist/`) — deployable anywhere. Vercel, Netlify, and Cloudflare Pages all auto-detect Astro projects with zero config if you point them at this `website/` directory as the project root. No environment variables or backend are required for the current page.

## Structure

```text
website/
├── src/
│   ├── layouts/Base.astro    # <head>, meta tags, shared page shell
│   ├── components/           # page sections (Hero, HowItWorks, Features, ...)
│   ├── pages/index.astro     # assembles the sections
│   └── assets/logo.png       # optimized via astro:assets (Image component)
└── public/                   # served as-is: favicon.ico, logo.svg
```

Brand assets (`logo.svg`, `logo.png`) are copied from the root `assets/`/`public/` — see the root README if the brand mark changes and these need re-syncing.
