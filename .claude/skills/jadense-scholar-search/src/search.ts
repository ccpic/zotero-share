import { isSearchCellPayload, searchCellKey, type CacheStore } from "./cache.js"
import { enrichResults, normalizeEnrichConfig } from "./enrichment.js"
import { MissingCredentialError, ProviderRequestError, safeErrorMessage, type ProviderErrorClassification } from "./errors.js"
import { MAX_LIMIT, MAX_SLICE_COUNT, classifyCandidateWindow, dedupeCandidates, inferProviders, isYearSliceable, normalizeLimit, normalizeQueries, rankCandidates, validateDateRange, yearSlices } from "./planning.js"
import { getProvider, providers } from "./providers/index.js"
import type { CapPolicy, EnrichCacheHit, EnrichmentConfig, PaperCandidate, PersistenceAdapter, ProviderAdapter, ProviderSearchInput, ProviderSearchResult, ProviderSourceType, QueryStatus, SearchDiagnostic, SearchRequest, SearchResponse, SearchRuntimeConfig, SearchProviderId, WindowStatus } from "./types.js"

export type SearchOptions = {
  runtime: SearchRuntimeConfig
  offline?: boolean
  persistence?: PersistenceAdapter
  adapters?: Partial<Record<SearchProviderId, ProviderAdapter>>
  enrich?: boolean | EnrichmentConfig
  /** provider 原始响应缓存（键与 TTL 见 workspace-guide §14.2）；不传即不缓存。 */
  cache?: CacheStore
  /** 显式重取：越过已有缓存格重新取数（仍写回新读数），窗口不因此顺延。 */
  refresh?: boolean
}

/** 该格本次的取数故事（票 06 §4）：调用数、是否触发过瞬时重试、重试是否真的取到数据。 */
type CellAttempts = { attempts: number; retried: boolean; recoveredByRetry: boolean }

type SearchCell = CellAttempts & {
  /** 该格的源类型（缺省召回形）：读数与取数同取一处，不各查一遍 adapter。 */
  sourceType: ProviderSourceType
  /** 日期过滤前的候选：cap 补偿后的并集，也可能进缓存载荷（票 09 §1）。 */
  candidates: PaperCandidate[]
  totalAvailable: number | null
  cacheHit: boolean
  collectedAt: string | null
  /** cap 补偿请求失败的留痕：补偿失败不丢已取到的候选，缺口由 capHit／capPolicy 进账，此处只交代原因。 */
  compensationFailure: string | null
}

/** 失败格同样要交代取数故事（票 06 §4 的「已同源重跑 n 次{恢复|未恢复}」），故重试读数随失败一起返回。 */
type CellFailure = CellAttempts & { error: unknown }
type CellOutcome = { ok: true; cell: SearchCell } | { ok: false; failure: CellFailure }
type CellCounters = { attempts: number; retried: boolean; recoveredByRetry: boolean }

/** 同格同源有界重试（票 06 §1）：整格一次、格内串行，只对可瞬时恢复的错误类出手。 */
const RETRY_BASE_MS = 800
const RETRYABLE_CLASSES: Partial<Record<ProviderErrorClassification, true>> = { unavailable: true, quota_exhausted: true }

function validateCandidate(candidate: PaperCandidate): PaperCandidate {
  if (!candidate.title || !candidate.retrievalProvider || !candidate.externalSource) throw new Error("Provider returned an invalid paper candidate.")
  return candidate
}

type CapCompensation = { result: ProviderSearchResult; compensationFailure: string | null }

/**
 * cap 分层处置（票 06 §2）：精确集源截断先抬 `limit` 至 `min(total, MAX_LIMIT)`；仍超且跨年则按年切片
 * （片数上限 `MAX_SLICE_COUNT`，剩余缺口进账）。召回形源只记总数——`count` 不作漏检量，也不为它多花请求。
 * 补偿请求失败**不丢已经取到的候选**：就地收手、把原因带出去，缺口由 capHit／capPolicy 进账（票 06 §4 的
 * 「剩余缺口进账」而非整格作废）。
 */
async function fetchWithCapHandling(
  input: ProviderSearchInput,
  sourceType: ProviderSourceType,
  call: (override: Partial<ProviderSearchInput>) => Promise<ProviderSearchResult>,
): Promise<CapCompensation> {
  const first = await call({})
  let candidates = first.candidates
  let totalAvailable = first.totalAvailable
  const done = (compensationFailure: string | null = null): CapCompensation => ({ result: { candidates, totalAvailable }, compensationFailure })
  if (sourceType !== "exact_set" || totalAvailable === null || candidates.length >= totalAvailable) return done()
  const raisedLimit = Math.min(totalAvailable, MAX_LIMIT)
  if (raisedLimit > input.limit) {
    try {
      const raised = await call({ limit: raisedLimit })
      candidates = dedupeCandidates([...candidates, ...raised.candidates])
      totalAvailable = raised.totalAvailable ?? totalAvailable
    } catch (error) {
      return done(safeErrorMessage(error))
    }
  }
  if (input.dateRange && isYearSliceable(input.dateRange) && candidates.length < totalAvailable) {
    for (const slice of yearSlices(input.dateRange).slice(0, MAX_SLICE_COUNT)) {
      try {
        const sliced = await call({ limit: MAX_LIMIT, dateRange: slice })
        candidates = dedupeCandidates([...candidates, ...sliced.candidates])
      } catch (error) {
        return done(safeErrorMessage(error))
      }
      if (candidates.length >= totalAvailable) break
    }
  }
  return done()
}

/**
 * cap 处置读数（票 06 §2／票 09 §2）：只由源类型、总数、实返与切片轴判定，故缓存命中格与本次取数格得出
 * 同一结论。总数不可得（含未取数）记 `unknown`——`null` 不得读作未截断。
 */
function capPolicyFor(sourceType: ProviderSourceType, totalAvailable: number | null, hitCount: number, yearSliceable: boolean): CapPolicy {
  if (totalAvailable === null) return "unknown"
  if (totalAvailable <= hitCount) return "none"
  if (sourceType !== "exact_set") return "recorded_only"
  return yearSliceable && totalAvailable > MAX_LIMIT ? "year_sliced" : "raised_limit"
}

/** 入池即带窗口标记；返回副本而非就地改——缓存载荷引用同一批对象，就地改会把本次运行标记写进载荷。 */
function markWindow(candidate: PaperCandidate, windowStatus: WindowStatus): PaperCandidate {
  return { ...candidate, sourceMetadata: { ...candidate.sourceMetadata, windowStatus } }
}

export async function executeSearch(request: SearchRequest, options: SearchOptions): Promise<SearchResponse> {
  const queries = normalizeQueries(request.queries)
  if (!queries.length) throw new Error("At least one non-empty query is required.")
  const limit = normalizeLimit(request.limit)
  const dateRange = validateDateRange(request.dateRange)
  const selectedProviders = inferProviders({ queries, providers: request.providers })
  if (!options.offline && selectedProviders.includes("google_scholar") && !options.runtime.credentials?.serpApiKey) throw new MissingCredentialError("google_scholar", "SERPAPI_API_KEY")

  const offline = Boolean(options.offline)
  // 离线跑走内置夹具、零网络成本，缓存一并不开也不写，免得夹具读数落进真实命名空间。
  const cache = offline ? undefined : options.cache
  const runStamp = new Date().toISOString()

  const diagnostics: SearchDiagnostic[] = []
  const jobs = selectedProviders.flatMap((provider) => queries.map((query) => ({ provider, query })))
  const cacheWrites: Array<Promise<void>> = []

  const fetchCell = async (job: { provider: SearchProviderId; query: string }): Promise<CellOutcome> => {
    const key = searchCellKey({ provider: job.provider, query: job.query, limit, dateRange: dateRange ?? null, offline })
    const adapter = options.adapters?.[job.provider] ?? getProvider(job.provider)
    const sourceType = adapter.sourceType ?? "recall_form"
    if (cache && !options.refresh) {
      // 旧形态（裸候选数组）对新载荷守卫形状不符，按既有「形状对不上即 miss」语义重取并改写新形态（票 09 §1）。
      const hit = await cache.read("search", key, isSearchCellPayload)
      if (hit) return { ok: true, cell: { sourceType, candidates: hit.value.candidates, totalAvailable: hit.value.totalAvailable, cacheHit: true, collectedAt: hit.collectedAt, attempts: 0, retried: false, recoveredByRetry: false, compensationFailure: null } }
    }
    const counters: CellCounters = { attempts: 0, retried: false, recoveredByRetry: false }
    const input: ProviderSearchInput = { query: job.query, limit, dateRange, runtime: options.runtime, offline: options.offline }
    const call = async (override: Partial<ProviderSearchInput>): Promise<ProviderSearchResult> => {
      const attempt = async (): Promise<ProviderSearchResult> => {
        counters.attempts += 1
        const result = await adapter.search({ ...input, ...override })
        return { candidates: result.candidates.map(validateCandidate), totalAvailable: result.totalAvailable }
      }
      try {
        return await attempt()
      } catch (error) {
        const transient = error instanceof ProviderRequestError && RETRYABLE_CLASSES[error.classification] === true
        if (counters.retried || !transient) throw error
        counters.retried = true
        await new Promise<void>((resolve) => setTimeout(resolve, RETRY_BASE_MS * (1 + Math.random())))
        const result = await attempt()
        counters.recoveredByRetry = true
        return result
      }
    }
    try {
      const compensated = await fetchWithCapHandling(input, sourceType, call)
      if (cache) cacheWrites.push(cache.write("search", key, compensated.result, runStamp))
      return { ok: true, cell: { ...compensated.result, sourceType, cacheHit: false, collectedAt: runStamp, attempts: counters.attempts, retried: counters.retried, recoveredByRetry: counters.recoveredByRetry, compensationFailure: compensated.compensationFailure } }
    } catch (error) {
      // 该格没交出数据：retried 记实（交代重试故事），recoveredByRetry 一律 false——否则覆盖账会把 error 格说成「已恢复」。
      return { ok: false, failure: { error, attempts: counters.attempts, retried: counters.retried, recoveredByRetry: false } }
    }
  }

  const outcomes = await Promise.all(jobs.map(fetchCell))
  const pooled: Array<{ candidate: PaperCandidate; query: string }> = []
  const queryStatuses: QueryStatus[] = []
  let dateRejectedCount = 0

  outcomes.forEach((outcome, index) => {
    const job = jobs[index]
    if (!outcome.ok) {
      const message = safeErrorMessage(outcome.failure.error)
      diagnostics.push({ severity: "warning", stage: "provider", provider: job.provider, query: job.provider === "google_scholar" ? null : job.query, message })
      queryStatuses.push({ query: job.query, provider: job.provider, status: "error", hitCount: 0, totalAvailable: null, capHit: null, capPolicy: "unknown", dateFilteredCount: 0, dateUnknownCount: 0, dateRejectedCount: 0, attempts: outcome.failure.attempts, retried: outcome.failure.retried, recoveredByRetry: outcome.failure.recoveredByRetry, error: message, cacheHit: false, collectedAt: null })
      return
    }
    const cell = outcome.cell
    if (cell.compensationFailure) diagnostics.push({ severity: "warning", stage: "provider", provider: job.provider, query: job.provider === "google_scholar" ? null : job.query, message: `cap compensation failed; the residual gap is recorded: ${cell.compensationFailure}` })
    let windowCount = 0
    let unknownCount = 0
    let rejectedCount = 0
    for (const candidate of cell.candidates) {
      const verdict = classifyCandidateWindow(candidate, dateRange)
      if (verdict === "out_of_window") {
        rejectedCount += 1
        continue
      }
      pooled.push({ candidate: markWindow(candidate, verdict), query: job.query })
      if (verdict === "date_unknown") unknownCount += 1
      else windowCount += 1
    }
    dateRejectedCount += rejectedCount
    queryStatuses.push({
      query: job.query,
      provider: job.provider,
      status: "success",
      hitCount: cell.candidates.length,
      totalAvailable: cell.totalAvailable,
      capHit: cell.totalAvailable === null ? null : cell.totalAvailable > cell.candidates.length,
      capPolicy: capPolicyFor(cell.sourceType, cell.totalAvailable, cell.candidates.length, Boolean(dateRange && isYearSliceable(dateRange))),
      dateFilteredCount: windowCount,
      dateUnknownCount: unknownCount,
      dateRejectedCount: rejectedCount,
      attempts: cell.attempts,
      retried: cell.retried,
      recoveredByRetry: cell.recoveredByRetry,
      cacheHit: cell.cacheHit,
      collectedAt: cell.collectedAt,
    })
  })
  await Promise.all(cacheWrites)

  const results = rankCandidates(pooled)
  const enrichConfig = normalizeEnrichConfig(options.enrich, results.length)
  let finalResults = results
  let enrichedCount = 0
  let enrichCacheHits: EnrichCacheHit[] = []
  if (enrichConfig) {
    const out = await enrichResults(results, enrichConfig, { runtime: options.runtime, offline: options.offline, adapters: options.adapters, cache, refresh: options.refresh })
    finalResults = out.results
    enrichedCount = out.enrichedCount
    enrichCacheHits = out.enrichCacheHits
    diagnostics.push(...out.diagnostics)
  }
  if (options.persistence) await options.persistence.save(finalResults)
  return { results: finalResults, queryPlan: { queries, providers: selectedProviders, limit, dateRange: dateRange ?? null, offline, totalHitCount: pooled.length + dateRejectedCount, dateRejectedCandidateCount: dateRejectedCount, mergedCandidateCount: results.length, enrichedCount, enrichCacheHits, queryStatuses }, diagnostics }
}

export { providers }
