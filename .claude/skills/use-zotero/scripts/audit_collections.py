"""Zotero 分类父级对账：期望父 vs 本地实际父。

When: 外部源（RDF 等）回灌分类后，收尾对账。
Do: python audit_collections.py --map mapping.json [--library-id 0]
Why: 建分类时父 key 未落定会静默落根，计数对了层级也可能错；
     逐条比期望父能抓出这类错位（曾一次抓出 33 个）。
mapping.json 格式：
  {"collections": {oldId: newKey},
   "parents": {oldId: oldParentId | null},
   "names": {oldId: name}}
只读，不写库。退出码 0=全对齐；1=有错位/缺失/多余。
"""
import argparse
import json
import sys

from pyzotero import zotero as zmod


def main() -> int:
    ap = argparse.ArgumentParser(description="审计 Zotero 分类父级是否与期望一致")
    ap.add_argument("--map", required=True, help="期望映射 JSON 路径")
    ap.add_argument("--library-id", default="0", help="本地库固定传 0")
    args = ap.parse_args()

    mapping = json.loads(open(args.map, encoding="utf-8").read())
    old2new = mapping["collections"]
    exp_parent = mapping["parents"]
    names = mapping.get("names", {})
    new2old = {v: k for k, v in old2new.items()}

    z = zmod.Zotero(library_id=args.library_id, library_type="user", local=True)
    live_by_key = {c["key"]: c["data"] for c in z.collections()}

    bad = []
    ok = 0
    for old, new in sorted(old2new.items()):
        d = live_by_key.get(new)
        if d is None:
            bad.append(("MISSING", old, names.get(old, old), "-", "-"))
            continue
        exp_old = exp_parent.get(old)
        exp_new = old2new[exp_old] if exp_old else ""
        act = d.get("parentCollection") or ""
        if act != exp_new:
            exp_name = names.get(exp_old, "(ROOT)") if exp_old else "(ROOT)"
            act_old = new2old.get(act)
            act_name = names.get(act_old, "(ROOT)") if act_old else "(ROOT)"
            bad.append(("MISPLACED", old, names.get(old, old), exp_name, act_name))
        else:
            ok += 1
    for k in sorted(set(live_by_key) - set(old2new.values())):
        bad.append(("EXTRA", "-", live_by_key[k]["name"], "-", "-"))

    print(f"OK={ok} BAD={len(bad)} (live={len(live_by_key)} expect={len(old2new)})")
    for kind, old, name, exp, act in bad:
        print(f"  {kind:9s} {name} old={old} expectParent={exp} actualParent={act}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
