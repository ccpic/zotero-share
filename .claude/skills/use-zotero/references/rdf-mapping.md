# rdf-mapping — RDF→API 字段映射（本次验证集）

RDF 回灌分支必读。映射边界按本次验证集写死，不要当通用真理外推。

## 条目

- `bib:Article` → `journalArticle`：`title←dc:title`、
  `abstractNote←dcterms:abstract`、`date←dc:date`、`pages←bib:pages`、
  `shortTitle←z:shortTitle`、`language←z:language`、
  `libraryCatalog←z:libraryCatalog`、`rights←dc:rights`、`extra←dc:description`。
- 期刊：`publicationTitle←dc:title`、
  `journalAbbreviation←dcterms:alternative`、`volume←prism:volume`、
  `issue←prism:number`；`DOI/ISSN` 从 `dc:identifier` 的 `DOI …/ISSN …`
  前缀剥离。期刊节点优先解顶层 `bib:Journal` 的 `urn:issn` 引用，
  找不到再用嵌套 `isPartOf/bib:Journal`。
- `url` 取 `dc:identifier/dcterms:URI/rdf:value`，无则 about 是 http
  才回退为 about（本集 identifier 全是 URL 形态，无纯 DOI 字符串）。
- `bib:Document`（`z:itemType=webpage`）→ `webpage`：title、url、
  accessDate（取 dateSubmitted）。
- 空值一律丢弃后再发（`clean`）。

## 作者与标签

- 作者：`bib:authors→author`、`bib:editors→editor`，
  `firstName←foaf:givenName / lastName←foaf:surname`。
- 标签：纯文本 `dc:subject` → 普通 tag；`z:AutomaticTag` 子节点 →
  `{"tag": v, "type": 1}`（含 PubMed MeSH 与 `/unread`，按原 type 保留）。

## 父子与分类

- 被父条目 `link:link` 引用的附件是 child，否则是独立附件；
  只有引用父条目的 about 才算 child，collection 直指附件的不算（本集为 0）。
- `z:Collection`：`name←dc:title`，`#collection_` 开头的是子分类其余是成员；
  按根分类拓扑排序建，成员中是父条目的建条目时带分类，
  是独立附件的上传时带分类。
