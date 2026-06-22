---
name: interactive-docs
description: Build polished interactive HTML companion documentation from canonical Markdown docs. Use when Codex is asked to convert process-heavy docs into walkthroughs, simulators, request builders, setup planners, configuration explorers, or browser-based onboarding pages while preserving Markdown as the source reference.
license: LicenseRef-NvidiaProprietary
---

# Interactive Documentation Builder

Use this skill to turn dense repository docs into useful browser-based companion pages.
The goal is not decoration. The goal is to help a developer understand, configure,
and safely operate the system faster than they could from static prose alone.

## Core Rules

- Keep canonical Markdown docs intact. Add HTML companions and cross-link them.
- Use plain HTML, CSS, and small local JavaScript unless the repo already has a docs build system.
- Share visual language across pages with a common CSS file when creating more than one page.
- Prefer interaction that teaches a decision or process: stage walkthroughs, payload builders, routing simulators, setup planners, verification pipelines, and checklists.
- Keep pages self-contained and runnable from disk when possible.
- Do not introduce external CDNs, network dependencies, build steps, or heavyweight frameworks for static docs.
- Validate every generated HTML page and local link set before finishing.

## Workflow

1. Inventory candidate docs.
   - Use `rg --files docs` and heading outlines such as `rg -n '^#{1,3} ' docs/*.md`.
   - Classify docs as interactive candidates or reference-only docs.
   - Prioritize docs with workflows, branching decisions, configuration choices, request/response shapes, or operational checklists.

2. Choose companion pages.
   - Convert high-value tutorial/process docs into HTML companions.
   - Leave schema, API, and exhaustive reference docs in Markdown unless an explorer genuinely helps.
   - Name companions predictably, for example `getting-started.html`, `deployment.html`, `mcp-integration.html`.

3. Design the interaction around the user task.
   - Setup docs: build a stepper or deployment planner with generated commands and expected signals.
   - Routing/config docs: build selectors that show resolved state and config snippets.
   - Protocol docs: build request builders that emit copy-ready JSON or commands.
   - Security/executor docs: build stage walkthroughs with pass/fail scenarios.
   - Account/permission docs: build explorers that compute scopes, membership, and denial reasons.

4. Build with repo-local style.
   - Create or reuse `docs/assets/interactive-docs.css`.
   - Keep cards at 8px radius or less.
   - Use constrained layouts, clear tables, readable code panels, and responsive grids.
   - Avoid in-app text that merely explains visual styling; text should teach the system.
   - Keep colors restrained and varied enough that pages do not become a one-hue theme.

5. Preserve levelization and maintainability.
   - Do not import application internals into static docs.
   - Encode examples as illustrative client-side data, not live calls.
   - Link to canonical Markdown for complete reference details.
   - Keep page-specific JavaScript near the bottom of the page and scoped to that page.

6. Wire navigation.
   - Add companion links to `docs/index.md`.
   - Add a top link from each canonical Markdown doc to its interactive companion.
   - Add README links for the most useful onboarding pages.
   - Verify every local link resolves.

7. Validate.
   - Parse HTML with Python:

     ```bash
     python3 - <<'PY'
     from html.parser import HTMLParser
     from pathlib import Path

     class Parser(HTMLParser):
         pass

     for path in sorted(Path("docs").glob("*.html")):
         Parser().feed(path.read_text())
         print(f"parsed {path}")
     PY
     ```

   - Check whitespace:

     ```bash
     git diff --check docs README.md
     ```

   - Check local links:

     ```bash
     python3 - <<'PY'
     from pathlib import Path
     import re

     files = list(Path("docs").glob("*.md")) + list(Path("docs").glob("*.html")) + [Path("README.md")]
     missing = []
     for path in files:
         text = path.read_text()
         targets = re.findall(r'\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)', text)
         targets += re.findall(r'href="([^"]+)"', text)
         for target in targets:
             if target.startswith(("http:", "https:", "#", "mailto:")):
                 continue
             if not (path.parent / target).exists():
                 missing.append((str(path), target))
     if missing:
         for item in missing:
             print(item)
         raise SystemExit(1)
     print("local doc links exist")
     PY
     ```

## Quality Bar

An interactive docs page is done when:

- A new developer can infer the system behavior by manipulating the page.
- The page exposes the relevant config surface or request shape directly.
- It shows expected results, not only commands.
- It handles at least one denial, failure, or alternate-path scenario when those are central to the topic.
- It links back to the canonical Markdown source.
- It works on narrow and wide screens without text overlap.

## Nomos Page Patterns

Use these established companion patterns in this repo:

- `workflow.html`: animated end-to-end request migration through framework stages.
- `getting-started.html`: setup wizard with generated commands and expected health signals.
- `multi-council.html`: route simulator for v1/v2, `council_id`, membership, and OPA profile.
- `writ-executor.html`: executor verification stepper with failure-stage scenarios.
- `authentication.html`: token, account, membership, and permission-scope explorer.
- `mcp-integration.html`: JSON-RPC request builder for council tools.
- `deployment.html`: topology and command planner for OPA, executor profiles, demo, and exposure.

When adding a new page, make it feel like it belongs beside these rather than inventing a new documentation product each time.
