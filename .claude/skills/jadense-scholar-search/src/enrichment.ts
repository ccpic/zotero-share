import { enrichCellKey, isPaperCandidate, type CacheStore } from "./cache.js"
import { safeErrorMessage } from "./errors.js"
import { getProvider } from "./providers/index.js"
import type { EnrichCacheHit, EnrichmentConfig, PaperCandidate, ProviderAdapter, SearchDiagnostic, SearchProviderId, SearchRuntimeConfig } from "./types.js"

export const DEFAULT_ENRICH_PROVIDERS: SearchProviderId[] = ["openalex"]

export function normalizeEnrichConfig(enrich: boolean | EnrichmentConfig | undefined, resultCount: number): EnrichmentConfig | null {
  if (enrich === undefined || enrich === false) return null
  if (enrich === true) return { abstract: true, topN: resultCount, providers: DEFAULT_ENRICH_PROVIDERS }
  const providers = enrich.providers && enrich.providers.length ? [...new Set(enrich.providers)] : DEFAULT_ENRICH_PROVIDERS
  const topN = enrich.topN !== undefined ? Math.max(0, Math.floor(Number(enrich.topN) || 0)) : resultCount
  return { abstract: enrich.abstract !== false, topN: Math.min(topN, resultCount), providers }
}

type EnrichResultsOptions = { runtime: SearchRuntimeConfig; offline?: boolean; adapters?: Partial<Record<SearchProviderId, ProviderAdapter>>; cache?: CacheStore; refresh?: boolean }

function isMissing(value: unknown): boolean { return value === null || value === undefined }
function isMissingAuthors(authors: PaperCandidate["authors"]): boolean { return authors.length === 0 }

function mergeDetail(candidate: PaperCandidate, detail: PaperCandidate): PaperCandidate {
  return {
    ...candidate,
    abstract: detail.abstract ?? candidate.abstract,
    venue: isMissing(detail.venue) ? candidate.venue : detail.venue,
    url: isMissing(detail.url) ? candidate.url : detail.url,
    publishedDate: isMissing(detail.publishedDate) ? candidate.publishedDate : detail.publishedDate,
    citationCount: isMissing(detail.citationCount) ? candidate.citationCount : detail.citationCount,
    authors: isMissingAuthors(detail.authors) ? candidate.authors : detail.authors,
  }
}

function wasEnriched(candidate: PaperCandidate, merged: PaperCandidate, fillAbstract: boolean): boolean {
  if (fillAbstract && !candidate.abstract && merged.abstract) return true
  if (candidate.venue === null && merged.venue !== null) return true
  if (candidate.url === null && merged.url !== null) return true
  if (candidate.publishedDate === null && merged.publishedDate !== null) return true
  if (candidate.citationCount === null && merged.citationCount !== null) return true
  if (isMissingAuthors(candidate.authors) && !isMissingAuthors(merged.authors)) return true
  return false
}

export async function enrichResults(results: PaperCandidate[], config: EnrichmentConfig, options: EnrichResultsOptions): Promise<{ results: PaperCandidate[]; enrichedCount: number; enrichCacheHits: EnrichCacheHit[]; diagnostics: SearchDiagnostic[] }> {
  const target = config.topN ? results.slice(0, config.topN) : results
  const providers = config.providers ?? DEFAULT_ENRICH_PROVIDERS
  const fillAbstract = config.abstract !== false
  const cache = options.cache
  const runStamp = new Date().toISOString()
  const diagnostics: SearchDiagnostic[] = []
  const cacheHits: Array<EnrichCacheHit | null> = target.map(() => null)
  const cacheWrites: Array<Promise<void>> = []
  let enrichedCount = 0

  const enriched = await Promise.all(target.map(async (candidate, index) => {
    if (fillAbstract && candidate.abstract) return candidate
    const cacheKey = cache ? enrichCellKey(candidate, providers) : null
    if (cache && cacheKey && !options.refresh) {
      const hit = await cache.read("enrich", cacheKey, isPaperCandidate)
      if (hit) {
        const merged = mergeDetail(candidate, hit.value)
        if (wasEnriched(candidate, merged, fillAbstract)) enrichedCount += 1
        cacheHits[index] = { doi: candidate.doi, externalId: candidate.externalId, title: candidate.title, collectedAt: hit.collectedAt }
        return merged
      }
    }
    for (const provider of providers) {
      const adapter = options.adapters?.[provider] ?? getProvider(provider)
      const detail = adapter.detail
      if (!detail) {
        diagnostics.push({ severity: "info", stage: "enrich", provider, message: `${provider} has no detail lookup; abstract left unchanged.` })
        continue
      }
      try {
        const detailed = await detail({ candidate, runtime: options.runtime, offline: options.offline })
        if (!detailed) continue
        const merged = mergeDetail(candidate, detailed)
        if (wasEnriched(candidate, merged, fillAbstract)) {
          enrichedCount += 1
          if (cache && cacheKey) cacheWrites.push(cache.write("enrich", cacheKey, merged, runStamp))
        }
        return merged
      } catch (error) {
        diagnostics.push({ severity: "warning", stage: "enrich", provider, message: safeErrorMessage(error) })
      }
    }
    return candidate
  }))
  await Promise.all(cacheWrites)

  return { results: config.topN ? [...enriched, ...results.slice(config.topN)] : enriched, enrichedCount, enrichCacheHits: cacheHits.filter((item): item is EnrichCacheHit => item !== null), diagnostics }
}