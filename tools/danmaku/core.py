# -*- coding: utf-8 -*-
"""抓取 B 站视频弹幕：分段拉取 protobuf → 解析 → Excel。

分段接口返回的是**原始 protobuf 字节**而非 JSON，因此走
core.session.http_get_bytes 这条二进制通道——它与 JSON 通道共用同一套闸门、
重试与风控统计，不是另建一条简化路径（契约见 tests/test_client_bytes.py）。

四条来自实测的硬约束（样本：BV1GJ411x7h7 一段 1410 条；长视频
BV1sHb56xEhC，10162 秒，逐段验证非空）：

1. 分段按 6 分钟切，segment_index 从 1 开始，**越界返回 0 字节**——不是
   code=0，也不是空 JSON。所以"空段"就是抓完的信号。
2. 空段**不重试**。这与用户动态那条路正好相反：那边空结果与风控降级的响应
   完全一致、无法区分，所以必须退避重试；这边空段是无歧义的协议语义，
   重试只会白白多打接口。
3. 但"空段出现得比时长推算的位置早"仍是可疑信号（可能被降级截断），要报截断。
4. 非空却解析出 0 条，说明字段号已经变了，必须报错——绝不能交一份"该视频
   没有弹幕"，那是把协议变更伪装成业务事实。

本模块不发弹幕、不做登录态、不需要 WBI 签名（实测游客态直接可用）。
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from math import ceil
from pathlib import Path

from openpyxl.utils import get_column_letter

from core import xlsx as xlsx_mod
from core.cancel import TaskCancelledError, wait as cancel_wait

from . import danmaku_pb2

SEG_URL = "https://api.bilibili.com/x/v2/dm/web/seg.so"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"

SEGMENT_SECONDS = 360          # 实测：每段 6 分钟
MINUTE_MS = 60_000
DEFAULT_MAX_SEGMENTS = 20
MAX_SEGMENTS_LIMIT = 500
DEFAULT_SLEEP = 0.8            # 段间隔（秒）

# 实测出现的取值：mode 1/4/5/7，pool 0/1。其余按官方文档惯例列出，
# 界面上一律带原值（未知取值显示成「未知(N)」），不假装认识。
MODE_LABELS = {1: "滚动", 4: "底部", 5: "顶部", 6: "逆向",
               7: "高级", 8: "代码", 9: "BAS"}
POOL_LABELS = {0: "普通", 1: "字幕", 2: "特殊"}


class DanmakuUnavailable(RuntimeError):
    """一条都没抓到，而视频自称有弹幕：无法区分"确实没有"与"被降级"时宁可报错。"""


class DanmakuDecodeError(RuntimeError):
    """分段字节无法按当前 proto 解析：协议变更，不能当成"没有弹幕"。"""


# ---------- 输入解析（纯函数，不发请求） ----------

def parse_target(text):
    """输入 → (bvid, aid, 分P)。链接 / BV号 / av号均可，支持 ?p=N。

    只解析，不发业务请求；b23.tv 短链例外（必须跟一次 30x 才知道指向哪）。
    用于拿 cid 的那次 view 请求在 fetch_video_meta 里发，且只发一次。
    """
    from core import links

    s = (text or "").strip().strip("\"'“”")
    if not s:
        raise ValueError("请输入视频链接或 BV 号")
    m = re.search(r"https?://[^\s\"'“”]+", s)
    if m:
        s = m.group(0)
    if links.B23_RE.search(s):
        s = links.resolve_url(s)
    page = 1
    pm = re.search(r"[?&]p=(\d+)", s, re.I)
    if pm:
        page = max(1, int(pm.group(1)))
    bm = links.BV_RE.search(s)
    if bm:
        return bm.group(0), None, page
    am = links.AV_RE.search(s)
    if am:
        return None, int(am.group(1)), page
    raise ValueError(f"无法识别的视频链接或 BV 号：{s[:60]}")


def fetch_video_meta(bvid=None, aid=None, page=1, cancel=None):
    """一次 view 请求取齐 cid / 时长 / 标题 / 分P 列表（游客可用）。"""
    from core import session

    if not bvid and not aid:
        raise ValueError("缺少 BV 号或 av 号")
    url = (f"{VIEW_URL}?bvid={bvid}" if bvid else f"{VIEW_URL}?aid={aid}")
    data = session.http_get_json(url, cancel=cancel)
    if data.get("code") != 0:
        raise ValueError(f"视频信息获取失败：code={data.get('code')} "
                         f"{data.get('message')}")
    d = data.get("data") or {}
    pages = d.get("pages") or []
    if pages:
        if not 1 <= page <= len(pages):
            raise ValueError(f"分P 超出范围：该视频共 {len(pages)} 个分P，"
                             f"请求的是 P{page}")
        pg = pages[page - 1]
        cid = pg.get("cid")
        duration = pg.get("duration") or 0
        part = pg.get("part") or ""
    else:
        cid, duration, part = d.get("cid"), d.get("duration") or 0, ""
    if not cid:
        raise ValueError("视频信息里没有 cid，无法定位弹幕")
    return {
        "bvid": d.get("bvid") or (bvid or ""),
        "aid": d.get("aid") or aid,
        "cid": int(cid),
        "duration": int(duration or 0),
        "title": d.get("title") or "",
        "owner": (d.get("owner") or {}).get("name") or "",
        "part": part,
        "page": page,
        "page_count": len(pages) or 1,
        "pubdate": d.get("pubdate"),
        # 接口自称的弹幕数。口径未经证实（可能含各种池/各分P），仅用于
        # "一条都没抓到却自称有弹幕"这一个判断，不参与对账。
        "claimed_danmaku": ((d.get("stat") or {}).get("danmaku") or 0),
    }


def expected_segments(duration):
    """按时长推算的段数上限。仅用于估算和截断判定，不当作硬边界（见 crawl）。"""
    if not duration:
        return None
    return max(1, ceil(int(duration) / SEGMENT_SECONDS))


# ---------- 解析（纯函数，可离线测试） ----------

def hhmmss(ms):
    """毫秒 → HH:MM:SS。定宽，便于在 Excel 里排序与筛选。"""
    total = max(0, int(ms)) // 1000
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def row_from_elem(elem):
    """一条 DanmakuElem → 扁平行。字段缺失一律降级为空/0，不抛异常。"""
    mode = int(elem.mode)
    pool = int(elem.pool)
    color = int(elem.color)
    ctime = int(elem.ctime)
    progress = int(elem.progress)
    return {
        "id": elem.idStr or str(int(elem.id)),
        "progress_ms": progress,
        "time": hhmmss(progress),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, f"未知({mode})"),
        "content": elem.content,
        "ctime": ctime,
        "sent_time": (datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M")
                      if ctime else ""),
        "mid_hash": elem.midHash,
        "color": color,
        "color_hex": f"#{color:06X}",
        "fontsize": int(elem.fontsize),
        "weight": int(elem.weight),
        "pool": pool,
        "pool_label": POOL_LABELS.get(pool, f"未知({pool})"),
        "attr": int(elem.attr),
    }


def parse_segment(raw):
    """一段原始字节 → 弹幕行列表。

    非空却解析出 0 条一律报错：那说明字段号变了（或抓到的根本不是这个接口的
    响应），静默返回空列表等于把协议变更伪装成"这个视频没有弹幕"。
    """
    reply = danmaku_pb2.DmSegMobileReply()
    try:
        reply.ParseFromString(raw)
    except Exception as exc:  # noqa: BLE001 - protobuf 的解析异常类型不安全
        raise DanmakuDecodeError(
            f"分段数据按 protobuf 解析失败（{len(raw)} 字节）："
            f"{type(exc).__name__}: {exc}") from exc
    rows = [row_from_elem(e) for e in reply.elems]
    if raw and not rows:
        raise DanmakuDecodeError(
            f"分段数据非空（{len(raw)} 字节）却解析出 0 条弹幕，"
            "疑似接口字段已变更；请把这条消息连同视频 BV 号反馈。")
    return rows


def minute_buckets(rows):
    """按分钟聚合弹幕密度 → [(分钟序号, 条数), ...]，按分钟升序。

    这是本工具相对"导出弹幕列表"的核心增量：一眼看出哪个时间点弹幕炸了。
    """
    counter = Counter(int(r.get("progress_ms") or 0) // MINUTE_MS for r in rows)
    return sorted(counter.items())


def page_minute_buckets(rows):
    """按 (分P, 分钟) 聚合 → [((分P, 分钟), 条数), ...]。

    多分P时每个分P都从 00:00 重新计时，只按分钟聚合会把不同分P的同一分钟
    混成一行，密度表就串台了。
    """
    counter = Counter(
        (int(r.get("page") or 1), int(r.get("progress_ms") or 0) // MINUTE_MS)
        for r in rows)
    return sorted(counter.items())


# ---------- 抓取 ----------

class DanmakuCrawler:
    """按 cid 逐段抓弹幕。可取消；空段即结束。"""

    def __init__(self, cid, out_dir, duration=0,
                 max_segments=DEFAULT_MAX_SEGMENTS, sleep=DEFAULT_SLEEP,
                 progress=None, cancel=None, fetch=None, sleeper=None,
                 name=None, page=1, part=""):
        self.cid = int(cid)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_segments = max(1, min(MAX_SEGMENTS_LIMIT, int(max_segments)))
        self.sleep = max(0.0, float(sleep))
        self.duration = int(duration or 0)
        self.cancel = cancel or (lambda: False)
        self._sleep = sleeper or time.sleep
        # 延迟导入：本模块要能离线单独导入（测试注入 fetch 后不碰网络）
        self._fetch = fetch or self._session_fetch
        self._progress = progress or (lambda **kw: None)
        # 多分P时每个分P一个爬取器，jsonl 得按名字区分，不能只认 cid
        self.page = int(page)
        self.part = part or ""
        self.out_path = self.out_dir / f"danmaku_{name or self.cid}.jsonl"
        self.rows = []
        self.stats = {"cid": self.cid, "segments": 0, "requests": 0, "rows": 0,
                      "expected_segments": expected_segments(self.duration),
                      "duplicates": 0, "truncated": False, "cancelled": False}

    def _p(self, **kw):
        self._progress(**kw)

    def _cancelled(self):
        return bool(self.cancel())

    def _session_fetch(self, url):
        """经 core.session 的二进制通道。cancel 必须透传：HTTP 层内部的退避
        等待靠它才能被打断，不透传的话用户按取消要等满整个等待预算。"""
        from core import session
        return session.http_get_bytes(url, cancel=self.cancel)

    def segment_url(self, index):
        return f"{SEG_URL}?type=1&oid={self.cid}&segment_index={index}"

    def _load_segment(self, index):
        """取一段原始字节。空（0 字节）即到头，不重试。"""
        if self._cancelled():
            raise TaskCancelledError()
        self.stats["requests"] += 1
        return self._fetch(self.segment_url(index))

    def _flush(self):
        """整表重写 + 原子替换。行数最多几万，不做增量追加。"""
        tmp = self.out_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in self.rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, self.out_path)

    def crawl(self):
        """逐段抓到空段 / 段数上限 / 用户取消为止。返回 stats。"""
        seen = set()
        expect = self.stats["expected_segments"]
        self.rows = []
        try:
            for index in range(1, self.max_segments + 1):
                raw = self._load_segment(index)
                if not raw:
                    # 空段 = 服务端说到头了。但比时长推算的位置早退，就可能
                    # 是被降级截断——记下来交给上层明示，不静默收工。
                    # 前提是已经抓到过东西：一条都没有时，这个视频本来就可能
                    # 没有弹幕，扣"截断"的帽子是误报。
                    if (self.stats["segments"] > 0 and expect is not None
                            and index <= expect):
                        self.stats["truncated"] = True
                        self._p(level="warn",
                                text=f"第 {index} 段起返回空（按时长预计共 "
                                     f"{expect} 段），可能被提前截断")
                    break
                elems = parse_segment(raw)
                new = 0
                for row in elems:
                    if row["id"] in seen:
                        self.stats["duplicates"] += 1
                        continue
                    seen.add(row["id"])
                    # 分P信息在这里盖戳：parse_segment 保持纯粹（只认字节），
                    # 而"这条弹幕属于哪个分P"是抓取上下文才知道的事。
                    row["page"] = self.page
                    row["part"] = self.part
                    self.rows.append(row)
                    new += 1
                self.stats["segments"] = index
                self.stats["rows"] = len(self.rows)
                self._flush()
                self._p(text=f"第 {index} 段：+{new} 条，"
                             f"累计 {len(self.rows):,} 条弹幕")
                if index < self.max_segments and not self._pace(self.sleep):
                    raise TaskCancelledError()
            else:
                # 跑满上限还没遇到空段：可能还有更后面的段
                if expect is None or self.max_segments < expect:
                    self.stats["truncated"] = True
        except TaskCancelledError:
            self.stats["cancelled"] = True
            self.stats["rows"] = len(self.rows)
            if self.rows:
                self._flush()
            self._p(level="warn",
                    text=f"已取消，保留已抓到的 {len(self.rows):,} 条弹幕")
        return self.stats

    def _pace(self, seconds):
        if seconds <= 0:
            return True
        return cancel_wait(seconds, self.cancel, sleep=self._sleep)


# ---------- 导出 ----------

_DETAIL_HEADERS = ("序号", "视频内时间", "进度(ms)", "模式", "正文", "发送时间",
                   "用户哈希", "颜色", "字号", "权重", "池", "属性", "弹幕ID")
_DETAIL_WIDTHS = (6, 12, 10, 8, 60, 18, 12, 10, 7, 7, 8, 8, 20)
# 多分P时多一列分P。单分P是绝大多数情况，为它保留一个恒为 "P1" 的列是噪音，
# 所以按任务是否真的跨分P切换列集，并在口径里写明。
_PART_HEADERS = ("序号", "分P", "视频内时间", "进度(ms)", "模式", "正文", "发送时间",
                 "用户哈希", "颜色", "字号", "权重", "池", "属性", "弹幕ID")
_PART_WIDTHS = (6, 18, 12, 10, 8, 60, 18, 12, 10, 7, 7, 8, 8, 20)


def part_label(page, part=""):
    """P2 第二局 / P1。分P标题为空（或缺失）时只留编号。

    page 缺失按 P1 处理，与 page_minute_buckets / 排序里 `int(... or 1)` 一致：
    旧 jsonl 或调用方手搓的行没有 page 字段，不该让一张报表在这里炸掉。
    """
    return f"P{int(page or 1)} {part or ''}".strip()


def _completion_note(stats, expect, rows):
    if stats.get("cancelled"):
        return "任务被中途取消"
    if not rows:
        return "一段都没抓到：该视频可能确实没有弹幕，也可能被降级，两者响应相同"
    if stats.get("truncated"):
        # 时长未知时 expect 是 None：直接印出来就是"预计约 None 段"，所以分叉。
        if expect:
            return (f"可能未抓全：按时长预计约 {expect} 段，"
                    f"实际抓到 {stats.get('segments', 0)} 段")
        return (f"可能未抓全：已抓到 {stats.get('segments', 0)} 段，"
                "但时长为 0 无法估算总段数")
    return "已抓到底"


_DASH = "—"
# 分析表的列宽。一张表里塞了四个块，列宽只能取一套折中值：B 列既是"指标"
# 也是"高频弹幕"，按最长的那类内容给。
_ANALYSIS_WIDTHS = (26, 16, 12, 30, 44)


def _pct_cell(value):
    return f"{value:.1f}%" if isinstance(value, (int, float)) else _DASH


def _users_value(analysis):
    if analysis.get("users") is None:
        return _DASH
    return f"{int(analysis['users']):,} 人"


def _write_analysis_sheet(wb, sw, label, analysis, multi):
    """弹幕分析写成一张表：KPI / 构成 / 高频弹幕+热词 / 热点分钟。

    分块之间空一行，跟本工具其他表（概览里的 kv + 说明）保持同一种读法。
    所有值都过 _pct_cell / 字符串化，None 不许漏进用户可见的单元格。
    """
    ws = wb.create_sheet("弹幕分析")
    sw.ws = ws
    sw.title_row(ws, f"{label} 弹幕分析（纯本地统计，无网络请求）", 5)
    kpi = analysis.get("kpi") or {}
    total = int(analysis.get("total") or 0)

    sw.kv(ws, [
        ("弹幕条数", f"{total:,}", "本次抓到的全部弹幕"),
        ("独立发送者", _users_value(analysis),
         analysis.get("users_note") or "按 mid_hash 去重，同一用户在同一视频下哈希稳定"),
        ("Top10 发送者占比", _pct_cell(analysis.get("top10_user_pct")),
         "发得最多的 10 个账号占全部弹幕的比例；越高越集中在少数人手里"),
        ("平均每条字数", f"{kpi.get('avg_len', 0.0):.1f}", "含表情标记，不去重"),
        ("非白弹幕", f"{int(kpi.get('colored') or 0):,}",
         f"颜色不是默认白色的弹幕，占 {_pct_cell(kpi.get('colored_pct'))}"),
        ("发送时间跨度", f"{kpi.get('span_hours', 0.0):.1f} 小时",
         f"{kpi.get('first_sent') or _DASH} ~ {kpi.get('last_sent') or _DASH}；"
         "跨度大说明是长尾视频，不是首播当晚刷完的"),
    ])

    # ---- 构成 ----
    sw.header_row(ws, ["类别", "条数", "占比"])
    composition = ([("模式 · " + k, v, p) for k, v, p in kpi.get("modes") or []]
                   + [("池 · " + k, v, p) for k, v, p in kpi.get("pools") or []])
    if composition:
        for name, count, pct in composition:
            ws.append([None, sw.wc(name), sw.wc(f"{count:,}"),
                       sw.wc(f"{pct:.1f}%")])
    else:
        ws.append([None, sw.wc("无弹幕，没有构成数据。", font=xlsx_mod.F_CAPTION)])
    ws.append([None])

    # ---- 高频弹幕 + 热词（并排，省一半行数） ----
    sw.header_row(ws, ["高频弹幕", "次数", "占比", "热词", "出现在几条弹幕"])
    top_danmaku = analysis.get("top_danmaku") or []
    top_words = analysis.get("top_words") or []
    if top_danmaku or top_words:
        for i in range(max(len(top_danmaku), len(top_words))):
            cells = []
            if i < len(top_danmaku):
                content, count, pct = top_danmaku[i]
                cells += [sw.wc(content), sw.wc(f"{count:,}"), sw.wc(f"{pct:.1f}%")]
            else:
                cells += [sw.wc(""), sw.wc(""), sw.wc("")]
            if i < len(top_words):
                word, count = top_words[i]
                cells += [sw.wc(word), sw.wc(f"{count:,}")]
            else:
                cells += [sw.wc(""), sw.wc("")]
            ws.append([None] + cells)
    else:
        ws.append([None, sw.wc(
            f"样本太少：没有出现达到 {analysis.get('high_freq_min_count')} 次的"
            "重复弹幕，也没有达到门槛的高频用词。", font=xlsx_mod.F_CAPTION)])
    ws.append([None])

    # ---- 热点分钟 ----
    headers = (["分P", "分钟区间", "条数", "占比", "该分钟刷得最多"] if multi
               else ["分钟区间", "条数", "占比", "该分钟刷得最多"])
    sw.header_row(ws, headers)
    hot = analysis.get("hot_minutes") or []
    if hot:
        for m in hot:
            cells = []
            if multi:
                cells.append(sw.wc(f"P{m.get('page')}"))
            cells += [sw.wc(m.get("time")), sw.wc(f"{m.get('count', 0):,}"),
                      sw.wc(f"{m.get('pct', 0.0):.1f}%"),
                      sw.wc("；".join(m.get("examples") or [])
                            or "（该分钟没有重复内容）")]
            ws.append([None] + cells)
    else:
        ws.append([None, sw.wc("没有弹幕，无法给出热点分钟。",
                               font=xlsx_mod.F_CAPTION)])
    ws.append([None])
    ws.append([None, sw.wc(
        "口径：纯本地统计，不产生任何网络请求。高频弹幕与热词"
        + ("跨全部分P合并统计" if multi else "按单分P统计")
        + "；热点分钟按 (分P, 分钟) 分开，各分P都从 00:00 重新计时。"
          "热词由中文 2/3/4 字滑窗 n-gram 加停用词过滤得到，非严格分词，"
          "可能出现不成词的片段。",
        font=xlsx_mod.F_CAPTION)])

    for idx, width in enumerate(_ANALYSIS_WIDTHS, start=2):
        ws.column_dimensions[get_column_letter(idx)].width = width


def export_xlsx(rows, meta, stats, path, progress=None, parts=None, sleep=None,
                analysis=None):
    """弹幕明细 → Excel（概览 / 弹幕分析 / 弹幕明细 / 密度分布 [+ 分P汇总]）。

    rows 为空也照样出表。parts 是各分P的小结列表；传了它就说明这次抓了多个
    分P，明细与密度都会多一列"分P"，并额外出一张分P汇总表。
    sleep 只用于在概览里如实记下本次的段间隔——写死默认值会让报告与实情不符。
    analysis 由 tools.danmaku.analysis 算好传进来；不传就不出"弹幕分析"表。
    方向刻意是这个：core 不去 import analysis，否则两边互相 import 成环。
    """
    if progress:
        progress(text="正在生成 Excel…")
    wb = xlsx_mod.new_workbook()
    sw = xlsx_mod.SheetWriter(wb)
    expect = stats.get("expected_segments")
    label = meta.get("bvid") or f"av{meta.get('aid')}"
    multi = bool(parts)
    step = SEGMENT_SECONDS // 60

    # ---- 概览 ----
    ws = wb.create_sheet("概览")
    sw.ws = ws
    sw.title_row(ws, f"弹幕导出 · {label}", 4)
    part_cell = (f"全部 {meta.get('page_count')} 个分P"
                 if multi else f"P{meta.get('page')} / 共 {meta.get('page_count')} 个分P")
    part_note = ("各分P分别统计" if multi
                 else (meta.get("part") or "") if meta.get("page_count", 1) > 1
                 else "单分P视频")
    # 多分P时概览说合计口径：时长求和，否则只会显示 P1 的时长，容易被误读成
    # "整个视频就这么长"。
    if multi:
        duration_s = sum(int(p.get("duration") or 0) for p in parts)
    else:
        duration_s = meta.get("duration", 0)
    sw.kv(ws, [
        ("视频标题", meta.get("title") or "", f"UP主：{meta.get('owner') or '未知'}"),
        ("BV号 / av号", f"{meta.get('bvid') or '-'} / av{meta.get('aid')}",
         f"cid={meta.get('cid')}（弹幕挂在 cid 上，不是 aid）"),
        ("分P", part_cell, part_note),
        ("视频时长", hhmmss(duration_s * 1000),
         f"共 {duration_s} 秒"
         + (f"（{len(parts)} 个分P 之和，各分P独立计时）" if multi
            else "")),
        ("弹幕条数", f"{len(rows):,}",
         f"来自 {stats.get('segments', 0)} 段（每段 {step} 分钟）"
         + (f"，预计 {expect} 段" if expect else "")),
        ("完成情况", _completion_note(stats, expect, rows),
         f"取消={stats.get('cancelled', False)} 截断={stats.get('truncated', False)}"
         f" 去重={stats.get('duplicates', 0)}"),
        ("接口自称弹幕数", f"{meta.get('claimed_danmaku', 0):,}",
         "视频接口的计数，口径未经证实（可能含各池/各分P），仅供参考不对账"),
        ("抓取时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         f"段间隔 {DEFAULT_SLEEP if sleep is None else sleep}s"),
    ])
    ws.append([None, sw.wc(
        "口径：B站弹幕分段接口（游客通道，二进制 protobuf，无需登录、无需签名）。"
        "「视频内时间」为该弹幕在第几秒出现，与视频进度条对应；"
        "明细表已按视频内时间排序（接口原始顺序是发送顺序，见同目录 jsonl）。"
        + ("多分P时明细与密度表带「分P」列，各分P都从 00:00 重新计时。"
           if multi else ""),
        font=xlsx_mod.F_CAPTION)])

    # ---- 弹幕分析（纯本地统计） ----
    # 放在概览之后、明细之前：它跟概览一样是"结论层"，明细与密度是"数据层"。
    if analysis:
        _write_analysis_sheet(wb, sw, label, analysis, multi)

    # ---- 分P汇总（只在多分P时存在） ----
    if multi:
        ws0 = wb.create_sheet("分P汇总")
        sw.ws = ws0
        sw.title_row(ws0, f"{label} 各分P小结", 6)
        sw.header_row(ws0, ["分P", "标题", "时长", "弹幕条数", "段数", "完成情况"])
        for p in parts:
            pexp = p.get("expected_segments")
            ws0.append([None, sw.wc(part_label(p.get("page"), p.get("part"))),
                        sw.wc(p.get("part") or ""),
                        sw.wc(hhmmss(p.get("duration", 0) * 1000)),
                        sw.wc(p.get("rows", 0)), sw.wc(p.get("segments", 0)),
                        sw.wc(_completion_note(p, pexp, p.get("rows", 0)))])
        for idx, width in enumerate((18, 26, 12, 12, 8, 40), start=2):
            ws0.column_dimensions[get_column_letter(idx)].width = width
        # 合计只占一行：拆成两行（标签一行、数字一行）在 Excel 里看着像两组数据。
        ws0.append([None] + [sw.wc(v, font=xlsx_mod.F_HEADER,
                                   fill=xlsx_mod.FILL_HEADER) for v in (
            "合计", "", hhmmss(sum(int(p.get("duration") or 0)
                                   for p in parts) * 1000),
            len(rows), stats.get("segments", 0), "")])

    # ---- 弹幕明细 ----
    # 按视频内时间排序：接口是按弹幕 ID（近似发送顺序）下发的，直接导出的话时间
    # 轴是散的，看不出哪句话出现在哪一段。只影响这张表——jsonl 保留原始顺序。
    ordered = sorted(rows, key=lambda r: (int(r.get("page") or 1),
                                          r.get("progress_ms") or 0,
                                          str(r.get("id") or "")))
    headers = _PART_HEADERS if multi else _DETAIL_HEADERS
    widths = _PART_WIDTHS if multi else _DETAIL_WIDTHS
    ws2 = wb.create_sheet("弹幕明细")
    sw.ws = ws2
    sw.title_row(ws2, f"{label} 弹幕明细", len(headers))
    sw.header_row(ws2, list(headers))
    for i, r in enumerate(ordered, 1):
        cells = [sw.wc(i)]
        if multi:
            cells.append(sw.wc(part_label(r.get("page"), r.get("part"))))
        cells += [sw.wc(r.get("time")), sw.wc(r.get("progress_ms")),
                  sw.wc(r.get("mode_label")), sw.wc(r.get("content")),
                  sw.wc(r.get("sent_time")), sw.wc(r.get("mid_hash")),
                  sw.wc(r.get("color_hex")), sw.wc(r.get("fontsize")),
                  sw.wc(r.get("weight")), sw.wc(r.get("pool_label")),
                  sw.wc(r.get("attr")), sw.wc(r.get("id"))]
        ws2.append([None] + cells)
    for idx, width in enumerate(widths, start=2):
        ws2.column_dimensions[get_column_letter(idx)].width = width

    # ---- 密度分布 ----
    ws3 = wb.create_sheet("密度分布")
    sw.ws = ws3
    headers3 = ["分P", "视频内时间", "该分钟条数", "占比", "累计占比", "累计条数"] \
        if multi else ["视频内时间", "该分钟条数", "占比", "累计占比", "累计条数"]
    sw.title_row(ws3, f"{label} 弹幕密度分布（按分钟）", len(headers3))
    sw.header_row(ws3, headers3)
    total = len(rows) or 1
    # 多分P时各分P的分钟会互相重叠，必须带上分P聚合，否则密度会串台
    raw_buckets = page_minute_buckets(rows) if multi else minute_buckets(rows)
    acc = 0
    for key, count in raw_buckets:
        acc += count
        page, minute = key if multi else (1, key)
        cells = []
        if multi:
            cells.append(sw.wc(part_label(page)))
        cells += [sw.wc(f"{hhmmss(minute * MINUTE_MS)} – "
                        f"{hhmmss((minute + 1) * MINUTE_MS)}"),
                  sw.wc(count), sw.wc(f"{count / total * 100:.1f}%"),
                  sw.wc(f"{acc / total * 100:.1f}%"), sw.wc(acc)]
        ws3.append([None] + cells)
    total_row = [None, sw.wc("合计", font=xlsx_mod.F_HEADER,
                             fill=xlsx_mod.FILL_HEADER)]
    if multi:
        total_row.append(sw.wc("", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER))
    total_row += [sw.wc(len(rows), font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER),
                  sw.wc("100.0%" if rows else "0.0%", font=xlsx_mod.F_HEADER,
                        fill=xlsx_mod.FILL_HEADER), sw.wc(""), sw.wc("")]
    ws3.append(total_row)
    for idx, width in enumerate((18, 26, 12, 10, 10, 10)[:len(headers3)], start=2):
        ws3.column_dimensions[get_column_letter(idx)].width = width
    ws3.append([None])
    ws3.append([None, sw.wc(
        "按弹幕出现的视频时间每 60 秒一档聚合。峰值档位通常对应"
        "「名场面」，可用于定位二次创作与切片素材。",
        font=xlsx_mod.F_CAPTION)])

    wb.save(path)
    return path
