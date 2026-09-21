import assert from "node:assert/strict"
import { mkdtemp } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import test from "node:test"
import { createFileCache, isSearchCellPayload, searchCellKey } from "../src/cache.js"
import { ProviderRequestError } from "../src/errors.js"
import { executeSearch } from "../src/search.js"
import type { DateRange, PaperCandidate, ProviderAdapter, ProviderSearchInput, SearchRuntimeConfig } from "../src/types.js"

const runtime: SearchRuntimeConfig = { userAgent: "hardening-agent/1.0" }

function paper(overrides: Partial<PaperCandidate> = {}): PaperCandidate {
  return { retrievalProvider: "openalex", externalSource: "openalex", externalId: "W1", doi: "10.1234/w1", title: "Paper", authors: [{ displayName: "Researcher" }], abstract: null, publishedDate: "2024-06-01", venue: null, url: null, citationCount: 0, sourceMetadata: { relevanceScore: 1 }, diagnostics: [], ...overrides }
}

function fixed(results: PaperCandidate[], totalAvailable: number | null): ProviderAdapter {
  return { id: "openalex", search: async () => ({ candidates: results, totalAvailable }) }
}

function transientError(status: number, classification: "unavailable" | "quota_exhausted" | "invalid_request" | "auth_failed"): ProviderRequestError {
  return new ProviderRequestError(`openalex request failed with status ${status}.`, status, classification, "openalex")
}

// 真钟例外（`ts-no-test-timers`）：退避本身就是被测行为（800–1600ms 含 jitter），确定性时钟会绕开它；
// 两个瞬时可重试类各一次，共约 2 秒，是本套件唯一的实等待。
test("transient provider failures are retried once inside the cell, other classes are not", async () => {
  for (const [status, classification] of [[500, "unavailable"], [429, "quota_exhausted"]] as const) {
    let calls = 0
    const adapter: ProviderAdapter = { id: "openalex", search: async () => { calls += 1; if (calls === 1) throw transientError(status, classification); return { candidates: [paper()], totalAvailable: null } } }
    const cache = createFileCache(await mkdtemp(join(tmpdir(), "jadense-retry-key-")))
    const response = await executeSearch({ queries: ["q"], providers: ["openalex"] }, { runtime, adapters: { openalex: adapter }, cache })
    const cell = response.queryPlan.queryStatuses[0]
    assert.equal(response.results.length, 1, `${status} should recover`)
    assert.equal(cell.attempts, 2, `${status} retries exactly once`)
    assert.equal(cell.retried, true)
    assert.equal(cell.recoveredByRetry, true)
    assert.equal(calls, 2)

    // 重试不进缓存键：重试格写下的格，同键复跑直接命中（零请求、零重试）。
    const replay = await executeSearch({ queries: ["q"], providers: ["openalex"] }, { runtime, adapters: { openalex: adapter }, cache })
    assert.equal(calls, 2, "the replay must not fetch again")
    assert.equal(replay.queryPlan.queryStatuses[0].cacheHit, true)
    assert.equal(replay.queryPlan.queryStatuses[0].attempts, 0)
    assert.equal(replay.queryPlan.queryStatuses[0].retried, false)
  }

  for (const [status, classification] of [[400, "invalid_request"], [401, "auth_failed"]] as const) {
    let calls = 0
    const adapter: ProviderAdapter = { id: "openalex", search: async () => { calls += 1; throw transientError(status, classification) } }
    const response = await executeSearch({ queries: ["q"], providers: ["openalex"] }, { runtime, adapters: { openalex: adapter } })
    const cell = response.queryPlan.queryStatuses[0]
    assert.equal(cell.status, "error", `${status} must not recover`)
    assert.equal(cell.attempts, 1, `${status} must not be re-requested`)
    assert.equal(cell.retried, false)
    assert.equal(cell.capHit, null, "an unfetched cell must not read as untruncated")
    assert.equal(cell.capPolicy, "unknown")
    assert.equal(calls, 1)
  }
})

test("exact-set cap handling raises the limit, then slices by year for the rest", async () => {
  const requests: Array<{ limit: number; dateRange: DateRange | undefined }> = []
  const exact = (y2024: number, y2025: number): ProviderAdapter => {
    // ID 用真实 PubMed 形态（纯数字）：身份归一按数字裁剪，编造的 alphanumeric id 会被并成同一篇。
    const corpus = [...Array.from({ length: y2024 }, (_, index) => ({ id: String(39000000 + index), year: 2024 })), ...Array.from({ length: y2025 }, (_, index) => ({ id: String(39100000 + index), year: 2025 }))]
    return {
      id: "pubmed",
      sourceType: "exact_set",
      search: async (input: ProviderSearchInput) => {
        requests.push({ limit: input.limit, dateRange: input.dateRange })
        const inRange = corpus.filter((item) => !input.dateRange || (item.year >= Number(input.dateRange.from.slice(0, 4)) && item.year <= Number(input.dateRange.to.slice(0, 4))))
        return { candidates: inRange.slice(0, input.limit).map((item) => paper({ retrievalProvider: "pubmed", externalSource: "pubmed", externalId: item.id, doi: null, publishedDate: `${item.year}-03-03` })), totalAvailable: corpus.length }
      },
    }
  }
  const dateRange: DateRange = { from: "2024-01-01", to: "2025-12-31" }

  // 单年内条目超过 MAX_LIMIT 时仍留缺口：cap 命中为真、policy 记 year_sliced，缺口进账而不是假装跑全。
  const residual = await executeSearch({ queries: ["q"], providers: ["pubmed"], limit: 20, dateRange }, { runtime, adapters: { pubmed: exact(35, 10) } })
  const residualCell = residual.queryPlan.queryStatuses[0]
  assert.equal(residualCell.hitCount, 40)
  assert.equal(residualCell.totalAvailable, 45)
  assert.equal(residualCell.capHit, true)
  assert.equal(residualCell.capPolicy, "year_sliced")
  assert.equal(residualCell.attempts, 4, "plan limit + raised limit + two year slices")
  assert.deepEqual(requests.map((item) => item.limit), [20, 30, 30, 30])
  assert.deepEqual(requests.map((item) => item.dateRange?.from), ["2024-01-01", "2024-01-01", "2024-01-01", "2025-01-01"], "slices walk the window oldest-first")

  // 切片跑全时 cap 不再是截断：读数由补偿后的实返与总数复算。
  requests.length = 0
  const complete = await executeSearch({ queries: ["q"], providers: ["pubmed"], limit: 20, dateRange }, { runtime, adapters: { pubmed: exact(20, 16) } })
  const completeCell = complete.queryPlan.queryStatuses[0]
  assert.equal(completeCell.hitCount, 36)
  assert.equal(completeCell.totalAvailable, 36)
  assert.equal(completeCell.capHit, false)
  assert.equal(completeCell.capPolicy, "none")

  // 召回形源只记总数：不抬 limit、不切片，一次请求。
  let recallCalls = 0
  const recallAdapter: ProviderAdapter = { id: "openalex", search: async () => { recallCalls += 1; return { candidates: [paper()], totalAvailable: 5241 } } }
  const recall = await executeSearch({ queries: ["q"], providers: ["openalex"], limit: 20, dateRange }, { runtime, adapters: { openalex: recallAdapter } })
  const recallCell = recall.queryPlan.queryStatuses[0]
  assert.equal(recallCell.hitCount, 1)
  assert.equal(recallCell.totalAvailable, 5241)
  assert.equal(recallCell.capHit, true)
  assert.equal(recallCell.capPolicy, "recorded_only")
  assert.equal(recallCell.attempts, 1)
  assert.equal(recallCalls, 1)

  // 无窗格的精确集源没有切片轴：抬 limit 到上限后剩余缺口进账。
  requests.length = 0
  const unwindowed = await executeSearch({ queries: ["q"], providers: ["pubmed"], limit: 20 }, { runtime, adapters: { pubmed: exact(35, 10) } })
  const unwindowedCell = unwindowed.queryPlan.queryStatuses[0]
  assert.equal(unwindowedCell.hitCount, 30)
  assert.equal(unwindowedCell.capHit, true)
  assert.equal(unwindowedCell.capPolicy, "raised_limit")
  assert.deepEqual(requests.map((item) => item.limit), [20, 30])
})

test("a failed cap compensation keeps the candidates already fetched", async () => {
  let calls = 0
  const adapter: ProviderAdapter = {
    id: "pubmed",
    sourceType: "exact_set",
    search: async (input: ProviderSearchInput) => {
      calls += 1
      if (calls === 1) return { candidates: Array.from({ length: input.limit }, (_, index) => paper({ retrievalProvider: "pubmed", externalSource: "pubmed", externalId: String(39000000 + index), doi: null })), totalAvailable: 36 }
      throw transientError(400, "invalid_request")
    },
  }
  const response = await executeSearch({ queries: ["q"], providers: ["pubmed"], limit: 20 }, { runtime, adapters: { pubmed: adapter } })
  const cell = response.queryPlan.queryStatuses[0]
  assert.equal(response.results.length, 20, "the first fetch must survive a failed compensation")
  assert.equal(cell.status, "success")
  assert.equal(cell.hitCount, 20)
  assert.equal(cell.totalAvailable, 36)
  assert.equal(cell.capHit, true)
  assert.equal(cell.capPolicy, "raised_limit")
  assert.equal(cell.attempts, 2)
  assert.ok(response.diagnostics.some((item) => item.stage === "provider" && item.message.includes("cap compensation failed")))
})

test("a single-year window is not sliced to no effect", async () => {
  const requests: Array<{ limit: number; from: string | null }> = []
  const adapter: ProviderAdapter = {
    id: "pubmed",
    sourceType: "exact_set",
    search: async (input: ProviderSearchInput) => {
      requests.push({ limit: input.limit, from: input.dateRange?.from ?? null })
      const corpus = Array.from({ length: 35 }, (_, index) => ({ id: String(39000000 + index), year: 2024 }))
      const inRange = corpus.filter((item) => !input.dateRange || item.year >= Number(input.dateRange.from.slice(0, 4)))
      return { candidates: inRange.slice(0, input.limit).map((item) => paper({ retrievalProvider: "pubmed", externalSource: "pubmed", externalId: item.id, doi: null, publishedDate: `${item.year}-03-03` })), totalAvailable: corpus.length }
    },
  }
  const response = await executeSearch({ queries: ["q"], providers: ["pubmed"], limit: 20, dateRange: { from: "2024-01-01", to: "2024-12-31" } }, { runtime, adapters: { pubmed: adapter } })
  const cell = response.queryPlan.queryStatuses[0]
  assert.equal(cell.hitCount, 30)
  assert.equal(cell.capHit, true)
  assert.equal(cell.capPolicy, "raised_limit", "a slice of the whole window would be a no-op")
  assert.deepEqual(requests.map((item) => item.limit), [20, 30])
})

test("windowed cells keep undated candidates with a mark, drop out-of-window ones, and count both", async () => {
  const adapter = fixed([paper({ externalId: "W-in", doi: "10.1234/in", publishedDate: "2024-05-05" }), paper({ externalId: "W-unknown", doi: "10.1234/unknown", publishedDate: null }), paper({ externalId: "W-old", doi: "10.1234/old", publishedDate: "2019-01-01" })], null)
  const response = await executeSearch({ queries: ["q"], providers: ["openalex"], dateRange: { from: "2024-01-01", to: "2024-12-31" } }, { runtime, adapters: { openalex: adapter } })
  const cell = response.queryPlan.queryStatuses[0]
  assert.equal(response.results.length, 2, "unknown dates are kept, out-of-window ones are dropped")
  assert.equal(cell.hitCount, 3)
  assert.equal(cell.dateFilteredCount, 1)
  assert.equal(cell.dateUnknownCount, 1)
  assert.equal(cell.dateRejectedCount, 1)
  assert.equal(response.queryPlan.dateRejectedCandidateCount, 1)
  assert.equal(response.results.find((item) => item.externalId === "W-in")?.sourceMetadata.windowStatus, "in_window")
  assert.equal(response.results.find((item) => item.externalId === "W-unknown")?.sourceMetadata.windowStatus, "date_unknown")
  assert.equal(response.results.some((item) => item.externalId === "W-old"), false)

  const unwindowed = await executeSearch({ queries: ["q"], providers: ["openalex"] }, { runtime, adapters: { openalex: adapter } })
  const unwindowedCell = unwindowed.queryPlan.queryStatuses[0]
  assert.equal(unwindowed.results.every((item) => item.sourceMetadata.windowStatus === "unfiltered"), true)
  assert.equal(unwindowedCell.dateFilteredCount, 3)
  assert.equal(unwindowedCell.dateUnknownCount, 0)
  assert.equal(unwindowedCell.dateRejectedCount, 0)
})

test("a cluster keeps date_unknown only when every member lacks a usable date", async () => {
  const shared = { externalId: null, doi: "10.1234/shared", title: "Shared Paper", citationCount: 0 }
  const dated = fixed([paper({ ...shared, externalSource: "pubmed", retrievalProvider: "pubmed", publishedDate: "2024-07-01" })], null)
  const undated = fixed([paper({ ...shared, publishedDate: null })], null)
  const window = { from: "2024-01-01", to: "2024-12-31" }

  const mixed = await executeSearch({ queries: ["q"], providers: ["openalex", "pubmed"], dateRange: window }, { runtime, adapters: { openalex: undated, pubmed: dated } })
  assert.equal(mixed.results.length, 1)
  assert.equal(mixed.results[0]?.sourceMetadata.windowStatus, "in_window", "a dated in-window member settles the cluster")

  const unknownOnly = await executeSearch({ queries: ["q"], providers: ["openalex", "pubmed"], dateRange: window }, { runtime, adapters: { openalex: undated, pubmed: undated } })
  assert.equal(unknownOnly.results.length, 1)
  assert.equal(unknownOnly.results[0]?.sourceMetadata.windowStatus, "date_unknown", "the mark survives for downstream screening")
})

test("search cells cache the receipt payload and old-shape cells fall back to a miss", async () => {
  const root = await mkdtemp(join(tmpdir(), "jadense-hardening-"))
  const cache = createFileCache(root)
  const key = searchCellKey({ provider: "openalex", query: "q", limit: 20, dateRange: null, offline: false })
  const adapter = fixed([paper({ externalId: "W1", doi: "10.1234/w1" })], 36)

  await cache.write("search", key, [paper({ externalId: "legacy", doi: "10.1234/legacy" })], new Date().toISOString())
  const fresh = await executeSearch({ queries: ["q"], providers: ["openalex"], limit: 20 }, { runtime, adapters: { openalex: adapter }, cache })
  assert.equal(fresh.results[0]?.externalId, "W1", "the old shape must not be served")
  assert.equal(fresh.queryPlan.queryStatuses[0].cacheHit, false)
  const stored = (await cache.read("search", key, isSearchCellPayload))?.value
  assert.deepEqual(stored, { candidates: [paper({ externalId: "W1", doi: "10.1234/w1" })], totalAvailable: 36 })

  const replay = await executeSearch({ queries: ["q"], providers: ["openalex"], limit: 20 }, { runtime, adapters: { openalex: adapter }, cache })
  const cell = replay.queryPlan.queryStatuses[0]
  assert.equal(replay.results[0]?.externalId, "W1")
  assert.equal(cell.cacheHit, true)
  assert.equal(cell.hitCount, 1)
  assert.equal(cell.totalAvailable, 36)
  assert.equal(cell.capHit, true)
  assert.equal(cell.attempts, 0)
  assert.equal(cell.retried, false)
  assert.equal(cell.recoveredByRetry, false)
  assert.equal(cell.collectedAt, fresh.queryPlan.queryStatuses[0].collectedAt)

  const windowedKey = searchCellKey({ provider: "openalex", query: "q", limit: 20, dateRange: { from: "2024-01-01", to: "2024-12-31" }, offline: false })
  await cache.write("search", windowedKey, { candidates: [paper({ externalId: "W-in", doi: "10.1234/in", publishedDate: "2024-05-05" }), paper({ externalId: "W-unknown", doi: "10.1234/unknown", publishedDate: null }), paper({ externalId: "W-old", doi: "10.1234/old", publishedDate: "2019-01-01" })], totalAvailable: 9 }, new Date().toISOString())
  const fromCache = await executeSearch({ queries: ["q"], providers: ["openalex"], limit: 20, dateRange: { from: "2024-01-01", to: "2024-12-31" } }, { runtime, adapters: { openalex: adapter }, cache })
  const cachedCell = fromCache.queryPlan.queryStatuses[0]
  assert.equal(cachedCell.cacheHit, true)
  assert.equal(cachedCell.hitCount, 3)
  assert.equal(cachedCell.dateFilteredCount, 1)
  assert.equal(cachedCell.dateUnknownCount, 1)
  assert.equal(cachedCell.dateRejectedCount, 1)
})
