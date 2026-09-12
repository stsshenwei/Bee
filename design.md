# Design - Bee

A design system for the Bee knowledge workspace, revised on 2026-09-09. Reference: Anthropic frontend-design and UI UX Pro Max, with Hallmark's in-place redesign constraints. Use this file before adding page-level styling.

## Genre
modern-minimal

## Macrostructure family
- App pages: compact Knowledge Workspace. A quiet navigation rail, one title/action header, inline metadata, tabs, then the actual content.
- Content pages: navigation and reading columns inside the same shell. Graph mode owns the entire content width.

## Theme
- Paper: `--color-paper` #ffffff
- Navigation: `--color-paper-soft` #f7f8f9
- Text: `--color-ink` #30363d
- Secondary text: `--color-muted` #626d78
- Border: `--color-rule` #e4e8eb
- Brand and focus: `--color-accent` / `--color-focus` #18765c
- File icons: `--color-file` #406caa

## Typography
- Display and body: native system sans-serif with PingFang SC and Microsoft YaHei fallbacks. No network font dependency.
- Body weights: 400 to 600. Monospace is reserved for code.
- Display tracking: 0.
- Page titles: 25px desktop, 23px mobile. Body: 12 to 14px. Fixed type sizes; no viewport font scaling.

## Spacing
4-point spacing scale. Desktop sidebar 224px, page padding 32px by 36px. Mobile padding 20px by 16px. Controls are 34 to 38px tall. Section heights follow content, never distributed grid space.

## Motion
- Easings: `--ease-out`, `--ease-in`, `--ease-in-out`.
- Reveal pattern: none by default for app pages.
- Interaction pattern: border/color change and instant focus ring. Cards remain stationary.
- Reduced-motion fallback: no spatial animation beyond 50 ms state changes.

## Microinteractions Stance
- Silent success.
- No celebratory motion.
- Hover is subtle; focus is explicit.
- Destructive actions use warning color only on intent, not as permanent chrome.

## CTA Voice
- Primary CTA: forest green, 6px radius, concrete verb labels.
- Secondary CTA: white surface, 1px border, forest on hover. Tools use labelled Lucide icons.

## Per-page Allowances
- App pages should not use decorative enrichment. The product surface is the visual asset.
- Chat uses a single column with header, scrollable thread, and a composer in normal layout flow.
- Knowledge graph nodes use semantic colors. Its toolbar, filters, and canvas must not overlap.
- Knowledge catalog cards use compact icon/title rows without a diagonal arrow. Document tiles group status and selection in the footer.
- Document detail uses a white reading surface on `--color-reader-canvas`, with supporting information to the right and normal page scrolling for text. Wiki articles use the full reader width, overriding the general prose measure for this surface.

## What Pages Must Share
- Bee wordmark treatment.
- Small forest accents on primary actions and selection.
- System sans-serif for titles, text, counts, and metadata.
- 1px borders and 4 to 8px radii.
- No nested decorative card stacks.

## What Pages May Differ On
- Density of document rows.
- Whether the active area is chat, catalog, wiki, trace, or document reading.
- Semantic status color for success, warning, and danger states.

## Exports

### tokens.css
See `frontend/tokens.css`.

### Tailwind v4 `@theme`
```css
@theme {
  --color-paper: #ffffff;
  --color-ink: #30363d;
  --color-accent: #18765c;
  --font-display: var(--font-body);
  --font-body: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif;
  --spacing-md: 16px;
  --text-md: 14px;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
}
```

### DTCG tokens.json
```json
{
  "color": {
    "paper": { "$value": "#ffffff", "$type": "color" },
    "ink": { "$value": "#30363d", "$type": "color" },
    "accent": { "$value": "#18765c", "$type": "color" }
  },
  "font": {
    "display": { "$value": "system-ui", "$type": "fontFamily" },
    "body": { "$value": "system-ui", "$type": "fontFamily" }
  },
  "space": {
    "md": { "$value": "16px", "$type": "dimension" }
  }
}
```

### shadcn/ui CSS variables
```css
:root {
  --background: #ffffff;
  --foreground: #30363d;
  --primary: #18765c;
  --primary-foreground: #ffffff;
  --muted: #f7f8f9;
  --muted-foreground: #626d78;
  --border: #e4e8eb;
  --input: #e4e8eb;
  --ring: #18765c;
  --radius: 6px;
}
```
