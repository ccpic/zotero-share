import { ProviderRequestError } from "../errors.js"
import type { SearchProviderId, SearchRuntimeConfig } from "../types.js"

export type JsonRecord = Record<string, unknown>

export function record(value: unknown): JsonRecord | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : null
}

export function runtimeFetch(runtime: SearchRuntimeConfig): typeof fetch {
  return runtime.fetch ?? fetch
}

/**
 * 源节流与并发上限（源扩展票 03 §4）：PubMed ≤3 rps 串行（实测 3 并发即全 429）、arXiv ToU ≤1 req/3s 单连接、
 * 其余源按各自预算。所有 provider HTTP 请求都经此排队——检索、同格重试与 cap 补偿的请求一视同仁。
 */
export type SourceBudget = { maxConcurrent: number; minIntervalMs: number }

const SOURCE_BUDGETS: Record<SearchProviderId, SourceBudget> = {
  pubmed: { maxConcurrent: 1, minIntervalMs: 334 },
  arxiv: { maxConcurrent: 1, minIntervalMs: 3000 },
  openalex: { maxConcurrent: 4, minIntervalMs: 100 },
  crossref: { maxConcurrent: 4, minIntervalMs: 100 },
  semantic_scholar: { maxConcurrent: 1, minIntervalMs: 1000 },
  google_scholar: { maxConcurrent: 1, minIntervalMs: 1000 },
}

type SourceQueue = { active: number; nextStartAt: number; waiting: Array<() => void> }

const sourceQueues = new Map<SearchProviderId, SourceQueue>()

function queueFor(provider: SearchProviderId): SourceQueue {
  const existing = sourceQueues.get(provider)
  if (existing) return existing
  const created: SourceQueue = { active: 0, nextStartAt: 0, waiting: [] }
  sourceQueues.set(provider, created)
  return created
}

/**
 * 取一个该源的请求槽：同源并发不超上限、请求起始按预算拉开；返回释放函数，调用方务必 finally 释放。
 * 起始时刻**先原子占用再睡**——多槽位源（OpenAlex／Crossref）若让等待者各自读同一个「上次起始时刻」，
 * 它们会在同一毫秒一起出发，恰恰把为防突发而设的预算作废。
 * 用 `new Promise` 执行器而非 `Promise.withResolvers`：包 `engines` 与 CI 都停在 Node 20，该 API 自 Node 22 才有。
 */
async function acquireSourceSlot(provider: SearchProviderId): Promise<() => void> {
  const budget = SOURCE_BUDGETS[provider]
  const queue = queueFor(provider)
  if (queue.active >= budget.maxConcurrent) await new Promise<void>((resolve) => queue.waiting.push(resolve))
  queue.active += 1
  const now = Date.now()
  const startAt = Math.max(queue.nextStartAt, now)
  queue.nextStartAt = startAt + budget.minIntervalMs
  if (startAt > now) await new Promise<void>((resolve) => setTimeout(resolve, startAt - now))
  return () => {
    queue.active -= 1
    queue.waiting.shift()?.()
  }
}

/** 总数读数（票 06 §2）：只认非负整数，缺字段／`null`／空串／非数一律 `null`——不得以 0 或实返数冒充。 */
export function totalOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null
  const number = Number(value)
  return Number.isInteger(number) && number >= 0 ? number : null
}

export function providerHeaders(runtime: SearchRuntimeConfig, accept: string): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: accept,
    "User-Agent": runtime.userAgent,
  }
  if (runtime.contactEmail) headers["X-Contact-Email"] = runtime.contactEmail
  return headers
}

function classifyStatus(status: number): ProviderRequestError["classification"] {
  if (status === 400) return "invalid_request"
  if (status === 401 || status === 403) return "auth_failed"
  if (status === 429) return "quota_exhausted"
  if (status >= 500) return "unavailable"
  return "provider_error"
}

export async function requestText(
  url: string,
  runtime: SearchRuntimeConfig,
  provider: SearchProviderId,
  init?: RequestInit,
): Promise<string> {
  const release = await acquireSourceSlot(provider)
  let response: Response
  try {
    response = await runtimeFetch(runtime)(url, { ...init, headers: { ...providerHeaders(runtime, "text/plain, */*"), ...init?.headers } })
  } catch {
    throw new ProviderRequestError(`${provider} is temporarily unavailable.`, null, "unavailable", provider)
  } finally {
    release()
  }
  if (!response.ok) {
    throw new ProviderRequestError(`${provider} request failed with status ${response.status}.`, response.status, classifyStatus(response.status), provider)
  }
  return response.text()
}

export async function requestJson(
  url: string,
  runtime: SearchRuntimeConfig,
  provider: SearchProviderId,
  init?: RequestInit,
): Promise<unknown> {
  const text = await requestText(url, runtime, provider, {
    ...init,
    headers: { ...providerHeaders(runtime, "application/json"), ...init?.headers },
  })
  try {
    return JSON.parse(text) as unknown
  } catch {
    throw new ProviderRequestError(`${provider} returned invalid JSON.`, null, "provider_error", provider)
  }
}

export function yearFromDate(value: unknown): number | null {
  const text = typeof value === "string" ? value : ""
  const match = text.match(/(?:19|20)\d{2}/)
  return match ? Number(match[0]) : null
}
