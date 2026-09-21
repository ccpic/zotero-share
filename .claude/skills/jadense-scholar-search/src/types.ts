export const SEARCH_PROVIDER_IDS = [
  "arxiv",
  "openalex",
  "crossref",
  "pubmed",
  "semantic_scholar",
  "google_scholar",
] as const

export type SearchProviderId = (typeof SEARCH_PROVIDER_IDS)[number]

export type DateRange = {
  from: string
  to: string
}

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue | undefined }

export type PaperAuthor = {
  displayName: string
  orcid?: string | null
  affiliation?: string | null
}

export type SearchDiagnostic = {
  severity: "info" | "warning" | "error"
  stage: string
  message: string
  provider?: SearchProviderId
  query?: string | null
  externalId?: string | null
}

export type PaperCandidate = {
  retrievalProvider: SearchProviderId
  externalSource: SearchProviderId
  externalId: string | null
  doi: string | null
  title: string
  authors: PaperAuthor[]
  abstract: string | null
  publishedDate: string | null
  venue: string | null
  url: string | null
  citationCount: number | null
  sourceMetadata: Record<string, JsonValue>
  diagnostics: SearchDiagnostic[]
}

export type PaperIdentity = {
  doi?: string | null
  externalSource?: SearchProviderId | null
  externalId?: string | null
}

export type PaperReference = {
  identity?: PaperIdentity
  title: string
  authors: PaperAuthor[]
  publishedDate: string | null
  venue: string | null
  doi: string | null
  url: string | null
  citationCount?: number | null
  source: SearchProviderId
}

export type ProviderCredentials = {
  serpApiKey?: string
  semanticScholarApiKey?: string
  ncbiApiKey?: string
  ncbiTool?: string
}

export type SearchRuntimeConfig = {
  fetch?: typeof fetch
  userAgent: string
  contactEmail?: string
  credentials?: ProviderCredentials
}

export type ProviderSearchInput = {
  query: string
  limit: number
  dateRange?: DateRange
  runtime: SearchRuntimeConfig
  offline?: boolean
}

export type ProviderDetailInput = {
  candidate: PaperCandidate
  runtime: SearchRuntimeConfig
  offline?: boolean
}

/** 源类型（源扩展票 03 §4 逐源标注）：精确集源的总数是检索集规模、可受理地穷尽；召回形源的总数是全文命中形，只作披露不作漏检量。 */
export type ProviderSourceType = "exact_set" | "recall_form"

/** 取数结果：候选＋该格总数。总数不可得记 `null`，不得以 0 或实返数冒充（源失败与上限票 06 §2）。 */
export type ProviderSearchResult = {
  candidates: PaperCandidate[]
  totalAvailable: number | null
}

export type ProviderAdapter = {
  id: SearchProviderId
  /** 缺省按 `recall_form` 处理：不抬 limit、不切片——宁可少花钱也不把召回形读数当漏检量。 */
  sourceType?: ProviderSourceType
  search(input: ProviderSearchInput): Promise<ProviderSearchResult>
  detail?: (input: ProviderDetailInput) => Promise<PaperCandidate | null>
  references?: (input: ProviderDetailInput) => Promise<PaperReference[] | null>
}

export type EnrichmentConfig = {
  abstract?: boolean
  topN?: number
  providers?: SearchProviderId[]
}

export type SearchRequest = {
  queries: string[]
  providers?: SearchProviderId[]
  limit?: number
  dateRange?: DateRange
}

/**
 * cap 处置分层（源失败与上限票 06 §2）：`none`＝未截断；`unknown`＝总数不可得（未取数／源不报总数，
 * 不得读作未截断）；`recorded_only`＝召回形源截断，只记总数；`raised_limit`＝精确集源截断且单次 limit
 * 已到 `min(total, MAX_LIMIT)` 上限，剩余缺口进账；`year_sliced`＝精确集源带窗格截断，已按年切片扩面。
 */
export type CapPolicy = "none" | "unknown" | "recorded_only" | "raised_limit" | "year_sliced"

/** 候选的窗口判定（票 06 §3）：无窗格 `unfiltered`；带窗格 `in_window`／`date_unknown`（无合法日期，保留入池）。 */
export type WindowStatus = "in_window" | "date_unknown" | "unfiltered"

export type QueryStatus = {
  query: string
  provider: SearchProviderId
  status: "success" | "error"
  hitCount: number
  /** 该格总数；不可得为 `null`（票 06 §2）。 */
  totalAvailable: number | null
  /** 是否被 limit 截断；总数不可得（含未取数）为 `null`——`null` 不得读作 `false`（票 09 §2）。 */
  capHit: boolean | null
  capPolicy: CapPolicy
  /** 窗内候选数；无窗格时＝该格入选候选数（其 `windowStatus` 皆为 `unfiltered`）。 */
  dateFilteredCount: number
  /** 无合法出版日期而带标记保留入池的候选数（带窗格；无窗格 0）。 */
  dateUnknownCount: number
  /** 出版日期合法但落窗外被丢弃的候选数（带窗格；无窗格 0）。 */
  dateRejectedCount: number
  /** 该格本次实际发起的取数调用次数：缓存命中 0；正常 1；含同格重试与 cap 补偿的额外调用。 */
  attempts: number
  /** 是否触发过同格同源瞬时错误重试（票 06 §1）。 */
  retried: boolean
  /** 重试后是否取到数据（重试未恢复时为 false，该格仍记 error）。 */
  recoveredByRetry: boolean
  error?: string
  /** 该格是否由缓存命中供数（缓存关闭或未命中皆 false）。 */
  cacheHit: boolean
  /** 数据自身采集时刻（ISO 8601）：命中的是缓存写入时刻，本次取数的是运行时刻；无数据为 null。 */
  collectedAt: string | null
}

/** 回填缓存命中读数：一命中一条，记数据自身采集时刻（不是重跑时刻）。 */
export type EnrichCacheHit = {
  doi: string | null
  externalId: string | null
  title: string
  collectedAt: string
}

export type SearchQueryPlan = {
  queries: string[]
  providers: SearchProviderId[]
  limit: number
  dateRange: DateRange | null
  offline: boolean
  totalHitCount: number
  dateRejectedCandidateCount: number
  mergedCandidateCount: number
  enrichedCount: number
  enrichCacheHits: EnrichCacheHit[]
  queryStatuses: QueryStatus[]
}

export type SearchResponse = {
  results: PaperCandidate[]
  queryPlan: SearchQueryPlan
  diagnostics: SearchDiagnostic[]
}

export type PersistenceAdapter = {
  save(candidates: PaperCandidate[]): Promise<void>
}