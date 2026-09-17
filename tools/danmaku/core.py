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
from core.xlsx_metadata import (
    FieldDefinition,
    QualityItem,
    classify_declared_count,
    make_metadata,
    primary_key_quality,
    write_metadata_sheets,
)
from core.xlsx_presentation import (
    TableLayout,
    append_sheet_directory,
    configure_table,
    finish_table,
    wrap_cell,
)
from core.budget import BudgetExhaustedError
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

def parse_target(text, cancel=None):
    """输入 → (bvid, aid, 分P)。链接 / BV号 / av号均可，支持 ?p=N。

    只解析，不发业务请求；b23.tv 短链例外（必须跟一次 30x 才知道指向哪，
    该次解析过全局闸门，cancel 仅作用于其闸门等待）。
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
        s = links.resolve_url(s, cancel=cancel)
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


def fetch_video_meta(bvid=None, aid=None, page=1, cancel=None, budget=None):
    """一次 view 请求取齐 cid / 时长 / 标题 / 分P 列表（游客可用）。"""
    from core import session

    if not bvid and not aid:
        raise ValueError("缺少 BV 号或 av 号")
    url = (f"{VIEW_URL}?bvid={bvid}" if bvid else f"{VIEW_URL}?aid={aid}")
    kwargs = {"cancel": cancel}
    if budget is not None:
        kwargs["budget"] = budget
    data = session.http_get_json(url, **kwargs)
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
    stat = d.get("stat") or {}
    claimed_danmaku = stat.get("danmaku") if "danmaku" in stat else None
    declared_status, _declared_value = classify_declared_count(
        claimed_danmaku, present="danmaku" in stat,
    )
    if declared_status == "invalid":
        # pipeline.py 有一条历史的“空结果且接口声称有弹幕”失败保护，
        # 只认 int/float。先在元信息边界把已确认非法值降为格式异常载荷，
        # 让本次任务继续生成 XLSX，而不把非法声明伪装成未返回。
        claimed_danmaku = str(claimed_danmaku)
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
        "claimed_danmaku": claimed_danmaku,
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
                 name=None, page=1, part="", budget=None):
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
        # 任务预算（core.budget.TaskBudget），多分P时各爬取器共享同一对象；
        # None = 无预算。段请求的强制点在 BiliClient._request（fetch 透传
        # budget），本层的职责是段循环边界查 expired() 与落盘后记账记录数。
        self.budget = budget
        # 多分P时每个分P一个爬取器，jsonl 得按名字区分，不能只认 cid
        self.page = int(page)
        self.part = part or ""
        self.out_path = self.out_dir / f"danmaku_{name or self.cid}.jsonl"
        self.rows = []
        self.stats = {"cid": self.cid, "segments": 0, "requests": 0, "rows": 0,
                      "candidate_records": 0, "missing_id": 0, "invalid_id": 0,
                      "expected_segments": expected_segments(self.duration),
                      "duplicates": 0, "truncated": False, "cancelled": False,
                      "max_segments": self.max_segments}

    def _p(self, **kw):
        self._progress(**kw)

    def _cancelled(self):
        return bool(self.cancel())

    def _session_fetch(self, url):
        """经 core.session 的二进制通道。cancel 必须透传：HTTP 层内部的退避
        等待靠它才能被打断，不透传的话用户按取消要等满整个等待预算。
        budget 同理透传：业务请求数的强制点在 HTTP 层入口；无预算时不附加
        该 kwarg，缺省调用与旧路径一致。"""
        from core import session
        kwargs = {"cancel": self.cancel}
        if self.budget is not None:
            kwargs["budget"] = self.budget
        return session.http_get_bytes(url, **kwargs)

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
        """逐段抓到空段 / 段数上限 / 用户取消 / 预算到限为止。返回 stats。"""
        seen = set()
        expect = self.stats["expected_segments"]
        self.rows = []
        try:
            for index in range(1, self.max_segments + 1):
                # 预算边界：到限按正常完成收尾（不是失败、也不是取消）。
                # 取消检查在预算检查之前（_load_segment 内），两者同时到期
                # 时按取消语义。
                if self.budget is not None and self.budget.expired():
                    self.stats["stopped_reason"] = "budget_reached"
                    self._p(level="warn",
                            text=f"已达预算上限（{self.budget.reason()}），"
                                 f"安全停止，保留已抓到的 "
                                 f"{len(self.rows):,} 条弹幕")
                    break
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
                self.stats["candidate_records"] += len(elems)
                new = 0
                for row in elems:
                    if row.get("id") in (None, ""):
                        self.stats["missing_id"] += 1
                        continue
                    dedup_key = (self.page, row["id"])
                    if dedup_key in seen:
                        self.stats["duplicates"] += 1
                        continue
                    row["page"] = self.page
                    seen.add(dedup_key)
                    # 分P信息在这里盖戳：parse_segment 保持纯粹（只认字节），
                    # 而"这条弹幕属于哪个分P"是抓取上下文才知道的事。
                    row["page"] = self.page
                    row["part"] = self.part
                    self.rows.append(row)
                    new += 1
                self.stats["segments"] = index
                self.stats["rows"] = len(self.rows)
                self._flush()
                if self.budget is not None:
                    self.budget.observe_records(new)
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
        except BudgetExhaustedError:
            # 兜底：段请求在 HTTP 层入口被预算硬拦。与取消分支对称地保留
            # 已抓数据，但记为预算停止——取消与预算互不冒充。
            self.stats["stopped_reason"] = "budget_reached"
            self.stats["rows"] = len(self.rows)
            if self.rows:
                self._flush()
            self._p(level="warn",
                    text=f"已达预算上限，安全停止，保留已抓到的 "
                         f"{len(self.rows):,} 条弹幕")
        self.stats["dedup_discarded"] = self.stats.get("duplicates", 0)
        self.stats["remaining_conflicts"] = 0
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
    if stats.get("stopped_reason") == "budget_reached":
        return "已达上限安全停止（请求数/时长预算到限，已抓数据完整保留）"
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


def _state_cell(kind, label):
    return xlsx_mod.cell_value(None, kind, note=label)


def _typed_cell(value, kind, label, number_format=None):
    if value is None:
        return _state_cell(xlsx_mod.CellKind.MISSING, f"{label}缺失")
    return xlsx_mod.checked_cell_value(value, kind, number_format=number_format,
                                       note=f"{label}格式异常")


def _field_cell(mapping, key, kind, label):
    if key not in mapping:
        return _state_cell(xlsx_mod.CellKind.NOT_RETURNED, f"{label}未返回")
    if mapping[key] is None:
        return _state_cell(xlsx_mod.CellKind.MISSING, f"{label}缺失")
    if kind is xlsx_mod.CellKind.DATETIME:
        return xlsx_mod.unix_seconds_cell_value(mapping[key], note=f"{label}格式异常")
    return _typed_cell(mapping[key], kind, label)


def _percent_cell(value, label):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return _state_cell(xlsx_mod.CellKind.MISSING, f"{label}缺失")
    return _typed_cell(value / 100, xlsx_mod.CellKind.PERCENT, label)


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
    total = analysis.get("total")

    sw.kv(ws, [
        ("弹幕条数", _typed_cell(total, xlsx_mod.CellKind.INTEGER, "弹幕条数"),
         "本次抓到的全部弹幕"),
        ("独立发送者", _typed_cell(analysis.get("users"), xlsx_mod.CellKind.INTEGER,
                                  "独立发送者"),
         analysis.get("users_note") or "按 mid_hash 去重，同一用户在同一视频下哈希稳定"),
        ("Top10 发送者占比", _percent_cell(analysis.get("top10_user_pct"),
                                          "Top10发送者占比"),
         "发得最多的 10 个账号占全部弹幕的比例；越高越集中在少数人手里"),
        ("平均每条字数", _typed_cell(kpi.get("avg_len"), xlsx_mod.CellKind.DECIMAL,
                                    "平均每条字数"), "含表情标记，不去重"),
        ("非白弹幕", _typed_cell(kpi.get("colored"), xlsx_mod.CellKind.INTEGER,
                                "非白弹幕"),
         f"颜色不是默认白色的弹幕，占 {_pct_cell(kpi.get('colored_pct'))}"),
        ("发送时间跨度", _typed_cell(kpi.get("span_hours"), xlsx_mod.CellKind.DECIMAL,
                                     "发送时间跨度", '0.0" 小时"'),
         f"{kpi.get('first_sent') or _DASH} ~ {kpi.get('last_sent') or _DASH}；"
         "跨度大说明是长尾视频，不是首播当晚刷完的"),
    ])

    # ---- 构成 ----
    sw.header_row(ws, ["类别", "条数", "占比"])
    composition = ([("模式 · " + k, v, p) for k, v, p in kpi.get("modes") or []]
                   + [("池 · " + k, v, p) for k, v, p in kpi.get("pools") or []])
    if composition:
        for name, count, pct in composition:
            sw.append([None, _typed_cell(name, xlsx_mod.CellKind.TEXT, "类别"),
                       _typed_cell(count, xlsx_mod.CellKind.INTEGER, "条数"),
                       _percent_cell(pct, "占比")])
    else:
        sw.append([None, sw.wc("无弹幕，没有构成数据。", font=xlsx_mod.F_CAPTION)])
    sw.append([None])

    # ---- 高频弹幕 + 热词（并排，省一半行数） ----
    sw.header_row(ws, ["高频弹幕", "次数", "占比", "热词", "出现在几条弹幕"])
    top_danmaku = analysis.get("top_danmaku") or []
    top_words = analysis.get("top_words") or []
    if top_danmaku or top_words:
        for i in range(max(len(top_danmaku), len(top_words))):
            cells = []
            if i < len(top_danmaku):
                content, count, pct = top_danmaku[i]
                cells += [_typed_cell(content, xlsx_mod.CellKind.TEXT, "高频弹幕"),
                           _typed_cell(count, xlsx_mod.CellKind.INTEGER, "次数"),
                           _percent_cell(pct, "占比")]
            else:
                cells += [_typed_cell("", xlsx_mod.CellKind.TEXT, "高频弹幕"),
                           _typed_cell("", xlsx_mod.CellKind.TEXT, "次数"),
                           _typed_cell("", xlsx_mod.CellKind.TEXT, "占比")]
            if i < len(top_words):
                word, count = top_words[i]
                cells += [_typed_cell(word, xlsx_mod.CellKind.TEXT, "热词"),
                           _typed_cell(count, xlsx_mod.CellKind.INTEGER, "出现在几条弹幕")]
            else:
                cells += [_typed_cell("", xlsx_mod.CellKind.TEXT, "热词"),
                           _typed_cell("", xlsx_mod.CellKind.TEXT, "出现在几条弹幕")]
            sw.append([None] + cells)
    else:
        sw.append([None, sw.wc(
            f"样本太少：没有出现达到 {analysis.get('high_freq_min_count')} 次的"
            "重复弹幕，也没有达到门槛的高频用词。", font=xlsx_mod.F_CAPTION)])
    sw.append([None])

    # ---- 热点分钟 ----
    headers = (["分P", "分钟区间", "条数", "占比", "该分钟刷得最多"] if multi
               else ["分钟区间", "条数", "占比", "该分钟刷得最多"])
    sw.header_row(ws, headers)
    hot = analysis.get("hot_minutes") or []
    if hot:
        for m in hot:
            cells = []
            if multi:
                cells.append(_typed_cell(f"P{m.get('page')}", xlsx_mod.CellKind.TEXT, "分P"))
            cells += [_typed_cell(m.get("time"), xlsx_mod.CellKind.TEXT, "分钟区间"),
                      _typed_cell(m.get("count"), xlsx_mod.CellKind.INTEGER, "条数"),
                      _percent_cell(m.get("pct"), "占比"),
                      _typed_cell("；".join(m.get("examples") or [])
                                  or "（该分钟没有重复内容）",
                                  xlsx_mod.CellKind.TEXT, "该分钟刷得最多")]
            sw.append([None] + cells)
    else:
        sw.append([None, sw.wc("没有弹幕，无法给出热点分钟。",
                               font=xlsx_mod.F_CAPTION)])
    sw.append([None])
    sw.append([None, sw.wc(
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

    def state(kind, label_text):
        return _state_cell(kind, label_text)

    def value(raw, kind, label_text, number_format=None):
        return _typed_cell(raw, kind, label_text, number_format)

    def field(mapping, key, kind, label_text):
        return _field_cell(mapping, key, kind, label_text)

    def safe_metric(raw):
        return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0

    declared_status, declared_value = classify_declared_count(
        meta.get("claimed_danmaku"),
        present="claimed_danmaku" in meta,
    )
    if declared_status == "not_returned":
        claimed_overview_cell = state(xlsx_mod.CellKind.NOT_RETURNED, "接口未返回")
    elif declared_status == "invalid":
        claimed_overview_cell = state(xlsx_mod.CellKind.MISSING, "声明数量格式异常")
    else:
        claimed_overview_cell = xlsx_mod.checked_cell_value(
            declared_value, xlsx_mod.CellKind.INTEGER, note="声明数量格式异常"
        )

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
    if multi:
        duration_s = sum(int(p["duration"]) for p in parts
                         if isinstance(p.get("duration"), (int, float))
                         and not isinstance(p.get("duration"), bool))
    else:
        duration_s = meta.get("duration")
    duration_s = safe_metric(duration_s)
    sw.kv(ws, [
        ("视频标题", value(meta.get("title") or "", xlsx_mod.CellKind.TEXT, "视频标题"),
         f"UP主：{meta.get('owner') or '未知'}"),
        ("BV号 / av号", value(f"{meta.get('bvid') or '-'} / av{meta.get('aid')}",
                              xlsx_mod.CellKind.TEXT, "视频ID"),
         f"cid={meta.get('cid')}（弹幕挂在 cid 上，不是 aid）"),
        ("分P", value(part_cell, xlsx_mod.CellKind.TEXT, "分P"), part_note),
        ("视频时长", value(hhmmss(duration_s * 1000), xlsx_mod.CellKind.TEXT, "视频时长"),
         f"共 {duration_s} 秒"
         + (f"（{len(parts)} 个分P之和，各分P独立计时）" if multi else "")),
        ("弹幕条数", value(len(rows), xlsx_mod.CellKind.INTEGER, "弹幕条数"),
         f"来自 {stats.get('segments', 0)} 段（每段 {step} 分钟）"
         + (f"，预计 {expect} 段" if expect else "")),
        ("完成情况", value(_completion_note(stats, expect, rows), xlsx_mod.CellKind.TEXT, "完成情况"),
         f"取消={stats.get('cancelled', False)} 截断={stats.get('truncated', False)}"
         f" 去重={stats.get('duplicates', 0)}"),
        ("接口自称弹幕数", claimed_overview_cell,
         "视频接口的计数，口径未经证实（可能含各池/各分P），仅供参考不对账"),
        ("抓取时间", value(datetime.now(xlsx_mod.ASIA_SHANGHAI).replace(tzinfo=None),
                           xlsx_mod.CellKind.DATETIME, "抓取时间"),
         f"段间隔 {DEFAULT_SLEEP if sleep is None else sleep}s"),
    ])
    sw.append([None, sw.wc(
        "口径：B站弹幕分段接口（游客通道，二进制 protobuf，无需登录、无需签名）。"
        "「视频内时间」为该弹幕在第几秒出现，与视频进度条对应；"
        "明细表已按视频内时间排序（接口原始顺序是发送顺序，见同目录 jsonl）。"
        + ("多分P时明细与密度表带「分P」列，各分P都从 00:00 重新计时。"
           if multi else ""),
        font=xlsx_mod.F_CAPTION)])

    directory_entries = []
    if analysis:
        directory_entries.append(("弹幕分析", "弹幕分析"))
    if multi:
        directory_entries.append(("分P汇总", "分P汇总"))
    directory_entries.extend((
        ("弹幕明细", "弹幕明细"),
        ("密度分布", "密度分布"),
        ("数据质量", "数据质量"),
        ("字段说明", "字段说明"),
    ))
    append_sheet_directory(sw, ws, directory_entries)

    if analysis:
        _write_analysis_sheet(wb, sw, label, analysis, multi)

    # ---- 分P汇总（只在多分P时存在） ----
    if multi:
        ws0 = wb.create_sheet("分P汇总")
        sw.ws = ws0
        parts_layout = TableLayout(3, 2, 7)
        configure_table(ws0, parts_layout)
        sw.title_row(ws0, f"{label} 各分P小结", 6)
        sw.header_row(ws0, ["分P", "标题", "时长", "弹幕条数", "段数", "完成情况"])
        for p in parts:
            pexp = p.get("expected_segments")
            sw.append([None, value(part_label(p.get("page"), p.get("part")),
                                   xlsx_mod.CellKind.TEXT, "分P"),
                        wrap_cell(sw, value(p.get("part") or "", xlsx_mod.CellKind.TEXT, "标题")),
                       value(hhmmss(safe_metric(p.get("duration")) * 1000),
                             xlsx_mod.CellKind.TEXT, "时长"),
                       value(p.get("rows"), xlsx_mod.CellKind.INTEGER, "弹幕条数"),
                       value(p.get("segments"), xlsx_mod.CellKind.INTEGER, "段数"),
                        value(_completion_note(p, pexp, p.get("rows") or 0),
                              xlsx_mod.CellKind.TEXT, "完成情况")])
        finish_table(ws0, parts_layout, len(parts))
        for idx, width in enumerate((18, 26, 12, 12, 8, 40), start=2):
            ws0.column_dimensions[get_column_letter(idx)].width = width
        total_duration = sum(int(p["duration"]) for p in parts
                             if isinstance(p.get("duration"), (int, float))
                             and not isinstance(p.get("duration"), bool))
        sw.append([None, sw.wc("合计", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER),
                   sw.wc("", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER),
                   sw.wc(hhmmss(total_duration * 1000), font=xlsx_mod.F_HEADER,
                         fill=xlsx_mod.FILL_HEADER),
                   sw.wc(len(rows), font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER,
                         kind=xlsx_mod.CellKind.INTEGER),
                   sw.wc(stats.get("segments", 0), font=xlsx_mod.F_HEADER,
                         fill=xlsx_mod.FILL_HEADER, kind=xlsx_mod.CellKind.INTEGER),
                   sw.wc("", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER)])

    # ---- 弹幕明细 ----
    def sort_page(row):
        return row.get("page") if isinstance(row.get("page"), int) else 1

    def sort_progress(row):
        return row.get("progress_ms") if isinstance(row.get("progress_ms"), int) else 0

    ordered = sorted(rows, key=lambda r: (sort_page(r), sort_progress(r), str(r.get("id") or "")))
    headers = _PART_HEADERS if multi else _DETAIL_HEADERS
    widths = _PART_WIDTHS if multi else _DETAIL_WIDTHS
    ws2 = wb.create_sheet("弹幕明细")
    sw.ws = ws2
    detail_layout = TableLayout(3, 2, len(headers) + 1)
    configure_table(ws2, detail_layout)
    sw.title_row(ws2, f"{label} 弹幕明细", len(headers))
    sw.header_row(ws2, list(headers))
    for i, r in enumerate(ordered, 1):
        sent_cell = (field(r, "ctime", xlsx_mod.CellKind.DATETIME, "发送时间")
                     if "ctime" in r and r.get("ctime") is not None
                     else value(r.get("sent_time") or "", xlsx_mod.CellKind.TEXT, "发送时间"))
        cells = [value(i, xlsx_mod.CellKind.INTEGER, "序号")]
        if multi:
            cells.append(value(part_label(r.get("page"), r.get("part")),
                               xlsx_mod.CellKind.TEXT, "分P"))
        cells += [field(r, "time", xlsx_mod.CellKind.TEXT, "视频内时间"),
                  field(r, "progress_ms", xlsx_mod.CellKind.INTEGER, "进度(ms)"),
                  field(r, "mode_label", xlsx_mod.CellKind.TEXT, "模式"),
                  wrap_cell(sw, field(r, "content", xlsx_mod.CellKind.TEXT, "正文")), sent_cell,
                  field(r, "mid_hash", xlsx_mod.CellKind.TEXT, "用户哈希"),
                  field(r, "color_hex", xlsx_mod.CellKind.TEXT, "颜色"),
                  field(r, "fontsize", xlsx_mod.CellKind.INTEGER, "字号"),
                  field(r, "weight", xlsx_mod.CellKind.INTEGER, "权重"),
                  field(r, "pool_label", xlsx_mod.CellKind.TEXT, "池"),
                  field(r, "attr", xlsx_mod.CellKind.INTEGER, "属性"),
                  field(r, "id", xlsx_mod.CellKind.ID, "弹幕ID")]
        sw.append([None] + cells)
    for idx, width in enumerate(widths, start=2):
        ws2.column_dimensions[get_column_letter(idx)].width = width
    finish_table(ws2, detail_layout, len(ordered))

    # ---- 密度分布 ----
    raw_buckets = page_minute_buckets(rows) if multi else minute_buckets(rows)
    ws3 = wb.create_sheet("密度分布")
    headers3 = (["分P", "视频内时间", "该分钟条数", "占比", "累计占比", "累计条数"]
                if multi else ["视频内时间", "该分钟条数", "占比", "累计占比", "累计条数"])
    sw.ws = ws3
    density_layout = TableLayout(3, 2, len(headers3) + 1)
    configure_table(ws3, density_layout)
    sw.title_row(ws3, f"{label} 弹幕密度分布（按分钟）", len(headers3))
    sw.header_row(ws3, headers3)
    total = len(rows)
    acc = 0
    for key, count in raw_buckets:
        acc += count
        page, minute = key if multi else (1, key)
        cells = []
        if multi:
            cells.append(value(part_label(page), xlsx_mod.CellKind.TEXT, "分P"))
        cells += [value(f"{hhmmss(minute * MINUTE_MS)} – "
                        f"{hhmmss((minute + 1) * MINUTE_MS)}",
                        xlsx_mod.CellKind.TEXT, "视频内时间"),
                  value(count, xlsx_mod.CellKind.INTEGER, "该分钟条数"),
                  _percent_cell(count / total * 100 if total else None, "占比"),
                  _percent_cell(acc / total * 100 if total else None, "累计占比"),
                   value(acc, xlsx_mod.CellKind.INTEGER, "累计条数")]
        sw.append([None] + cells)
    finish_table(ws3, density_layout, len(raw_buckets))
    total_row = [None, sw.wc("合计", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER)]
    if multi:
        total_row.append(sw.wc("", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER))
    total_row += [sw.wc(len(rows), font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER,
                        kind=xlsx_mod.CellKind.INTEGER),
                  sw.wc(1.0 if rows else 0.0, font=xlsx_mod.F_HEADER,
                        fill=xlsx_mod.FILL_HEADER, kind=xlsx_mod.CellKind.PERCENT),
                  sw.wc("", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER),
                  sw.wc("", font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER)]
    sw.append(total_row)
    for idx, width in enumerate((18, 26, 12, 10, 10, 10)[:len(headers3)], start=2):
        ws3.column_dimensions[get_column_letter(idx)].width = width
    sw.append([None])
    sw.append([None, sw.wc(
        "按弹幕出现的视频时间每 60 秒一档聚合。峰值档位通常对应"
        "「名场面」，可用于定位二次创作与切片素材。",
        font=xlsx_mod.F_CAPTION)])

    if declared_status == "valid":
        claimed_cell = xlsx_mod.checked_cell_value(
            declared_value, xlsx_mod.CellKind.INTEGER, note="声明数量格式异常")
        claimed_quality_status = "口径不可比较"
        coverage_note = "口径不可比较，不计算覆盖率"
    elif declared_status == "invalid":
        claimed_cell = xlsx_mod.cell_value(None, xlsx_mod.CellKind.MISSING,
                                            note="声明数量格式异常")
        claimed_quality_status = "声明数量格式异常"
        coverage_note = "声明数量格式异常，覆盖率不适用"
    else:
        claimed_cell = xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_RETURNED,
                                            note="接口未返回")
        claimed_quality_status = "接口未返回"
        coverage_note = "接口未返回声明数量，覆盖率不适用"
    def q(item, value, unit, note, presentation_state=None):
        return QualityItem("弹幕", item, value, unit, note, presentation_state)
    quality = [
        q("候选记录总数", xlsx_mod.checked_cell_value(len(rows), xlsx_mod.CellKind.INTEGER), "条", "当前明细写入前没有独立候选总数，使用实际解析行作为可观测下界"),
        q("实际写入弹幕明细数", xlsx_mod.checked_cell_value(len(rows), xlsx_mod.CellKind.INTEGER), "条", "按当前导出明细行计数"),
        q("检测到的重复数", xlsx_mod.checked_cell_value(stats.get("duplicates", 0), xlsx_mod.CellKind.INTEGER), "条", "按 (page, id) 去重；单分P等价于 id"),
        q("预计分段数", (xlsx_mod.checked_cell_value(stats["expected_segments"], xlsx_mod.CellKind.INTEGER)
                         if stats.get("expected_segments") is not None else
                         xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_RETURNED, note="接口未返回")), "段", "来自视频时长推算的预计值"),
        q("接口声称弹幕数", claimed_cell, "条", "保留接口原始声明值；缺失时为接口未返回"),
        q("声明数量口径状态", xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_APPLICABLE, note=claimed_quality_status), "状态", "接口计数可能包含各池/各分P，口径不可比较" if claimed_quality_status == "口径不可比较" else claimed_quality_status),
        q("覆盖率", xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_APPLICABLE, note=coverage_note), "状态", "不根据不可比较声明值计算" if claimed_quality_status == "口径不可比较" else coverage_note),
        q("是否取消", xlsx_mod.checked_cell_value(bool(stats.get("cancelled", False)), xlsx_mod.CellKind.BOOLEAN), "状态", "来自 crawler stats", "stop"),
        q("是否截断", xlsx_mod.checked_cell_value(bool(stats.get("truncated", False)), xlsx_mod.CellKind.BOOLEAN), "状态", "来自 crawler stats", "warning"),
        q("是否预算到限", xlsx_mod.checked_cell_value(stats.get("stopped_reason") == "budget_reached", xlsx_mod.CellKind.BOOLEAN), "状态", "来自 stopped_reason", "stop"),
        q("是否部分成功", xlsx_mod.checked_cell_value(bool(rows) and bool(stats.get("cancelled") or stats.get("truncated") or stats.get("stopped_reason")), xlsx_mod.CellKind.BOOLEAN), "状态", "仅在存在有效弹幕且目标/分段未完整结束时为真", "warning"),
    ]
    quality.extend(primary_key_quality(
        "主键", "弹幕主键(page,id)" if multi else "弹幕主键(id)",
        denominator=stats.get("candidate_records"),
        missing=stats.get("missing_id"),
        invalid=stats.get("invalid_id"),
        duplicates=stats.get("duplicates"),
        dedup_discarded=stats.get("dedup_discarded"),
        remaining_conflicts=stats.get("remaining_conflicts"),
        source_note="来自弹幕解析/crawler 结构化统计；不扫描弹幕明细表",
    ))
    fields = []
    for display, stable, dtype, metric in (
        ("序号", "row_number", "整数", "本次输出顺序"), ("分P", "page", "整数", "多分P时的分P号"),
        ("视频内时间", "time", "文本", "进度毫秒格式化为 HH:MM:SS"), ("进度(ms)", "progress_ms", "整数", "视频内进度"),
        ("模式", "mode_label", "文本", "原始模式值映射"), ("正文", "content", "文本", "弹幕用户文本"),
        ("发送时间", "ctime", "日期时间", "Unix 秒转换"), ("用户哈希", "mid_hash", "文本", "接口匿名标识"),
        ("颜色", "color_hex", "文本", "颜色十六进制展示"), ("字号", "fontsize", "整数", "接口返回字号"),
        ("权重", "weight", "整数", "接口返回权重"), ("池", "pool_label", "文本", "原始池值映射"),
        ("属性", "attr", "整数", "接口返回属性"), ("弹幕ID", "id", "ID", "单分P按 id，多分P按 (page,id)"),
    ):
        fields.append(FieldDefinition("弹幕明细", display, stable, dtype, "", "是", "弹幕分段接口/本地解析", metric,
                                      xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_APPLICABLE), "接口未返回"))
    def add_field(sheet, display, stable, dtype, metric):
        fields.append(FieldDefinition(sheet, display, stable, dtype, "", "是", "弹幕接口/本地分析", metric,
                                      xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_APPLICABLE), "接口未返回或该结构未生成"))
    for display, stable, dtype, metric in (
        ("视频标题", "title", "文本", "视频接口标题"), ("BV号 / av号", "video_id", "文本", "视频标识"),
        ("分P", "page_selection", "文本", "本次分P选择"), ("视频时长", "duration", "文本", "视频时长格式化"),
        ("弹幕条数", "danmaku_count", "整数", "本次有效弹幕数"), ("完成情况", "completion", "文本", "crawler 终态说明"),
        ("接口自称弹幕数", "claimed_danmaku", "整数", "接口原始声明值；仅供参考"),
        ("抓取时间", "captured_at", "日期时间", "本地导出时间"),
    ):
        add_field("概览", display, stable, dtype, metric)
    if analysis:
        for display, stable, dtype, metric in (
            ("弹幕条数", "danmaku_count", "整数", "本地分析总数"), ("独立发送者", "unique_senders", "整数", "按 mid_hash 去重"),
            ("Top10发送者占比", "top10_user_ratio", "百分比", "Top10 发送者占全部弹幕比例"),
            ("平均每条字数", "average_length", "小数", "正文长度平均值"), ("非白弹幕", "colored_count", "整数", "颜色非默认白色条数"),
            ("发送时间跨度", "time_span_hours", "小数", "有效发送时间跨度"), ("类别", "category", "文本", "模式/池分类"),
            ("条数", "count", "整数", "分类条数"), ("占比", "ratio", "百分比", "分类条数/分析总数"),
            ("高频弹幕", "top_danmaku", "文本", "重复弹幕文本"), ("次数", "occurrences", "整数", "重复出现次数"),
            ("热词", "top_word", "文本", "高频词"), ("出现在几条弹幕", "message_count", "整数", "包含该词的弹幕条数"),
            ("分钟区间", "minute_range", "文本", "视频内分钟区间"),
            ("条数", "count", "整数", "该分钟弹幕条数"), ("该分钟刷得最多", "top_in_minute", "文本", "分钟内代表性弹幕"),
        ):
            add_field("弹幕分析", display, stable, dtype, metric)
        if multi:
            add_field("弹幕分析", "分P", "page", "文本", "多分P时的分P标识")
    if multi:
        for display, stable, dtype, metric in (
            ("分P", "page", "文本", "分P标识"), ("标题", "part", "文本", "分P标题"),
            ("时长", "duration", "文本", "分P时长"), ("弹幕条数", "danmaku_count", "整数", "分P有效弹幕数"),
            ("段数", "segments", "整数", "分P抓取段数"), ("完成情况", "completion", "文本", "分P终态"),
        ):
            add_field("分P汇总", display, stable, dtype, metric)
    for display, stable, dtype, metric in (
        ("视频内时间", "time", "文本", "视频内时间格式化"), ("该分钟条数", "count", "整数", "该分钟弹幕条数"),
        ("占比", "ratio", "百分比", "该分钟条数/总弹幕数"), ("累计占比", "cumulative_ratio", "百分比", "累计条数/总弹幕数"),
        ("累计条数", "cumulative_count", "整数", "按分钟累计弹幕数"),
    ):
        add_field("密度分布", display, stable, dtype, metric)
    if multi:
        add_field("密度分布", "分P", "page", "文本", "分P标识")
    metadata = make_metadata(
        tool="弹幕", report_type="弹幕分析", parameters={
            "规范化视频 ID": xlsx_mod.cell_value(str(meta.get("bvid") or meta.get("aid") or ""), xlsx_mod.CellKind.ID),
            "分P选择": xlsx_mod.cell_value("全部" if multi else str(meta.get("page", 1)), xlsx_mod.CellKind.TEXT),
            "分段上限": (xlsx_mod.checked_cell_value(stats["max_segments"], xlsx_mod.CellKind.INTEGER)
                         if stats.get("max_segments") is not None else
                         xlsx_mod.cell_value(None, xlsx_mod.CellKind.NOT_APPLICABLE, note="上游未传入分段上限")),
        }, parameter_allowlist=("规范化视频 ID", "分P选择", "分段上限"),
        quality_items=quality, fields=fields)
    write_metadata_sheets(wb, metadata)
    xlsx_mod.save_workbook_atomic(wb, path)
    return path
