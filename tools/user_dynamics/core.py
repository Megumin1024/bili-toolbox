# -*- coding: utf-8 -*-
"""按 UID 抓取 B 站用户动态：解析、翻页、导出。

网络访问全部经注入的 fetch（默认走 core.session 四层风控通道，自动过全局闸门）。
本模块不自己建连接，因此翻页 / 解析 / 空结果重试都能离线测试。

两条来自实测的硬约束（依据见 core.wbi 与 core.transport 的模块注释）：

1. 动态接口必须带 WBI 签名，否则返回 code=0 但 items 为空——**静默清空，不报错**；
2. 即便带了签名，"空结果"仍会间歇出现，而且与"真的没有更多"的响应完全一致
   （都是 code=0 + items=[] + has_more=false）。所以空结果必须退避重试，
   且第一页重试耗尽时**不能**下结论说"该用户没有动态"——那可能是在撒谎。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from openpyxl.utils import get_column_letter

from core import wbi, xlsx as xlsx_mod
from core.cancel import TaskCancelledError, wait as cancel_wait

DYNAMIC_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

# 每页实测 12 条。仅用于给用户估算"这大概是多少条"。
PAGE_SIZE = 12
DEFAULT_MAX_PAGES = 5
MAX_PAGES_LIMIT = 100
DEFAULT_EMPTY_RETRIES = 3
# 页间业务间隔（秒）。动态接口在实测中比评论区更敏感，默认给得比评论抓取宽。
DEFAULT_SLEEP = 1.0
EMPTY_RETRY_BASE = 2.0
EMPTY_RETRY_CAP = 30.0

TYPE_LABELS = {
    "DYNAMIC_TYPE_AV": "视频投稿",
    "DYNAMIC_TYPE_DRAW": "图文",
    "DYNAMIC_TYPE_WORD": "纯文字",
    "DYNAMIC_TYPE_FORWARD": "转发",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_MUSIC": "音频",
}


class DynamicsUnavailable(RuntimeError):
    """第一页反复取空：无法区分"该用户没有动态"与"被限流降级"。"""


def parse_uid(text):
    """UID 纯数字，或 space.bilibili.com/<uid> 链接（含 /dynamic 等子路径）。"""
    s = (text or "").strip().strip("\"'“”")
    if not s:
        raise ValueError("请输入 UID 或用户空间链接")
    m = re.search(r"space\.bilibili\.com/(\d+)", s, re.I)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"\d{1,20}", s):
        return int(s)
    raise ValueError(f"无法识别的 UID：{s[:60]}")


# ---------- 解析（纯函数，可离线测试） ----------

def to_int(value, default=0):
    """B 站的计数/时间戳常以**字符串**下发，直接当整数用会炸。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _stat_count(module_stat, key):
    node = (module_stat or {}).get(key)
    return to_int(node.get("count")) if isinstance(node, dict) else 0


def _abs_url(url):
    """动态里的跳转链接是协议相对地址（//www.bilibili.com/...），补上 https:。"""
    u = str(url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    return u if u.startswith(("http://", "https://")) else ""


def normalize_type(type_raw):
    return TYPE_LABELS.get(str(type_raw or ""), str(type_raw or ""))


def parse_item(item):
    """一条动态 → 扁平行。字段缺失一律降级为空/0，绝不抛异常。"""
    item = item or {}
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    archive = major.get("archive") or {}
    opus = major.get("opus") or {}
    desc = (dynamic.get("desc") or {}).get("text") or ""
    orig = item.get("orig") or {}
    orig_author = ((orig.get("modules") or {}).get("module_author") or {})

    # 正文来源随类型而变：转发/纯文字在 desc，投稿在 archive.title，
    # 图文在 opus.summary.text。按优先级取第一个非空的。
    text = (desc or archive.get("title")
            or (opus.get("summary") or {}).get("text") or opus.get("title") or "")
    pub_ts = to_int(author.get("pub_ts"))
    type_raw = str(item.get("type") or "")

    return {
        "id": str(item.get("id_str") or ""),
        "type": normalize_type(type_raw),
        "type_raw": type_raw,
        "mid": to_int(author.get("mid")),
        "author": author.get("name") or "",
        "pub_ts": pub_ts,
        "time": (datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M")
                 if pub_ts else ""),
        "text": " ".join(str(text).split()),
        "major_type": str(major.get("type") or ""),
        "bvid": archive.get("bvid") or "",
        "url": _abs_url(archive.get("jump_url") or opus.get("jump_url")),
        "forward": _stat_count(modules.get("module_stat"), "forward"),
        "comment": _stat_count(modules.get("module_stat"), "comment"),
        "like": _stat_count(modules.get("module_stat"), "like"),
        "coin": _stat_count(modules.get("module_stat"), "coin"),
        "favorite": _stat_count(modules.get("module_stat"), "favorite"),
        "is_forward": type_raw == "DYNAMIC_TYPE_FORWARD",
        "orig_id": str(orig.get("id_str") or ""),
        "orig_author": orig_author.get("name") or "",
    }


def parse_page(payload):
    """响应体 → (items, has_more, next_offset)。结构不符预期时返回空，不抛。"""
    data = (payload or {}).get("data") or {}
    items = data.get("items") or []
    if not isinstance(items, list):
        items = []
    return items, bool(data.get("has_more")), str(data.get("offset") or "")


# ---------- 抓取 ----------

class DynamicsCrawler:
    """按 uid 翻页抓取动态。可取消；空结果会退避重试。"""

    def __init__(self, uid, out_dir, max_pages=DEFAULT_MAX_PAGES,
                 sleep=DEFAULT_SLEEP, empty_retries=DEFAULT_EMPTY_RETRIES,
                 progress=None, cancel=None, fetch=None, sleeper=None,
                 key_cache=None):
        self.uid = int(uid)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_pages = max(1, min(MAX_PAGES_LIMIT, int(max_pages)))
        self.sleep = max(0.0, float(sleep))
        self.empty_retries = max(1, int(empty_retries))
        self.cancel = cancel or (lambda: False)
        self._sleep = sleeper or time.sleep
        self._fetch = fetch or self._session_fetch
        # 取密钥也要能取消：首次请求前会先取一次 nav，那条路同样会退避重试。
        self._keys = key_cache or wbi.WbiKeyCache(cancel=self.cancel)
        self._progress = progress or (lambda **kw: None)
        self.out_path = self.out_dir / f"dynamics_{self.uid}.jsonl"
        self.rows = []
        self.stats = {"uid": self.uid, "pages": 0, "requests": 0,
                      "rows": 0, "truncated": False, "cancelled": False}

    def _p(self, **kw):
        self._progress(**kw)

    def _cancelled(self):
        return bool(self.cancel())

    def _session_fetch(self, url):
        """经 core.session 的四层风控通道发请求。

        cancel 必须透传：HTTP 层内部的退避等待受 TOTAL_WAIT_BUDGET 约束，拿不到
        取消谓词就只能睡满预算。爬取器自己那层取消检查在这之下够不着，
        不透传的话用户按取消最坏要等 90 秒。
        """
        from core import session          # 延迟导入：本模块要能离线单独导入
        return session.http_get_json(url, cancel=self.cancel)

    def _load_page(self, offset):
        """取一页。空结果退避重试；返回 (items, has_more, next_offset)。"""
        params = {"host_mid": self.uid, "offset": offset, "platform": "web",
                  "timezone_offset": -480, "features": "itemOpusStyle"}
        for attempt in range(self.empty_retries):
            if self._cancelled():
                raise TaskCancelledError()
            img_key, sub_key = self._keys.get()
            url = wbi.signed_url(DYNAMIC_FEED_URL, params, img_key, sub_key)
            self.stats["requests"] += 1
            items, has_more, next_offset = parse_page(self._fetch(url))
            if items:
                return items, has_more, next_offset
            if attempt + 1 < self.empty_retries:
                wait = min(EMPTY_RETRY_BASE * (2 ** attempt), EMPTY_RETRY_CAP)
                self._p(level="warn",
                        text=f"第 {self.stats['pages'] + 1} 页返回空，"
                             f"{wait:.0f}s 后重试（{attempt + 1}/{self.empty_retries}）")
                if not cancel_wait(wait, self.cancel, sleep=self._sleep):
                    raise TaskCancelledError()
        # 连续空：密钥可能已经轮换，丢掉缓存，下次取新的
        self._keys.invalidate()
        return [], False, ""

    def _pace(self, seconds):
        if seconds <= 0:
            return True
        return cancel_wait(seconds, self.cancel, sleep=self._sleep)

    def _flush(self):
        """整表重写 + 原子替换。行数最多几千，不必做增量追加。"""
        tmp = self.out_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in self.rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, self.out_path)

    def crawl(self):
        """抓取到 max_pages / 没有更多 / 用户取消为止。返回 stats。"""
        seen = set()
        offset = ""
        self.rows = []
        try:
            while self.stats["pages"] < self.max_pages:
                if self._cancelled():
                    raise TaskCancelledError()
                items, has_more, next_offset = self._load_page(offset)
                if not items:
                    if self.stats["pages"] == 0:
                        raise DynamicsUnavailable(
                            f"连续 {self.empty_retries} 次取到的都是空结果。"
                            "该用户没有动态、与被限流降级，两者响应完全相同，"
                            "无法区分——请稍后重试，或换一个 UID 验证。")
                    self.stats["truncated"] = True
                    self._p(level="warn", text="后续页返回空，可能在翻页途中被截断")
                    break
                new = 0
                for item in items:
                    row = parse_item(item)
                    if row["id"] and row["id"] in seen:
                        continue
                    if row["id"]:
                        seen.add(row["id"])
                    self.rows.append(row)
                    new += 1
                self.stats["pages"] += 1
                self.stats["rows"] = len(self.rows)
                self._flush()
                self._p(text=f"第 {self.stats['pages']} 页：+{new} 条，"
                             f"累计 {len(self.rows)} 条动态")
                if not has_more or not next_offset:
                    break
                offset = next_offset
                if self.stats["pages"] >= self.max_pages:
                    self.stats["truncated"] = True
                    break
                if not self._pace(self.sleep):
                    raise TaskCancelledError()
        except TaskCancelledError:
            self.stats["cancelled"] = True
            self.stats["rows"] = len(self.rows)
            if self.rows:
                self._flush()
            self._p(level="warn", text=f"已取消，保留已抓到的 {len(self.rows)} 条动态")
        return self.stats


# ---------- 导出 ----------

_HEADERS = ("序号", "发布时间", "类型", "正文", "转发", "评论", "点赞",
            "收藏", "投币", "BV号", "链接", "动态ID")
_WIDTHS = (6, 18, 10, 60, 8, 8, 10, 8, 8, 16, 40, 22)


def export_xlsx(rows, uid, stats, path, progress=None):
    """动态明细 → Excel。rows 为空也照样出一份带说明的表。"""
    if progress:
        progress(text="正在生成 Excel…")
    wb = xlsx_mod.new_workbook()
    sw = xlsx_mod.SheetWriter(wb)

    ws = wb.create_sheet("概览")
    sw.ws = ws
    sw.title_row(ws, f"用户动态导出 · UID {uid}", 4)
    note = ("注意：达到页数上限，可能还有更早的动态未抓取" if stats.get("truncated")
            else "已抓到底" if not stats.get("cancelled") else "任务被中途取消")
    sw.kv(ws, [
        ("UID", uid, "数据来自该用户的公开动态"),
        ("动态条数", len(rows), f"共 {stats.get('pages', 0)} 页"
                                f"（每页约 {PAGE_SIZE} 条）"),
        ("完成情况", note, f"取消={stats.get('cancelled', False)}"),
        ("抓取时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""),
    ])
    ws.append([None, sw.wc("口径：B站动态接口（游客通道 + WBI 签名）。"
                           "计数为接口返回的实时口径，非历史定格。",
                           font=xlsx_mod.F_CAPTION)])

    ws2 = wb.create_sheet("动态明细")
    sw.ws = ws2
    sw.title_row(ws2, f"UID {uid} 的动态明细", len(_HEADERS))
    sw.header_row(ws2, list(_HEADERS))
    for i, r in enumerate(rows, 1):
        ws2.append([None,
                    sw.wc(i), sw.wc(r.get("time")), sw.wc(r.get("type")),
                    sw.wc(r.get("text")), sw.wc(r.get("forward")),
                    sw.wc(r.get("comment")), sw.wc(r.get("like")),
                    sw.wc(r.get("favorite")), sw.wc(r.get("coin")),
                    sw.wc(r.get("bvid")), sw.wc(r.get("url")), sw.wc(r.get("id"))])
    for idx, width in enumerate(_WIDTHS, start=2):
        ws2.column_dimensions[get_column_letter(idx)].width = width
    wb.save(path)
    return path
