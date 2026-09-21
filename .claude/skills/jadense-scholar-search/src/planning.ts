import { normalizeDoi, normalizeExternalId, normalizeText, normalizeTitle, publicationYear } from "./normalize.js"
import { SEARCH_PROVIDER_IDS, type DateRange, type PaperCandidate, type SearchProviderId, type WindowStatus } from "./types.js"

export const DEFAULT_LIMIT = 10
export const MAX_LIMIT = 30
export const MAX_QUERIES = 4

export function normalizeQueries(queries: string[]): string[] { return [...new Set(queries.map(normalizeText).filter(Boolean))].slice(0, MAX_QUERIES) }
export function normalizeLimit(limit: number | undefined): number {
  if (limit !== undefined && (!Number.isFinite(limit) || !Number.isInteger(limit))) throw new Error("limit must be an integer.")
  return Math.max(1, Math.min(MAX_LIMIT, limit ?? DEFAULT_LIMIT))
}

export function validateDateRange(value: DateRange | undefined): DateRange | undefined {
  if (!value) return undefined
  const valid = (date: string) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return false
    const parsed = new Date(`${date}T00:00:00.000Z`)
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === date
  }
  if (!valid(value.from) || !valid(value.to) || value.from > value.to) throw new Error("dateRange must contain valid YYYY-MM-DD dates with from <= to.")
  return value
}

export function inferProviders(input: { queries: string[]; providers?: SearchProviderId[] }): SearchProviderId[] {
  const explicit = [...new Set(input.providers ?? [])]
  if (explicit.length) return explicit
  const corpus = input.queries.join(" ").toLowerCase()
  const inferred: SearchProviderId[] = []
  if (/google scholar|谷歌学术|cited by|高被引/.test(corpus)) inferred.push("google_scholar")
  if (/latest|recent|preprint|state[- ]of[- ]the[- ]art|sota|最新|近期|预印本|前沿/.test(corpus)) inferred.push("arxiv")
  if (/pubmed|biomedical|medicine|medical|clinical|biology|genomic|gene|drug|patient|trial|医学|生物|临床|药物|基因|患者/.test(corpus)) inferred.push("pubmed")
  if (/crossref|doi|publisher|metadata|出版社|期刊元数据/.test(corpus)) inferred.push("crossref")
  if (/semantic scholar|semantic_scholar|paperid|citation graph|语义学者|引用图谱/.test(corpus)) inferred.push("semantic_scholar")
  if (/published|journal|citation|review|survey|formal|openalex|发表|期刊|引用|综述|正式论文/.test(corpus)) inferred.push("openalex")
  const unique = [...new Set(inferred)].slice(0, 2)
  return unique.length ? unique : ["openalex"]
}

export function providerId(value: string): SearchProviderId | null { return (SEARCH_PROVIDER_IDS as readonly string[]).includes(value) ? value as SearchProviderId : null }

export function candidateIdentity(candidate: PaperCandidate): string {
  const doi = normalizeDoi(candidate.doi)
  if (doi) return `doi:${doi}`
  if (candidate.externalId) return `${candidate.externalSource}:${normalizeExternalId(candidate.externalSource, candidate.externalId) ?? candidate.externalId}`
  const google = candidate.sourceMetadata.googleScholar
  if (google && typeof google === "object" && !Array.isArray(google)) {
    const resultId = normalizeText(google.resultId)
    if (resultId) return `google_scholar:${resultId}`
  }
  return `title:${normalizeTitle(candidate.title)}`
}

/**
 * 候选的窗口判定（票 06 §3）：无窗格 `unfiltered`；带窗格 `in_window`／`date_unknown`（缺合法
 * `YYYY-MM-DD`，**不丢弃**、带标记入池交下游裁定）／`out_of_window`（日期合法但落窗外，丢弃并计数）。
 * 越窗是正确过滤、无日期是覆盖不确定，两者不得混为一谈。
 */
export function classifyCandidateWindow(candidate: PaperCandidate, dateRange?: DateRange): WindowStatus | "out_of_window" {
  const date = candidate.publishedDate
  if (!dateRange) return "unfiltered"
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return "date_unknown"
  return date >= dateRange.from && date <= dateRange.to ? "in_window" : "out_of_window"
}

/** 精确集源 cap 补偿的切片片数上限（票 06 §2 成本口径：一律切片约为格数 ×6，本票只在 cap 命中时切）。 */
export const MAX_SLICE_COUNT = 6

/** 窗口是否跨年＝按年切片能否真的收窄检索面；单年窗切出来的「片」就是原窗，是空动作、不切片（也不记 year_sliced）。 */
export function isYearSliceable(dateRange: DateRange): boolean {
  return dateRange.from.slice(0, 4) !== dateRange.to.slice(0, 4)
}

/** 按年切片轴：最旧年优先（抬 limit 已带回最新一批，缺口在旧侧），片内区间按原窗裁剪；片数上限由调用方施加。 */
export function yearSlices(dateRange: DateRange): DateRange[] {
  const fromYear = Number(dateRange.from.slice(0, 4))
  const toYear = Number(dateRange.to.slice(0, 4))
  const slices: DateRange[] = []
  for (let year = fromYear; year <= toYear; year += 1) {
    const from = `${year}-01-01` < dateRange.from ? dateRange.from : `${year}-01-01`
    const to = `${year}-12-31` > dateRange.to ? dateRange.to : `${year}-12-31`
    if (from <= to) slices.push({ from, to })
  }
  return slices
}

/** 格内去重：同格多次请求（cap 抬 limit／按年切片）会带回重叠记录，按身份键保留首个。 */
export function dedupeCandidates(candidates: PaperCandidate[]): PaperCandidate[] {
  const seen = new Set<string>()
  return candidates.filter((candidate) => {
    const key = candidateIdentity(candidate)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function completeness(candidate: PaperCandidate): number { return [candidate.doi, candidate.externalId, candidate.abstract, candidate.publishedDate, candidate.venue, candidate.url, candidate.authors.length ? "authors" : null].filter(Boolean).length }
function relevance(candidate: PaperCandidate): number { const value = Number(candidate.sourceMetadata.relevanceScore); return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0 }

/**
 * 窗口标记保留规则（票 06 §3）：同簇只要有窗内成员，该篇即判为窗内——无日期标记消失；簇内只剩无日期
 * 成员时标记存续，正是下游筛选需要裁定的情形。主候选由完备度选出，不能替簇作答，故在此按簇判定。
 */
function clusterWindowStatus(candidates: PaperCandidate[]): WindowStatus | null {
  const statuses = candidates.map((candidate) => candidate.sourceMetadata.windowStatus)
  if (statuses.includes("in_window")) return "in_window"
  if (statuses.includes("unfiltered")) return "unfiltered"
  if (statuses.includes("date_unknown")) return "date_unknown"
  return null
}

function mergeCandidates(candidates: PaperCandidate[]): PaperCandidate {
  const primary = [...candidates].sort((left, right) => completeness(right) - completeness(left) || (right.citationCount ?? 0) - (left.citationCount ?? 0))[0]
  const windowStatus = clusterWindowStatus(candidates)
  return { ...primary, sourceMetadata: { ...primary.sourceMetadata, ...(windowStatus ? { windowStatus } : {}), retrievalProviders: [...new Set(candidates.map((candidate) => candidate.retrievalProvider))], matchedQueries: [...new Set(candidates.map((candidate) => normalizeText(candidate.sourceMetadata.query)).filter(Boolean))], mergedSources: candidates.map((candidate) => ({ provider: candidate.retrievalProvider, externalId: candidate.externalId, doi: candidate.doi })) } }
}

function score(candidate: PaperCandidate, query: string): number {
  const citationScore = Math.min(1, Math.log10((candidate.citationCount ?? 0) + 1) / 5)
  const year = publicationYear(candidate)
  const recencyScore = year ? Math.max(0, Math.min(1, (new Date().getUTCFullYear() - year + 1) / 10)) : 0
  const exactTitle = normalizeTitle(candidate.title) === normalizeTitle(query) ? 0.2 : 0
  return relevance(candidate) * 0.6 + citationScore * 0.25 + recencyScore * 0.15 + exactTitle
}

export function rankCandidates(input: Array<{ candidate: PaperCandidate; query: string }>): PaperCandidate[] {
  const groups = new Map<string, Array<{ candidate: PaperCandidate; query: string }>>()
  for (const item of input) {
    const key = candidateIdentity(item.candidate)
    groups.set(key, [...(groups.get(key) ?? []), item])
  }
  return [...groups.values()].map((group) => ({ candidate: mergeCandidates(group.map((item) => item.candidate)), score: Math.max(...group.map((item) => score(item.candidate, item.query))) })).sort((left, right) => right.score - left.score || left.candidate.title.localeCompare(right.candidate.title)).map(({ candidate, score: finalScore }, index) => ({ ...candidate, sourceMetadata: { ...candidate.sourceMetadata, rank: index + 1, finalScore } }))
}
