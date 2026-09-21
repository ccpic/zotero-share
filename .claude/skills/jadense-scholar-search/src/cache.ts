import { createHash } from "node:crypto"
import { mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { normalizeDoi, normalizeExternalId, normalizeTitle } from "./normalize.js"
import type { DateRange, PaperCandidate, ProviderSearchResult, SearchProviderId } from "./types.js"

/** 有效期窗口（workspace-guide §14.2：检索响应与回填统一 168 小时）。 */
export const CACHE_TTL_HOURS = 168

const CACHE_VERSION = 1
const HOUR_MS = 60 * 60 * 1000

export type CacheScope = "search" | "enrich"

/** 命中读数：`collectedAt` ＝数据自身采集时刻（缓存写入时刻），不是本次运行时刻。 */
export type CacheHit<T> = {
  value: T
  collectedAt: string
}

export type CacheStore = {
  read<T>(scope: CacheScope, key: string, isValid: (value: unknown) => value is T): Promise<CacheHit<T> | null>
  /** `collectedAt` ＝该格数据的自身采集时刻，同时是 TTL 到期基准；调用方声明，不由存储层现编。 */
  write(scope: CacheScope, key: string, value: unknown, collectedAt: string): Promise<void>
}

export type FileCacheOptions = {
  /** 缓存自身故障（不可读、损坏、写不进去）的通报口；缓存永不因自身故障中断检索。 */
  onIssue?: (message: string) => void
}

/**
 * 文件缓存：一格一文件（`<root>/<scope>/<sha256(key)>.json`），TTL 到期即 miss。
 * 到期基准＝缓存写入时刻，命中不重写、不顺延；损坏或对不上的条目按 miss 处理（悬空即 miss）。
 */
export function createFileCache(root: string, options: FileCacheOptions = {}): CacheStore {
  const reportIssue = options.onIssue ?? (() => undefined)
  return {
    async read<T>(scope: CacheScope, key: string, isValid: (value: unknown) => value is T): Promise<CacheHit<T> | null> {
      const file = entryPath(root, scope, key)
      let raw: string
      try {
        raw = await readFile(file, "utf8")
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") reportIssue(`cannot read ${scope} entry: ${error instanceof Error ? error.message : String(error)}`)
        return null
      }
      const entry = parseEntry(raw)
      if (!entry || entry.scope !== scope || entry.key !== key || !isIsoDate(entry.collectedAt)) {
        reportIssue(`ignoring malformed ${scope} entry`)
        return null
      }
      if (Date.now() - Date.parse(entry.collectedAt) > CACHE_TTL_HOURS * HOUR_MS) return null
      if (!isValid(entry.value)) {
        reportIssue(`ignoring ${scope} entry with an unexpected value shape`)
        return null
      }
      return { value: entry.value, collectedAt: entry.collectedAt }
    },
    async write(scope: CacheScope, key: string, value: unknown, collectedAt: string): Promise<void> {
      const file = entryPath(root, scope, key)
      const entry = { version: CACHE_VERSION, scope, key, collectedAt, value }
      try {
        await mkdir(dirname(file), { recursive: true })
        const staging = `${file}.${process.pid}.staging`
        await writeFile(staging, `${JSON.stringify(entry)}\n`, "utf8")
        await rename(staging, file)
      } catch (error) {
        reportIssue(`cannot write ${scope} entry: ${error instanceof Error ? error.message : String(error)}`)
      }
    },
  }
}

/** provider 原始响应键：provider＋归一查询＋limit＋日期窗＋离线标志（窗口见 workspace-guide §14.2）；离线格与联网格不共命名空间——离线跑当前不读不写缓存，该分量即第二道守卫。 */
export function searchCellKey(input: { provider: SearchProviderId; query: string; limit: number; dateRange?: DateRange | null; offline: boolean }): string {
  return JSON.stringify(canonicalize({
    provider: input.provider,
    query: input.query,
    limit: input.limit,
    dateRange: input.dateRange ? { from: input.dateRange.from, to: input.dateRange.to } : null,
    offline: input.offline,
  }))
}

/** 回填合并结果键：`DOI ?? 外部标识 ?? 归一题名`＋provider 集合；无任何身份项时不入缓存。 */
export function enrichCellKey(candidate: PaperCandidate, providers: SearchProviderId[]): string | null {
  const doi = normalizeDoi(candidate.doi)
  const externalId = doi ? null : normalizeExternalId(candidate.externalSource, candidate.externalId)
  const title = doi || externalId ? null : normalizeTitle(candidate.title)
  const identity = doi ? `doi:${doi}` : externalId ? `id:${candidate.externalSource}:${externalId}` : title ? `title:${title}` : null
  return identity ? JSON.stringify(canonicalize({ identity, providers: [...providers].sort() })) : null
}

/** 缓存值形状守卫：只放行结构完整的候选条目（缺字段的条目按 miss 处理，绝不带进结果）。 */
export function isPaperCandidate(value: unknown): value is PaperCandidate {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const candidate = value as PaperCandidate
  return typeof candidate.title === "string" && typeof candidate.retrievalProvider === "string" && typeof candidate.externalSource === "string"
    && Array.isArray(candidate.authors) && Array.isArray(candidate.diagnostics)
}

/**
 * search 专属载荷形态守卫：落盘形态就是 provider 取数收据那一对「候选＋总数」（只收不可复算读数——日期三计数、
 * 实返由载荷＋键复算，不入载荷，同一读数不得两处真源），故直接复用 `ProviderSearchResult`，不另立同形类型。
 * **无旧形态兼容分支**（票 09 §1）：旧格（裸 `PaperCandidate[]`）形状对不上即按 miss 处理，触发重取并改写新形态。
 */
export function isSearchCellPayload(value: unknown): value is ProviderSearchResult {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const payload = value as ProviderSearchResult
  if (!Array.isArray(payload.candidates) || !payload.candidates.every(isPaperCandidate)) return false
  return payload.totalAvailable === null || (typeof payload.totalAvailable === "number" && Number.isInteger(payload.totalAvailable) && payload.totalAvailable >= 0)
}

function entryPath(root: string, scope: CacheScope, key: string): string {
  return join(root, scope, `${createHash("sha256").update(key, "utf8").digest("hex")}.json`)
}

type CacheEntry = { version: number; scope: string; key: string; collectedAt: unknown; value: unknown }

function parseEntry(raw: string): CacheEntry | null {
  try {
    const parsed = JSON.parse(raw) as CacheEntry | null
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) && parsed.version === CACHE_VERSION ? parsed : null
  } catch {
    return null
  }
}

function isIsoDate(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value))
}

/** 键对象按键名排序后序列化：同一格在任何构造顺序下都得同一把钥匙。 */
function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
    return Object.fromEntries(entries.map(([name, item]) => [name, canonicalize(item)]))
  }
  return value
}
