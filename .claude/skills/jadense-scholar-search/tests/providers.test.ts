import assert from "node:assert/strict"
import test from "node:test"
import { buildArxivSearchUrl, searchArxiv } from "../src/providers/arxiv.js"
import { buildOpenAlexSearchUrl, searchOpenAlex } from "../src/providers/openalex.js"
import { buildCrossrefSearchUrl, searchCrossref } from "../src/providers/crossref.js"
import { buildPubmedSearchUrl, searchPubmed } from "../src/providers/pubmed.js"
import { buildSemanticScholarSearchUrl, searchSemanticScholar } from "../src/providers/semantic-scholar.js"
import { buildGoogleScholarSearchUrl, mapGoogleScholarResult, searchGoogleScholar, SerpApiRequestError } from "../src/providers/google-scholar.js"
import type { SearchRuntimeConfig } from "../src/types.js"

function jsonFetch(payload: unknown, status = 200, calls: string[] = []) {
  return (input: string | URL, init?: RequestInit) => { calls.push(`${String(input)} ${init?.headers ? JSON.stringify(init.headers) : ""}`); return Promise.resolve(new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } })) }
}
const base = (fetchImpl?: typeof fetch, credentials?: SearchRuntimeConfig["credentials"]): SearchRuntimeConfig => ({ userAgent: "fixture-agent/1.0", contactEmail: "fixture@example.org", fetch: fetchImpl, credentials })

test("provider request builders include explicit date and contact parameters", () => {
  const input = { query: "neural retrieval", limit: 10, dateRange: { from: "2020-01-01", to: "2024-12-31" }, runtime: base(undefined, { serpApiKey: "fixture-key" }) }
  assert.match(buildArxivSearchUrl(input), /submittedDate/)
  assert.match(buildOpenAlexSearchUrl(input), /from_publication_date/)
  assert.match(buildCrossrefSearchUrl(input), /from-pub-date/)
  assert.match(buildPubmedSearchUrl(input), /mindate=2020%2F01%2F01/)
  assert.match(buildSemanticScholarSearchUrl(input), /publicationDateOrYear=2020-01-01%3A2024-12-31/)
  assert.match(buildGoogleScholarSearchUrl(input), /engine=google_scholar/)
  assert.match(buildGoogleScholarSearchUrl(input), /api_key=fixture-key/)
})

test("arXiv XML parser preserves title, author, date, and DOI", async () => {
  const xml = `<feed xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"><opensearch:totalResults>410</opensearch:totalResults><entry><id>https://arxiv.org/abs/2401.00001v2</id><title>Example Paper</title><summary>Example abstract.</summary><published>2024-01-02T00:00:00Z</published><author><name>Ada Author</name></author><arxiv:doi>10.1234/example</arxiv:doi><link rel="alternate" href="https://arxiv.org/abs/2401.00001" /></entry></feed>`
  const result = await searchArxiv({ query: "example", limit: 1, runtime: base(async () => new Response(xml)) })
  assert.equal(result.candidates[0]?.externalId, "2401.00001")
  assert.equal(result.candidates[0]?.doi, "10.1234/example")
  assert.equal(result.candidates[0]?.publishedDate, "2024-01-02")
  assert.equal(result.totalAvailable, 410)
})

test("Crossref keeps partial date unknown and OpenAlex handles an empty response", async () => {
  const crossref = await searchCrossref({ query: "example", limit: 1, runtime: base(jsonFetch({ message: { items: [{ DOI: "10.1234/example", title: ["Example"], issued: { "date-parts": [[2024]] } }], "total-results": 351775 } })) })
  assert.equal(crossref.candidates[0]?.publishedDate, null)
  assert.equal(crossref.candidates[0]?.sourceMetadata.publicationYear, null)
  assert.equal(crossref.totalAvailable, 351775)
  const single = await searchCrossref({ query: "10.1234/example", limit: 1, runtime: base(jsonFetch({ message: { DOI: "10.1234/example", title: ["Example"] } })) })
  assert.equal(single.totalAvailable, null)
  const openalex = await searchOpenAlex({ query: "empty", limit: 5, runtime: base(jsonFetch({ results: [] })) })
  assert.deepEqual(openalex.candidates, [])
  assert.equal(openalex.totalAvailable, null)
  const counted = await searchOpenAlex({ query: "arni", limit: 20, runtime: base(jsonFetch({ meta: { count: 5241 }, results: [] })) })
  assert.equal(counted.totalAvailable, 5241)
})

test("PubMed performs ID then summary requests with date filters", async () => {
  const calls: string[] = []
  const fetchImpl = (input: string | URL) => {
    calls.push(String(input))
    if (String(input).includes("esearch.fcgi")) return Promise.resolve(new Response(JSON.stringify({ esearchresult: { count: "36", idlist: ["123"] } })))
    return Promise.resolve(new Response(JSON.stringify({ result: { "123": { uid: "123", title: "PubMed paper", pubdate: "2024 Jan", sortpubdate: "2024/01/02", articleids: [{ idtype: "pubmed", value: "123" }], authors: [] } } })))
  }
  const result = await searchPubmed({ query: "pubmed", limit: 1, dateRange: { from: "2020-01-01", to: "2024-12-31" }, runtime: base(fetchImpl) })
  assert.equal(result.candidates[0]?.externalId, "123")
  assert.equal(result.totalAvailable, 36)
  assert.equal(calls.length, 2)
  assert.match(calls[0], /mindate=2020%2F01%2F01/)
  assert.match(calls[1], /id=123/)
})

test("Semantic Scholar parser and Google Scholar pagination keep stable identity rules", async () => {
  const semantic = await searchSemanticScholar({ query: "semantic", limit: 1, runtime: base(jsonFetch({ total: 1200, data: [{ paperId: "paper-1", externalIds: { DOI: "10.1234/semantic" }, title: "Semantic", year: 2024, authors: [] }] })) })
  assert.equal(semantic.candidates[0]?.externalId, "paper-1")
  assert.equal(semantic.totalAvailable, 1200)
  const temporary = mapGoogleScholarResult({ title: "Temporary result", result_id: "result-1", publication_info: { summary: "A. Author - Journal - 2024" }, inline_links: { cited_by: { total: 4, cites_id: "cites-1" } } })
  assert.equal(temporary?.externalId, null)
  assert.equal((temporary?.sourceMetadata.googleScholar as Record<string, unknown>).resultId, "result-1")

  const starts: string[] = []
  const google = await searchGoogleScholar({ query: "google", limit: 11, runtime: base((input) => { starts.push(new URL(String(input)).searchParams.get("start") ?? ""); const start = starts.at(-1); const item = { title: `Google ${start}`, result_id: `r-${start}`, publication_info: { summary: "A. Author - Journal - 2024" }, inline_links: {} }; return Promise.resolve(new Response(JSON.stringify({ search_information: { total_results: 42 }, organic_results: [item], serpapi_pagination: start === "0" ? { next: "next" } : {} }))) }, { serpApiKey: "key" }) })
  assert.equal(google.candidates.length, 2)
  assert.equal(google.totalAvailable, 42)
  assert.deepEqual(starts, ["0", "10"])
})

test("Google Scholar classifies quota failures without exposing a key", async () => {
  await assert.rejects(() => searchGoogleScholar({ query: "google", limit: 1, runtime: base(async () => new Response("{}", { status: 429 }), { serpApiKey: "super-secret-key" }) }), (error: unknown) => error instanceof SerpApiRequestError && error.classification === "quota_exhausted" && !error.message.includes("super-secret-key"))
  await assert.rejects(() => searchGoogleScholar({ query: "google", limit: 1, runtime: base(async () => new Response(JSON.stringify({ error: "quota" }), { status: 200 }), { serpApiKey: "super-secret-key" }) }), (error: unknown) => error instanceof SerpApiRequestError && error.status === 200)
})

// 真钟例外（`ts-no-test-timers`）：这两条断言的就是源节流自己的墙钟间隔、串行度与并发上限，node:test 无常驻
// 假时钟，确定性时间控制等于绕开被测行为；故用 5ms 的短请求让「并发中」可见。同批真钟件＝`tests/hardening.test.ts`
// 的重退避用例（退避本身是被测行为）。两者合计约 2 秒。
test("same-source requests are serialized and spaced inside the source budget", async () => {
  const starts: number[] = []
  let inFlight = 0
  let maxInFlight = 0
  const fetchImpl = (input: string | URL) => {
    starts.push(Date.now())
    inFlight += 1
    maxInFlight = Math.max(maxInFlight, inFlight)
    const body = String(input).includes("esearch.fcgi")
      ? { esearchresult: { count: "1", idlist: ["123"] } }
      : { result: { "123": { uid: "123", title: "Budget paper", sortpubdate: "2024/01/02", articleids: [{ idtype: "pubmed", value: "123" }], authors: [] } } }
    return new Promise<Response>((resolve) => setTimeout(() => { inFlight -= 1; resolve(new Response(JSON.stringify(body))) }, 5))
  }
  await searchPubmed({ query: "budget probe one", limit: 1, runtime: base(fetchImpl) })
  await searchPubmed({ query: "budget probe two", limit: 1, runtime: base(fetchImpl) })

  assert.equal(starts.length, 4)
  assert.equal(maxInFlight, 1, "PubMed must stay serial")
  // 预算是速率（≤3 rps）而非逐间隔硬下限：起始时刻是预占的，定时器迟到会让单个间隔略短于 334ms。
  const busiest = Math.max(...starts.map((at) => starts.filter((other) => other >= at && other < at + 1000).length))
  assert.ok(busiest <= 3, `any second may hold at most 3 starts, saw ${busiest} (starts at ${starts.map((at) => at - starts[0]).join(", ")}ms)`)
})

test("multi-slot sources keep their concurrency and start budget", async () => {
  const starts: number[] = []
  let inFlight = 0
  let maxInFlight = 0
  const fetchImpl = () => {
    starts.push(Date.now())
    inFlight += 1
    maxInFlight = Math.max(maxInFlight, inFlight)
    return new Promise<Response>((resolve) => setTimeout(() => { inFlight -= 1; resolve(new Response(JSON.stringify({ meta: { count: 0 }, results: [] }))) }, 5))
  }
  await Promise.all(Array.from({ length: 6 }, (_, index) => searchOpenAlex({ query: `budget probe ${index}`, limit: 3, runtime: base(fetchImpl) })))

  assert.equal(starts.length, 6)
  assert.ok(maxInFlight <= 4, `OpenAlex allows 4 concurrent requests, saw ${maxInFlight}`)
  const elapsed = starts.at(-1)! - starts[0]
  assert.ok(elapsed >= 400, `6 requests at 10 rps must span ≥500ms, saw ${elapsed}ms (a burst would finish in ~10ms)`)
})
