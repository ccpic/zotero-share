"""导入后归置：建缺失分类 → 设 collections → 转挂附件 → 删旧快照 → 回查落点。

When: 补录正式条目后，条目落在“未分类条目”，或快照类旧条目需被正式条目替换。
Do: python place_imports.py --map mapping.json [--dry-run]
Why: 手工逐条点收藏集易漏、易错类；统一走计划-执行-回查闭环可复核。
key 经参数或 ZOTERO_LOCAL_API_KEY 环境变量传入；无绝对路径与密钥落盘。
mapping.json 格式：
  {"placements": {itemKey: [collectionKey, ...]},
   "new_collections": [{"name": ..., "parentCollection": ...}],
   "reparents": {attachmentKey: newParentKey},
   "delete": [oldSnapshotKey, ...]}
退出码 0=计划与实际全对齐；1=有失败。
只读核验：--dry-run 只读库并报告计划（目标分类存在性、条目/附件当前归属），不写库。
读快照只省读、不省确认（写库确认门与写路径不变）：
- dry-run 读到的收藏集快照与 placements 逐条当前归属存 skill 包内 .cache/（--cache-dir 须为被忽略的
  非交付路径；--no-cache 显式绕过）；紧接的写库轮以此省去重复读。
- 命中条件：读数在 READ_TTL_MINUTES 分钟内（自采集时刻，命中不重写、不顺延）＋写前轻量探针
  （库版本）与读数时刻相等；探针取不到版本时一律不命中。
- 任一库写入（本会话或他会话）都使库版本前进：本会话写库后快照即删，他会话写入由探针识别，均重读。
- reparents 与 delete 位不进快照（写前照旧逐条重验 version）；逐条回查读库口径不变。
留痕：每类读数一行 CACHE，记 source＝snapshot／snapshot+fetch／fetch 与命中读数或 miss 原因。
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from pyzotero import zotero as zmod

READ_TTL_MINUTES = 10
CACHE_SCOPE = "zotero"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"  # skill 包内忽略位


def load_plan(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def plan_sha256(path):
    """plan 文件字节哈希：快照主键——plan 一变即 miss 重读。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LibraryProbe:
    """写前轻量探针：一次 `items?limit=1` 取库版本（同库任一写入都使其前进）。

    取不到（传输异常或无该响应头）＝ None：调用方一律按 miss 处理、退回逐条读库。
    """

    def __init__(self, z):
        self.z = z
        self._value = None
        self._probed = False

    def version(self):
        if not self._probed:
            self._probed = True
            try:
                self._value = int(self.z.last_modified_version() or 0) or None
            except Exception as e:  # 探针故障＝不命中，绝不因此中断归置
                print(f"WARN library version probe failed: {type(e).__name__} {e}", file=sys.stderr)
        return self._value


class ReadSnapshot:
    """库读快照：dry-run 的只读结果供紧接的写库轮复用（O-08／O-13，只省读）。

    命中＝读数在 READ_TTL_MINUTES 内（自采集时刻）＋写前轻量探针的库版本与采集时刻相等；
    任一库写入（本会话或他会话）即 miss 重读。缓存故障只通报，不中断归置。
    """

    def __init__(self, cache_dir, name, kind, library_id, now):
        self.enabled = cache_dir is not None
        self.path = None if cache_dir is None else Path(cache_dir) / CACHE_SCOPE / name
        self.kind = kind
        self.library_id = str(library_id)
        self.now = now
        self.reason = None  # 未命中原因（留痕用）
        self.entry = None  # 命中的读数条目：库版本＋采集时刻＋payload
        self.payload = {}  # 本次运行实际使用的读数（命中即快照，否则重读）
        self.fresh = False  # 本次是否重读（只读轮回写快照；命中不重写、不顺延）

    def load(self):
        if not self.enabled:
            self.reason = "disabled"
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            collected = datetime.fromisoformat(raw["collected_at"])
            version = int(raw["library_version"])
            library = str(raw["library_id"])
            payload = raw["payload"]
        except FileNotFoundError:
            self.reason = "missing"
            return
        except (AttributeError, KeyError, OSError, TypeError, ValueError) as e:
            self.reason = "malformed"
            print(f"WARN read snapshot malformed: {self.path}: {e}", file=sys.stderr)
            return
        if library != self.library_id or version <= 0 or not isinstance(payload, dict):
            self.reason = "malformed"
            print(f"WARN read snapshot malformed: {self.path}: library or value shape mismatch",
                  file=sys.stderr)
            return
        if self.now - collected > timedelta(minutes=READ_TTL_MINUTES):
            self.reason = "expired"
            return
        self.entry = {"version": version, "collected_at": raw["collected_at"], "payload": payload}

    def confirm(self, probe):
        """写前轻量探针：库版本等才采用快照读数；取不到／不等＝miss 重读。"""
        if self.entry is None:
            return False
        version = probe()
        if version is None:
            self.reason = "no-probe"
        elif version != self.entry["version"]:
            self.reason = "probe-changed"
        else:
            self.payload = self.entry["payload"]
            return True
        self.entry = None
        return False

    def read_all(self, fetch):
        """整类读数（如全库收藏集）：命中即复用快照，否则 fetch() 重读并留待回写。"""
        if self.entry is not None:
            return self.payload
        data = fetch()
        if self.enabled:  # --no-cache：不落读数，照旧逐类重读
            self.payload = data
            self.fresh = True
        return data

    def read_one(self, key, fetch):
        """逐条读数（如 placements 当前归属）：命中（或本进程已读）即复用，否则 fetch(key) 重读。"""
        if key in self.payload:
            return self.payload[key]
        data = fetch(key)
        if self.enabled:
            self.payload[key] = data
            self.fresh = True
        return data

    def report(self):
        if self.entry is not None:
            source = "snapshot+fetch" if self.fresh else "snapshot"
            print(f"CACHE {self.kind} source={source} collected={self.entry['collected_at']} "
                  f"version={self.entry['version']}")
        else:
            print(f"CACHE {self.kind} source=fetch reason={self.reason or 'miss'}")

    def store(self, version):
        """只读轮回写快照：只在全 miss 重读后写（命中不重写、不顺延）；探针取不到版本则不写。"""
        if not self.enabled or self.entry is not None or not self.fresh:
            return
        if version is None:
            print(f"WARN read snapshot not stored: {self.kind}: library version probe unavailable",
                  file=sys.stderr)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            staging = self.path.with_name(self.path.name + ".tmp")
            staging.write_text(json.dumps(
                {"library_id": self.library_id, "library_version": version,
                 "collected_at": self.now.isoformat(timespec="seconds"), "payload": self.payload},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            staging.replace(self.path)
        except OSError as e:  # 缓存故障只通报，不中断归置
            print(f"WARN read snapshot write failed: {self.path}: {e}", file=sys.stderr)

    def drop(self):
        """写库即失效：删快照文件（后续运行重读）。"""
        if not self.enabled:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as e:
            print(f"WARN read snapshot drop failed: {self.path}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Zotero 导入后归置与落点报告")
    ap.add_argument("--map", required=True, help="归置计划 JSON 路径")
    ap.add_argument("--library-id", default="0", help="本地库固定传 0")
    ap.add_argument("--api-key", default=os.environ.get("ZOTERO_LOCAL_API_KEY"),
                    help="本地写 key，默认读 ZOTERO_LOCAL_API_KEY")
    ap.add_argument("--dry-run", action="store_true", help="只报告计划，不写库")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="库读快照目录（缺省＝skill 包内 .cache/；须为被忽略的非交付路径）")
    ap.add_argument("--no-cache", action="store_true",
                    help="显式绕过库读快照：不读不写（照旧逐条读库）")
    args = ap.parse_args()

    plan = load_plan(args.map)
    placements = plan.get("placements", {})
    new_cols = plan.get("new_collections", [])
    reparents = plan.get("reparents", {})
    delete = plan.get("delete", [])

    cache_dir = None if args.no_cache else Path(args.cache_dir)
    now = datetime.now().astimezone()
    z = zmod.Zotero(library_id=args.library_id, library_type="user", local=True,
                    local_api_key=args.api_key)
    probe = LibraryProbe(z)
    cols_snap = ReadSnapshot(cache_dir, f"collections-{args.library_id}.json", "collections",
                             args.library_id, now)
    plan_snap = ReadSnapshot(cache_dir, f"place-{args.library_id}-{plan_sha256(args.map)}.json",
                             "plan-read", args.library_id, now)
    cols_snap.load()
    plan_snap.load()
    # 探针固定在读数之前：探针值 ≤ 数据版本，回写后命中（库版本相等）即证明读数之后无写入；
    # 读数之后再取会把「读数与探针之间」的他会话写入吞成命中（数据已旧而版本号仍等）。
    if cache_dir is not None:
        probe.version()
    cols_snap.confirm(probe.version)
    plan_snap.confirm(probe.version)

    live_cols = cols_snap.read_all(lambda: {c["key"]: c["data"] for c in z.collections()})
    cols_snap.report()

    def read_placement(key):
        """placements 逐条当前归属：快照命中即省读；miss（含快照缺该键）逐条读库。"""
        return plan_snap.read_one(key, z.item)

    ok = True

    for nc in new_cols:
        parent = nc.get("parentCollection") or ""
        status = "OK  " if (not parent or parent in live_cols) else "BAD "
        if status == "BAD ":
            ok = False
        print(f"{'PLAN' if args.dry_run else status} new-collection "
              f"{nc['name']} parent={live_cols.get(parent, {}).get('name', '(ROOT)')}")
    for key, cols in placements.items():
        try:
            cur = read_placement(key)["data"]
        except Exception as e:
            ok = False
            print(f"FAIL {key} read: {type(e).__name__} {e}")
            continue
        missing = [c for c in cols if c not in live_cols]
        if missing:
            ok = False
        print(f"{'PLAN' if args.dry_run else 'DO  '} place {key} "
              f"{(cur.get('title') or '')[:50]} now={cur.get('collections')} "
              f"-> {cols} missing={missing or '-'}")
    for akey, parent in reparents.items():
        try:
            cur = z.item(akey)["data"]
        except Exception as e:
            ok = False
            print(f"FAIL {akey} read: {type(e).__name__} {e}")
            continue
        print(f"{'PLAN' if args.dry_run else 'DO  '} reparent {akey} "
              f"now={cur.get('parentItem')} -> {parent}")
    for key in delete:
        print(f"{'PLAN' if args.dry_run else 'DO  '} delete {key}")
    plan_snap.report()

    if args.dry_run:
        cols_snap.store(probe.version())  # 回写用读数前取的库版本（见 main 开头注释）
        plan_snap.store(probe.version())
        return 0 if ok else 1

    new_keys = {}
    try:
        if new_cols:
            r = z.create_collections(new_cols)
            for i, nc in enumerate(new_cols):
                k = (r.get("successful") or {}).get(str(i), {}).get("key")
                if not k:
                    print(f"FAIL new-collection {nc['name']}")
                    return 1
                new_keys[nc["name"]] = k
                print(f"OK   new-collection {nc['name']} key={k}")

        def resolve(cols):
            return [new_keys.get(c, c) for c in cols]

        for key, cols in placements.items():
            env = read_placement(key)
            env["data"]["collections"] = resolve(cols)
            z.update_items([env])
        for akey, parent in reparents.items():
            env = z.item(akey)
            env["data"]["parentItem"] = parent
            z.update_items([env])
        for key in delete:
            env = z.item(key)
            z.delete_item({"key": key, "version": env["data"]["version"]})
    finally:
        cols_snap.drop()  # 写库即失效
        plan_snap.drop()

    bad = 0
    for key, cols in placements.items():
        cur = z.item(key)["data"]
        want = resolve(cols)
        mark = "OK  " if cur.get("collections") == want else "FAIL"
        if mark == "FAIL":
            bad += 1
        names = [live_cols.get(c, {}).get("name", c) for c in want]
        print(f"{mark} {key} {(cur.get('title') or '')[:50]} -> {'/'.join(names)}")
    for akey, parent in reparents.items():
        cur = z.item(akey)["data"]
        mark = "OK  " if cur.get("parentItem") == parent else "FAIL"
        if mark == "FAIL":
            bad += 1
        print(f"{mark} attachment {akey} parent={cur.get('parentItem')}")
    for key in delete:
        try:
            z.item(key)
            print(f"FAIL delete {key} still exists")
            bad += 1
        except Exception:
            print(f"OK   delete {key} gone")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
