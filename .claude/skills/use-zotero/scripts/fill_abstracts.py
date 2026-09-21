# -*- coding: utf-8 -*-
"""既有条目仅空补齐摘要（ADR-0021）：计划 → 一次确认 → 写 → 回查（与归置／挂载同纪律）。

When: 库内既有条目（早前入库未带摘要，或由人工／其它入口先行入库）需要按其 DOI 补 `abstractNote` 时。
Do:   uv run --isolated --no-project --with pyzotero python fill_abstracts.py \
          --dois 10.1056/NEJMoa1913805 [...] [--abstracts-file run/b1/ingest-abstracts.json] \
          [--no-fetch] [--dry-run]
      先跑 --dry-run 出计划表，确认后再跑一次（不带 --dry-run）写库，行形如：
        PLAN <doi> key=<key> current=empty action=fill        计划轮：空条目且文种可摘要，拟补
        PLAN <doi> key=<key> current=empty action=na          计划轮：空条目但文种不适用（写入轮零 PATCH）
        PLAN <doi> key=<key> current=nonempty action=skip     计划轮：非空跳过（零写入）
        PLAN <doi> current=missing action=skip                计划轮：库内无该 DOI
        SKIP missing <doi>                                    库内无该 DOI（计入已处理，不算失败）
        SKIP nonempty <doi> key=<key>                         已有摘要：永不覆盖、零写入
        RESOLVE <doi> source=table|crossref|pubmed|openalex|none status=filled|empty|na|no-identifier
        PATCH <doi> key=<key>                                 仅 status=filled 才写
        VERIFY <doi> abstract=ok|empty|na                     写后回查（唯一读数）
        OK filled=N skipped-nonempty=N missing=N empty=N       计数行，四数之和＝输入条数
Why:  摘要的落点只有一个字段 `abstractNote`；既有条目全量覆盖会盖掉用户手工内容，一律不动又修不掉
      历史缺口——故只补空、非空永不覆盖，并以写后回查为准。取数与清洗不在此另写一份：走共享模块
      `abstracts`（与 `seed_from_doi.py` 同一份梯级与清洗口径，ADR-0021 单源）。
判定（ADR-0021）：
- 主语是「库内承载该 DOI 的条目」，**不分文种**：文种只决定取数适用性（`abstracts.applicability`），
  报告型条目记 `na`（不适用）而不是「库内没有」。同族 `dedup_gate.find_by_doi` 只把 journalArticle 认作
  正式条目——那是建库路径（ADR-0020 预检）的裁决，本脚本不据它判在不在库。
- 命中且 `abstractNote` 非空 → `SKIP nonempty` 且零写入；命中且为空 → 按「入库摘要表 → Crossref →
  PubMed → OpenAlex」取数（`abstracts.resolve`），**只有 `filled` 才写**；`empty`／`na`／`no-identifier`
  一律不写占位文本，字段原样留空、状态如实留痕（`na`＝文种不适用，不算缺口）。
- 幂等可重跑：本轮补过的条目再跑即 `SKIP nonempty`；**仍为空的条目再跑会重试取数**
  （缺口非阻断，上游可能后来才 deposit 摘要）。
- 未取到的原因分两层留痕：`status=` 给状态类（`empty`＝表未命中且各级无正文或取数失败／`na`＝文种
  不适用／`no-identifier`＝无标识）；失败层级与异常类由共享模块逐级打 `WARN abstract fetch failed
  source=<层级> id=<标识>: <异常类>: <消息>` 到 stderr（不阻断梯级，继续降级）。
- `--abstracts-file` 传 Step2 的「入库摘要表」（不传＝不启用表，手工调用行为不变）；`--no-fetch` 显式
  关联网补漏（表照读——那不是本次请求；联网级一律不发）。表读错 fail-closed 中止（静默降级成空表会
  伪装成「全库无摘要」）。
- 并发面：写前取最新信封，并**先重判信封里的 `abstractNote`**——取数（表／Crossref／PubMed／OpenAlex，
  每级 30s 超时）与那次读之间隔着数秒到数十秒，他人（桌面端／他会话）在这段窗口里填上的摘要必须原样留住：
  非空即打 `SKIP nonempty <doi> key=<key>`、计入 `skipped-nonempty`、**零写入**并接着下一条（判定读的是刚
  取回的信封，不是取数之前那次检索的快照）。信封自带 `version` 仍作 PATCH 的并发前置条件
  （`If-Unmodified-Since-Version`），但它带的是**载荷自己的 version**，只护得住「重判之后、写入之前」
  这一小段——上面那次重判才是窗口内的主防线；写后仍以回查为准。
- 计划轮只读：`--dry-run` 不取数、不写库，只回查库内状态并出计划（网络与取数都留到写库轮）。
- 计数面：每条 DOI 只进一个计数；`empty` ＝本跑未写入且库内字段仍为空（含 `na` 与 `no-identifier`——
  票面计数行没有 `na` 位，这两类的如实通报在 RESOLVE／VERIFY 行）；计划轮的 `filled` ＝**拟补条数**
  （只计 `action=fill`；`action=na` 的条目计划面也写明不写、进 `empty` 侧——同一类条目在两轮落同一侧，
  票 09 的「计划与写入逐条一致」才逐行对得上），写库轮 ＝回查判定 `ok` 的条数（唯一读数）。
退出码 0=全处理完（含空、跳过与 `na`——缺口非阻断）；1=有失败（读库／写库／回查异常）或摘要表异常（fail-closed）。
计数行恒为票面固定的 `OK …` 形态；真失败以 stderr 的 `FAIL …` 行与该退出码通报（计数行不因此改形）。
key 经参数或 ZOTERO_LOCAL_API_KEY 环境变量传入；无绝对路径与密钥落盘。
"""
import argparse
import os
import sys
from pathlib import Path

from pyzotero import zotero as zmod

sys.path.insert(0, str(Path(__file__).resolve().parent))
import abstracts  # noqa: E402 摘要取数与清洗共享模块（ADR-0021；同包脚本，与 dedup_gate 同式）
from dedup_gate import doi_probes, norm_doi, search_hits  # noqa: E402 同族脚本跨件复用（DOI 归一与检索单源）


def find_library_item(z, doi):
    """读库内承载该 DOI 的条目（不分文种）：DOI 探针（整串＋后缀）命中集内比对条目 DOI 字段。

    `dedup_gate.find_by_doi` 只把 journalArticle 认作「正式条目」——那是建库路径（ADR-0020 预检）的裁决；
    本脚本的主语是「库里已有这条」，文种只是取数适用性的输入，报告型条目该记 `na` 而不是「库里没有」。
    故此处只复用它三件原语（`norm_doi`／`doi_probes`／`search_hits`），判定键仍是条目自身的 DOI 字段
    （归一后逐字相等）。命中多条时取 key 稳定序首个；空 DOI 一律不命中（无从比对）。
    异常向上抛，由调用方记 FAIL 并按退出码语义处理。
    """
    want = norm_doi(doi)
    if not want:
        return None
    hits = []
    for probe in doi_probes(want):
        hits = [hit for hit in search_hits(z, probe, modes=("everything",))
                if norm_doi((hit.get("data") or {}).get("DOI") or "") == want]
        if hits:  # 命中即停：后缀探针只为捞回整串检索漏掉的那条，已有命中就不再问（同 find_by_doi）
            break
    return sorted(hits, key=lambda h: ((h.get("data") or {}).get("key") or ""))[0] if hits else None


def readback_abstract(item):
    """写后回查判定：`ok`／`empty`／`na`（读的是库内那条，不看本进程的预期）。

    与 `seed_from_doi.readback_abstract` 同一词表与同一口径（VERIFY 行＝ADR-0021 的唯一读数）；
    「哪些文种可摘要」仍只由共享模块 `abstracts.applicability` 判（单源），na 由**库内条目的
    itemType** 判，不由本进程的期望判。
    """
    data = (item or {}).get("data") or {}
    if not abstracts.applicability(data.get("itemType")):
        return "na"
    return "ok" if (data.get("abstractNote") or "").strip() else "empty"


def fill(z, dois, table, allow_fetch, dry_run) -> int:
    """逐条只读回查 → 取数 → 仅空写入 → 回查；返回退出码。"""
    ok = True
    filled = skipped = missing = empty = 0
    for doi in dois:
        try:
            item = find_library_item(z, doi)
        except Exception as e:  # 读库异常＝真失败（fail-loud：不冒充「库内没有」）
            ok = False
            empty += 1  # 字段未被写入，计入「仍为空」侧
            print(f"FAIL {doi} lookup: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if item is None:
            missing += 1
            print(f"PLAN {doi} current=missing action=skip" if dry_run else f"SKIP missing {doi}")
            continue
        data = item.get("data") or {}
        key = data.get("key")
        if (data.get("abstractNote") or "").strip():  # 非空永不覆盖（ADR-0021）：这一条零写入
            skipped += 1
            print(f"PLAN {doi} key={key} current=nonempty action=skip" if dry_run
                  else f"SKIP nonempty {doi} key={key}")
            continue
        if dry_run:  # 计划轮只读：取数（网络）与写入都留到确认后的写库轮
            # 拟动作按文种适用性如实打（单源仍是 `abstracts.applicability`，本脚本不另立一份）：文种不
            # 适用的条目，写库轮 `resolve` 会短路成 `na`、一个 PATCH 都不发——计划面就不该自称 `fill`
            if abstracts.applicability(data.get("itemType")):
                filled += 1  # 计划轮的 filled ＝拟补条数，只计 action=fill
                print(f"PLAN {doi} key={key} current=empty action=fill")
            else:
                empty += 1  # 与写库轮同侧：本跑不写、字段照旧留空
                print(f"PLAN {doi} key={key} current=empty action=na")
            continue
        text, source, status = abstracts.resolve(doi, None, data.get("itemType"), table, allow_fetch)
        print(f"RESOLVE {doi} source={source} status={status}")
        if status == "filled":  # 只有取到正文才写；empty／na／no-identifier 一律不写占位文本
            try:
                env = z.item(key)  # 写前取最新信封：只改 abstractNote，不回退别人的改动
                if ((env.get("data") or {}).get("abstractNote") or "").strip():
                    # 取数窗口内他人（桌面端／他会话）已填：以刚取回的信封为准——原样留住、零写入，接着
                    # 下一条。只靠信封 version 护不住这一段：pyzotero 的 `If-Unmodified-Since-Version`
                    # 带的是载荷自己的 version（早于取数），取数期间被填上的摘要正是本处要防的那个窗口。
                    skipped += 1
                    print(f"SKIP nonempty {doi} key={key}")
                    continue
                env["data"]["abstractNote"] = text
                # 单条目单字段即走逐条 PATCH（pyzotero `update_item`：PATCH /items/<key>）；信封自带
                # version 作并发前置条件（If-Unmodified-Since-Version）。place_imports 的 `update_items`
                # 是批量 POST，用在一次改多条的场合，不用于本脚本的单条写
                z.update_item(env)
                print(f"PATCH {doi} key={key}")
            except Exception as e:
                ok = False
                print(f"FAIL {doi} patch: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            verdict = readback_abstract(z.item(key))  # 写后回查＝唯一读数，不以写入返回为准
        except Exception as e:
            ok = False
            verdict = "?"
            print(f"FAIL {doi} verify: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"VERIFY {doi} abstract={verdict}")
        if verdict == "ok":
            filled += 1
        else:
            empty += 1
            if status == "filled":  # 判 filled 却没在库里读回非空＝真失败
                ok = False
    print(f"OK filled={filled} skipped-nonempty={skipped} missing={missing} empty={empty}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="给库内既有条目仅空补齐摘要（ADR-0021）")
    ap.add_argument("--dois", nargs="*", default=[], help="DOI 清单")
    ap.add_argument("--dois-file", help="每行一个 DOI 的文本文件（空行与 # 注释跳过）")
    ap.add_argument("--library-id", default="0", help="本地库固定传 0")
    ap.add_argument("--api-key", default=os.environ.get("ZOTERO_LOCAL_API_KEY"),
                    help="本地写 key，默认读 ZOTERO_LOCAL_API_KEY")
    ap.add_argument("--dry-run", action="store_true", help="只读回查并出计划表，不取数、不写库")
    ap.add_argument("--abstracts-file", help="「入库摘要表」路径（Step2 产物；不传即不启用表）")
    ap.add_argument("--no-fetch", action="store_true",
                    help="显式关联网补漏：摘要只从入库摘要表来（表未命中即留空并如实记 empty）")
    args = ap.parse_args()

    dois = list(args.dois)
    if args.dois_file:
        dois += [line.strip() for line in open(args.dois_file, encoding="utf-8")
                 if line.strip() and not line.startswith("#")]
    dois = list(dict.fromkeys(dois))
    if not dois:
        print("no DOIs given", file=sys.stderr)
        return 1

    try:  # 表读错必须炸（ADR-0021）：静默降级成空表会伪装成「全库无摘要」
        # 只取 load_table 首返回值（归一 DOI → 记录）；无摘要清单属 Step2 覆盖率面，取数梯级用不到
        table = abstracts.load_table(args.abstracts_file)[0] if args.abstracts_file else None
    except Exception as e:
        print(f"ABSTRACT-TABLE-FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    z = zmod.Zotero(library_id=args.library_id, library_type="user", local=True,
                    local_api_key=args.api_key)
    return fill(z, dois, table, not args.no_fetch, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
