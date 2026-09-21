# Issue tracker: Local Markdown

Issues and PRDs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The PRD is `.scratch/<feature-slug>/PRD.md`
- Implementation issues are `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`
- Triage state is recorded as a `Status:` line near the top of each issue file (see `triage-labels.md` for the role strings)
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one **child** file per ticket.

- **Map**: `.scratch/<effort>/map.md` — the Notes / Decisions-so-far / Fog body.
- **Child ticket**: `.scratch/<effort>/tickets/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `open`/`closed`; an `Assignee:` line records the claim (empty = unclaimed).
- **Blocking**: a `Blocked-by:` line near the top listing blocker filenames (`NN-<slug>.md`, comma-separated). A ticket is unblocked when every file it lists is `closed`.
- **Frontier**: scan `.scratch/<effort>/tickets/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Assignee:` and save before any work.
- **Resolve**: append the answer under an `## Resolution` heading, set `Status: closed`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
- **Validate**: after any structure-changing transaction, run the wayfinder validator (`scripts/validate_local_map.py` in the wayfinder skill) against the map directory; fix every reported error before stopping.
