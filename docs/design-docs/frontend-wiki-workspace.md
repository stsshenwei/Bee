# Frontend Wiki Workspace

## Layout

The knowledge-base detail header exposes Documents, Wiki, and Graph as peer views. Wiki and Graph share a bounded workspace with a 320px independently scrolling navigation rail and a flexible unframed reader. On viewports below 700px the rail and reader become stacked regions; detail and issue panels become full-screen drawers.

The navigation rail contains:

- bounded remote search
- pinned Index and Log system destinations
- Knowledge and Summary page groups with persisted counts
- tree/list segmented controls and root-folder creation
- folder filters and grouped page navigation
- one global issue entry

## Loading Boundaries

Initial Wiki navigation requests overview, one bounded page list, root folders, and task status. Page detail, logs, graph, issues, proposals, and raw source evidence are separate requests. Task polling runs every 2.5 seconds only while a generation or processing task is non-terminal.

## Reader And Graph

The article reader renders Markdown with internal `[[slug|label]]` links mapped to in-workspace navigation and safe external links opened in a new tab. Provenance, aliases, version, inbound/outbound links, and exact source chunks live in a secondary drawer so they do not crowd the article.

Graph mode uses the full reader region, supports page-type filters, renders a bounded scoped graph, and keeps the same navigation rail. Empty and loading states have stable dimensions.

## Visual Verification

Run `node scripts/wiki-visual-smoke.mjs` while backend port 8000 and frontend port 3000 are active. The script creates a temporary Wiki KB through HTTP APIs, captures desktop Index, desktop Graph, and 390px mobile screenshots, checks navigation/reader overlap and horizontal overflow, then archives the temporary KB.
