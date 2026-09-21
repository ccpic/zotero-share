---
name: "jadense-scholar-search"
description: "Search scholarly literature across arXiv, OpenAlex, Crossref, PubMed, Semantic Scholar, and optional Google Scholar through a provider-neutral search core. Invoke when an agent needs paper discovery, comparison, date-bounded retrieval, or a traceable literature shortlist."
license: MIT
---

# Jadense Scholar Search

Provider-neutral scholarly literature search. The core normalizes results from arXiv, OpenAlex, Crossref, PubMed, Semantic Scholar, and optional SerpApi-backed Google Scholar into one transient response with query planning, provider diagnostics, stable identities, and source metadata.

## Layout (self-contained)

This skill is self-contained. The underlying implementation and scripts live in this directory alongside this SKILL.md:

- `src/` — TypeScript source (core, providers, CLI entry).
- `dist/` — Built output run directly by Node (`dist/bin/scholar-search.mjs`).
- `node_modules/` — Installed runtime dependencies (`@xmldom/xmldom`).
- `tests/` — Test suites (`tsx --test tests/*.test.ts`).
- `package.json` — Scripts and CLI metadata (`bin.jadense-scholar-search`).
- `README.md` / `SECURITY.md` / `LICENSE` — CLI, provider, and security details.
- `references/html-result-guide.md` — HTML result delivery style guide, self-contained template, and usage steps.
- `references/translation-guide.md` — intelligent translation and reading-guide rules (bilingual title, tabbed abstract, recommendation rationale).
- `scripts/fetch_pubmed_abstract.py` — stdlib fallback that fetches the PubMed record's own abstract for records still missing one after enrichment.
- `integrations/jadense/README.md` — Downstream integration notes.

## Running the CLI

`dist/` and `node_modules/` are both gitignored, so a fresh checkout has neither — run `npm ci && npm run build` first. Then run from this directory:

```sh
npm run build          # rebuild dist from src (tsc)
npm test               # run the test suite
node dist/bin/scholar-search.mjs --offline --query "transformer interpretability"
node dist/bin/scholar-search.mjs --query "retrieval augmented generation" --provider openalex --provider crossref --from 2020-01-01 --to 2025-12-31 --format markdown
node dist/bin/scholar-search.mjs --query "coconut water composition" --provider pubmed --enrich --format json
```

CLI flags: `-q/--query` (repeat up to four), `-p/--provider` (repeat or comma-separate), `--limit 1..30`, `--from`/`--to` (YYYY-MM-DD), `--format json|markdown`, `--offline`, `--enrich` (complete missing abstracts via OpenAlex after ranking), `--cache-dir`, `--no-cache`, `--refresh`, `--user-agent`, `--contact-email`.

## Response cache

Provider responses and OpenAlex enrichment merges are cached in one cell per key — `provider + normalized query + limit + date range + offline flag` for search, `DOI ?? external id ?? normalized title + provider set` for enrichment — with a 168-hour window anchored at the cell's own collection moment (workspace-guide §14.2). Only successful cells are cached: provider errors are never stored, so failure diagnostics are re-recorded on every run. A cache hit never extends the window, and `--refresh` bypasses existing cells explicitly.

- Cache directory: `--cache-dir <path>`, else `SCHOLAR_SEARCH_CACHE_DIR`, else `<package>/.cache` (gitignored). Point it at a gitignored, non-delivery path — the default qualifies; a delivery directory (`workspace/<delivery>/…`) or any tracked path does not (cleanup rules: `workspace-guide.md` §15). `--no-cache` disables reading and writing.
- `--offline` never reads or writes the cache (fixture readings must not land in real cells).
- Provenance: each `queryPlan.queryStatuses[]` cell carries `cacheHit` and `collectedAt` (the data's own collection date, not the rerun date); `queryPlan.enrichCacheHits[]` does the same per hit. Record the data date in the search-strategy and screening logs so a cached reading is never reported as fetched today.
- Payload: a search cell stores `{ candidates, totalAvailable }` — the candidates before date filtering plus the one reading that cannot be recomputed from them. `CACHE_VERSION` is unchanged; the stricter payload guard makes an old-shape cell (a bare candidate array) fall through the existing "shape mismatch = miss" path, so it is refetched and rewritten in the current shape — there is no compatibility branch. On a hit the cell reports `attempts: 0`, `retried: false`, `recoveredByRetry: false`, and `hitCount`/date counts/`capHit`/`capPolicy` are recomputed from the payload, so a cached cell keeps its truncation disclosure.

## Retrieval hardening

Provider requests pass through one shared gate, so a transient outage does not silently shrink a cell and a truncated cell is never silently short.

- **Throttling and concurrency caps** — PubMed is serial at ≤3 requests/second (three concurrent calls reliably produce 429s), arXiv follows its Terms of Use at ≤1 request / 3 s on a single connection, OpenAlex and Crossref run at 10 rps, Semantic Scholar and SerpApi at 1 rps. Retries and cap compensation queue through the same gate.
- **Bounded same-cell retry** — a failing cell is re-run once, in place, and only for transient classes: `unavailable` (transport failure or 5xx) and `quota_exhausted` (429). `invalid_request` (400) and `auth_failed` (401/403) are never retried. The retry waits 800–1600 ms (exponential base 800 ms with jitter), stays inside the cell, never restarts the pool, and never enters the cache key.
- **Total-available diagnostics** — every cell reports `totalAvailable`, `null` when the source reports no total (never 0 and never the returned count). `capHit` is `true` when `totalAvailable > hitCount` and `null` when the total is unknown; `null` never reads as `false`.
- **Cap handling by source type** — exact-set sources (PubMed, arXiv: their count is the searchable set) raise the cell limit to `min(totalAvailable, 30)` and, for a dated cell still short, slice the window by year (oldest first, ≤6 slices of ≤30); the residual gap is recorded rather than hidden. Recall-form sources (OpenAlex, Crossref, Semantic Scholar, Google Scholar) record the total only and never buy extra requests. `capPolicy` names the branch: `none` / `unknown` / `recorded_only` / `raised_limit` / `year_sliced`.
- **Date windows** — with a date range, a candidate without a valid `YYYY-MM-DD` date is **kept** and marked `sourceMetadata.windowStatus = "date_unknown"` for downstream screening, while one with a valid date outside the window is dropped and counted (`dateRejectedCount`). Without a date range every pooled candidate is marked `unfiltered`. A merged cluster takes the strongest verdict: any dated in-window member removes the `date_unknown` mark, and the mark survives only when no member has a usable date.

## Workflow

1. Decide whether the request is an exact lookup or a broad search.
2. Select one or two providers when the user gives a clear source preference. Use arXiv for preprints and recent work, OpenAlex for broad formal discovery and citations, PubMed for biomedical work, Crossref for DOI or publisher metadata, and Semantic Scholar for paper IDs or citation context.
3. Select Google Scholar only when web-visible coverage or Google Scholar citation discovery is explicitly needed. A SerpApi key is required; it is never enabled by default.
4. Create one to four focused queries and one search request. Use an explicit date range only when the user asks for a publication window.
5. Treat results as evidence for the final answer. Preserve DOI or provider external identity when rendering or saving a paper.
6. Do not infer methods, findings, or dates that the provider did not return. Unknown dates remain unknown: an undated candidate stays in the pool with `windowStatus: "date_unknown"` for screening to judge, and only a valid date outside the window is a drop.

## HTML Result Delivery

Deliver search results as a self-contained HTML page when the user asks for a visual, shareable, printable, or report-style deliverable, or when presenting multiple papers for comparison. The style and template live in `references/html-result-guide.md`.

- **When**: Use HTML for "网页 / 可视化 / 精美交付 / 报告页 / 保存成文件" requests and for multi-paper comparison pages. Keep markdown or plain text for simple Q&A or when the user needs the verbatim text pasted into chat.
- **How**: Read `references/html-result-guide.md`, copy its embedded self-contained template, then map `SearchResponse.queryPlan` (summary band), `results[]` (one card per paper), and `diagnostics[]` (severity-coded footer section) into the placeholders. Save as a single `.html` file and return its path.
- **Constraints**: The page must be zero-dependency (no CDN, external fonts, or external JS), use CSS variables for all colors, meet WCAG AA contrast, and be responsive (single column on mobile). Preserve each DOI / URL / external id. Do not fabricate missing fields (unknown dates stay unknown); Google Scholar records without a DOI must not get a fake stable identity.

## Enrichment, Translation & Reading Guide

After selecting the valuable papers, complete their missing abstracts and then add translation and a reading guide.

1. **Enrich**: run `--enrich` (or library call `enrich: true`) to complete missing abstracts via OpenAlex after ranking. Enrichment fills `abstract` and backfills missing venue/URL/date/citation metadata; failures are isolated as diagnostics and never discard results. For selected records that still lack an abstract after enrichment (some publishers deposit none in either source), recover it from the PubMed record — `scripts/fetch_pubmed_abstract.py <PMID> …` prints the cleaned abstract (PMID from the record's external id); if no source provides one, mark the gap explicitly in the deliverable instead of leaving truncated or non-abstract text in place.
2. **Translate**: follow `references/translation-guide.md` to intelligently translate each paper's title + abstract into the user's language. Deliverables keep **bilingual content**: titles show Chinese and English simultaneously; abstracts toggle between languages via tabs (default Chinese).
3. **Reading guide**: follow `references/translation-guide.md` to write a short recommendation rationale and reading points per paper, plus an optional overall guide when presenting multiple papers.
4. **Deliver**: when rendering an HTML page, merge the enriched, translated, and guided content into the `references/html-result-guide.md` template.

## Credentials

Only Google Scholar requires a key: set `SERPAPI_API_KEY` when selecting `google_scholar`. Optional credentials are `SEMANTIC_SCHOLAR_API_KEY`, `NCBI_API_KEY`, and `NCBI_TOOL`. The CLI maps these environment variables into the injected runtime; the core itself does not read the environment.

## Host Boundaries

The package does not know about users, permissions, billing, databases, favorites, browser extensions, or CNKI. A host may inject a `PersistenceAdapter` for an explicit save operation, but the core response remains transient and contains no persistence or billing fields.

Google Scholar records without a DOI have temporary result or cluster metadata only. Do not invent a stable identity for them or pass them to identity-based persistence.

Provider failures are isolated per query/provider; successful providers still return results with a warning diagnostic. Transient failures (transport errors, 5xx, 429) are retried once inside the cell after a jittered backoff, and the run stays inside each source's rate budget; inject `NCBI_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY` to raise those budgets further. A cell that still fails reports `status: "error"` with `attempts` / `retried` / `recoveredByRetry`, so its gap stays visible instead of reading as an empty cell.

See `README.md` for full CLI and provider configuration details.