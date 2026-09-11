# -*- coding: utf-8 -*-
"""弹幕分析：热词 / 高频弹幕 / 时间轴热点。

纯计算——不吃网络、不碰文件、不依赖 Qt。给它一批已抓到的弹幕行，返回一份
可渲染的结果；Excel 与 Markdown 两个出口都从这一份结果出发，免得两边各算
一遍，口径悄悄跑偏。

三条口径值得单独说：

1. **高频弹幕与热词是全部分P合并统计**。问"什么话被刷得最多"，答案是整场
   加起来 500 次；按分P拆开反而失真。
2. **热点分钟按 (分P, 分钟) 分开**。每个分P都从 00:00 重新计时，合并会把
   不同分P的同一分钟混成一档。
3. **用户统计只做单分P**。mid_hash 取自各 cid 的分段响应，跨分P是否同一个
   盐未经证实；没证实的事不合并——多分P时该块留空并写明原因。
"""
from collections import Counter, defaultdict
from datetime import datetime

from core import text as text_mod

from . import core

HIGH_FREQ_LIMIT = 20          # 高频弹幕榜长度
HIGH_FREQ_MIN_COUNT = 3       # 进高频榜的最低出现次数
WORD_LIMIT = 20               # 热词榜长度
WORD_MIN_COUNT = 2
HOT_MINUTE_LIMIT = 15         # 热点分钟榜长度
MINUTE_EXAMPLE_LIMIT = 2      # 每个热点分钟给几句"当时在刷什么"
MINUTE_EXAMPLE_MIN_COUNT = 2

DASH = "—"


def analyze(rows, meta=None, parts=None):
    """弹幕行 → 分析结果 dict。空输入返回一份结构完整的空结果，不抛异常。

    行里缺字段一律按缺失处理（旧 jsonl、调用方手搓的行都可能没有 page 或
    mode_label），报表不该因为一行脏数据整张炸掉。
    """
    rows = list(rows or [])
    meta = meta or {}
    total = len(rows)
    pages = {int(r.get("page") or 1) for r in rows}
    multi = bool(parts) or len(pages) > 1

    contents = Counter()
    words = Counter()
    for r in rows:
        text = text_mod.normalize(r.get("content"))
        if text_mod.is_meaningful(text):
            contents[text] += 1
        words.update(text_mod.tokenize(r.get("content")))

    top_danmaku = [(c, n, _pct(n, total))
                   for c, n in text_mod.ranked(contents, HIGH_FREQ_MIN_COUNT,
                                               HIGH_FREQ_LIMIT)]
    top_words = text_mod.top_phrases(words, WORD_LIMIT, WORD_MIN_COUNT)

    users = top10_pct = None
    users_note = ""
    if multi:
        users_note = ("多分P不合并用户统计：mid_hash 取自各 cid 的分段响应，"
                      "跨分P是否同一个盐未经证实，硬合会得出假结论。")
    else:
        hashes = Counter(r.get("mid_hash") for r in rows if r.get("mid_hash"))
        users = len(hashes)
        if hashes and total:
            top10_pct = _pct(sum(n for _, n in hashes.most_common(10)), total)

    return {
        "total": total,
        "multi": multi,
        "kpi": _kpi(rows, total),
        "top_danmaku": top_danmaku,
        "top_words": top_words,
        "hot_minutes": _hot_minutes(rows, multi, total),
        "users": users,
        "top10_user_pct": top10_pct,
        "users_note": users_note,
        # 门槛跟着结果一起走：Excel 里那句"样本太少"要引用它，让渲染层自己
        # 去 import 本模块会绕成环（本模块 import core）。
        "high_freq_min_count": HIGH_FREQ_MIN_COUNT,
        "word_min_count": WORD_MIN_COUNT,
    }


# ---------- 各块 ----------

def _kpi(rows, total):
    modes = Counter(r.get("mode_label") or DASH for r in rows)
    pools = Counter(r.get("pool_label") or DASH for r in rows)
    lengths = [len(text_mod.normalize(r.get("content"))) for r in rows]
    ctimes = sorted(int(r.get("ctime") or 0) for r in rows if r.get("ctime"))
    colored = sum(1 for r in rows
                  if str(r.get("color_hex") or "").upper() not in ("", "#FFFFFF"))
    return {
        "modes": [(k, v, _pct(v, total)) for k, v in text_mod.ranked(modes, 0)],
        "pools": [(k, v, _pct(v, total)) for k, v in text_mod.ranked(pools, 0)],
        "colored": colored,
        "colored_pct": _pct(colored, total),
        "avg_len": (sum(lengths) / len(lengths)) if lengths else 0.0,
        "span_hours": ((ctimes[-1] - ctimes[0]) / 3600.0 if len(ctimes) >= 2
                       else 0.0),
        "first_sent": _ts(ctimes[0]) if ctimes else DASH,
        "last_sent": _ts(ctimes[-1]) if ctimes else DASH,
    }


def _hot_minutes(rows, multi, total):
    """最密的 N 个分钟，附上"那一分钟被刷得最多的两句原话"。

    只报条数没用——"第 87 分钟很密"谁都能从密度表看出来；配上当时刷的那句话,
    才知道那是个什么场面。
    """
    buckets = core.page_minute_buckets(rows) if multi else core.minute_buckets(rows)
    per_minute = defaultdict(Counter)
    for r in rows:
        text = text_mod.normalize(r.get("content"))
        if text_mod.is_meaningful(text):
            per_minute[_bucket_key(r, multi)][text] += 1

    out = []
    for key, count in sorted(buckets, key=lambda kv: (-kv[1], kv[0]))[:HOT_MINUTE_LIMIT]:
        page, minute = key if multi else (1, key)
        examples = text_mod.ranked(per_minute.get(key, Counter()),
                                   MINUTE_EXAMPLE_MIN_COUNT, MINUTE_EXAMPLE_LIMIT)
        out.append({
            "page": page,
            "minute": minute,
            "time": f"{core.hhmmss(minute * core.MINUTE_MS)} – "
                    f"{core.hhmmss((minute + 1) * core.MINUTE_MS)}",
            "count": count,
            "pct": _pct(count, total),
            "examples": [f"{c}（{n}次）" for c, n in examples],
        })
    return out


# ---------- 渲染 ----------

def render_markdown(analysis, meta=None):
    """分析结果 → Markdown 报告文本。

    任何来自数据的值都要过 _s()：None 一旦漏进去，用户看到的就是
    "预计约 None 段"那类文案（上一轮踩过的坑）。
    """
    analysis = analysis or {}
    meta = meta or {}
    total = int(analysis.get("total") or 0)
    kpi = analysis.get("kpi") or {}
    multi = bool(analysis.get("multi"))
    label = meta.get("bvid") or (f"av{meta.get('aid')}" if meta.get("aid") else "未知视频")

    L = []
    a = L.append
    a(f"# 弹幕分析报告 · {label}\n")
    a("| 项 | 值 |")
    a("| --- | --- |")
    a(f"| 视频标题 | {_s(meta.get('title'))} |")
    a(f"| UP主 | {_s(meta.get('owner'))} |")
    a(f"| 弹幕条数 | {total:,} |")
    a(f"| 统计范围 | {'全部分P合并' if multi else '单分P'} |")
    a(f"| 独立发送者 | {_users_cell(analysis)} |")
    a(f"| 平均每条字数 | {kpi.get('avg_len', 0):.1f} |")
    a(f"| 非白弹幕 | {int(kpi.get('colored') or 0):,}（{_pct_text(kpi.get('colored_pct'))}） |")
    a(f"| 发送时间跨度 | {kpi.get('span_hours', 0):.1f} 小时"
      f"（{_s(kpi.get('first_sent'))} ~ {_s(kpi.get('last_sent'))}） |")
    a("")
    if analysis.get("users_note"):
        a(f"> {analysis['users_note']}\n")

    n_dm = len(analysis.get("top_danmaku") or [])
    a(f"## 高频弹幕 Top {n_dm}" if n_dm else "## 高频弹幕")
    a("")
    if analysis.get("top_danmaku"):
        a("| 弹幕 | 次数 | 占比 |")
        a("| --- | ---: | ---: |")
        for content, count, pct in analysis["top_danmaku"]:
            a(f"| {_md_cell(content)} | {count:,} | {pct:.1f}% |")
    else:
        a(f"没有出现达到 {HIGH_FREQ_MIN_COUNT} 次的重复弹幕。")
    a("")

    n_w = len(analysis.get("top_words") or [])
    a(f"## 热词 Top {n_w}" if n_w else "## 热词")
    a("")
    if analysis.get("top_words"):
        a("| 词 | 出现在几条弹幕 |")
        a("| --- | ---: |")
        for word, count in analysis["top_words"]:
            a(f"| {_md_cell(word)} | {count:,} |")
    else:
        a(f"没有出现达到 {WORD_MIN_COUNT} 次的高频用词。")
    a("")

    n_m = len(analysis.get("hot_minutes") or [])
    a(f"## 热点分钟 Top {n_m}" if n_m else "## 热点分钟")
    a("")
    if analysis.get("hot_minutes"):
        head = "| 分P | 分钟区间 | 条数 | 占比 | 该分钟刷得最多 |" if multi else \
               "| 分钟区间 | 条数 | 占比 | 该分钟刷得最多 |"
        rule = "| --- | --- | ---: | ---: | --- |" if multi else \
               "| --- | ---: | ---: | --- |"
        a(head)
        a(rule)
        for m in analysis["hot_minutes"]:
            ex = "；".join(_md_cell(e) for e in m["examples"]) or "（该分钟没有重复内容）"
            if multi:
                a(f"| P{m['page']} | {m['time']} | {m['count']:,} | {m['pct']:.1f}% | {ex} |")
            else:
                a(f"| {m['time']} | {m['count']:,} | {m['pct']:.1f}% | {ex} |")
    else:
        a("没有弹幕，无法给出热点分钟。")
    a("")

    a("## 构成")
    a("")
    if kpi.get("modes"):
        a("| 弹幕模式 | 条数 | 占比 |")
        a("| --- | ---: | ---: |")
        for k, v, p in kpi["modes"]:
            a(f"| {_md_cell(k)} | {v:,} | {p:.1f}% |")
    else:
        a("无弹幕，没有构成数据。")
    a("")
    if kpi.get("pools"):
        a("| 弹幕池 | 条数 | 占比 |")
        a("| --- | ---: | ---: |")
        for k, v, p in kpi["pools"]:
            a(f"| {_md_cell(k)} | {v:,} | {p:.1f}% |")
    else:
        a("无弹幕，没有构成数据。")
    a("")
    a("---")
    a("")
    a("口径：纯本地统计，本报告不产生任何网络请求。"
      + ("高频弹幕与热词跨全部分P合并统计；" if multi
         else "高频弹幕与热词按单分P统计；")
      + "热点分钟按 (分P, 分钟) 分开，各分P都从 00:00 重新计时。"
        "热词由中文 2/3/4 字滑窗 n-gram 加停用词过滤得到，非严格分词，"
        "可能出现不成词的片段；计的是「出现在多少条弹幕里」，同一条里重复不累加。")
    a("")
    return "\n".join(L)


# ---------- 小工具 ----------

def _pct(part, whole):
    return part / whole * 100.0 if whole else 0.0


def _pct_text(value):
    return f"{value:.1f}%" if isinstance(value, (int, float)) else DASH


def _ts(epoch):
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return DASH


def _s(value, dash=DASH):
    """用户可见文本里绝不放 None / 空白。"""
    if value is None:
        return dash
    text = str(value).strip()
    return text or dash


def _md_cell(value):
    """表格单元格：竖线会破坏 Markdown 表格，换行会破坏行结构。"""
    return _s(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _users_cell(analysis):
    if analysis.get("users") is None:
        return DASH
    pct = _pct_text(analysis.get("top10_user_pct"))
    return (f"{int(analysis['users']):,} 人，Top10 占 {pct}"
            if pct != DASH else f"{int(analysis['users']):,} 人")


def _bucket_key(row, multi):
    minute = int(row.get("progress_ms") or 0) // core.MINUTE_MS
    return (int(row.get("page") or 1), minute) if multi else minute
