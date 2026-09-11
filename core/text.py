# -*- coding: utf-8 -*-
"""中文短文本分词与词频统计（弹幕/评论共用）。

弹幕极短（平均十字以内），而且信息量最大的恰恰是 "666" "yyds" "awsl"
这类非中文串——上 jieba 那种分词器在这里得不偿失：凭空多一个依赖，还会
把这些串切碎或当成噪声过滤掉。所以这里用滑窗 n-gram + 停用词 + 长词抑制，
纯标准库、可离线测试、同样输入结果可复现。

**本模块是唯一正本**。tools/comments/core.py 里另有一份历史私有实现
（EMOJI_RE / STOP + 内联分词），它的输出被现有测试锁着，迁移属于单独一轮，
不要顺手改——顺手改等于给自己制造回归面。
"""
import re
from collections import Counter

# [doge] 这类表情标记：热词榜里不算"词"（高频弹幕榜按原文精确匹配，那里
# 照样会统计它），所以分词前先摘掉。
EMOJI_RE = re.compile(r"\[([^\[\]]{1,12})\]")

# 中文停用词。只收高频虚词/代词——弹幕里"哈哈""牛逼"这类是要上榜的，
# 不能按"语气词"一刀切掉。
STOP = frozenset((
    "的 了 是 我 你 他 她 它 我们 你们 他们 这那 就是 也 都 还有 但是 因为 "
    "所以 如果 现在 什么 怎么 一个 这个 那个 自己 没有 可以 不是 已经 知道 "
    "觉得 时候 直接 还是 只是 真的 一下 这么 那么 不能 不会 有人 这里 那里 "
    "为啥 为什么 怎么办 有没有 这种 那种 感觉 有点 其实 应该 可能").split())

# 落在 n-gram 里就整条丢弃的填充字。滑窗会把"我真的觉得"切成"真的""的觉"
# 这种半截词，光靠停用词表挡不住（"真的"在表里，但"真的觉"不在），所以再
# 加一层字符黑名单：n-gram 里含这些字就丢掉。
#
# 刻意**不收**"不""有""人""很""大"这些——它们是好词的骨架（不错、有点、
# 人物、很棒），为了少几个半截词把真词一起误杀得不偿失。
_FILLER_CHARS = frozenset("的一是在了和这那我你他她它着吗呢吧啊呀哦嗯")

_CJK_RE = re.compile("[\\u4e00-\\u9fff]+")
_ALNUM_RE = re.compile(r"[0-9A-Za-z]{2,}")
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize(value):
    """去首尾空白 + 内部连续空白压成一个空格。统计与展示共用这一份口径，
    免得同一个字符串在两处被判成不同内容。"""
    return _WS_RE.sub(" ", str(value if value is not None else "")).strip()


def is_meaningful(token):
    """够格进榜吗：长度 ≥ 2，且不是纯标点/纯空白。"""
    token = token or ""
    return len(token) >= 2 and not _PUNCT_ONLY_RE.match(token)


def tokenize(value, sizes=(2, 3, 4)):
    """一段文本 → 候选词频 Counter。

    中文走滑窗 n-gram（默认 2/3/4 字）；英文与数字串整段保留——"yyds"
    "666" 整串才是弹幕里的那个梗，切成 "yy"+"ds" 就没意义了。

    **同一个词在一段文本里只计一次**（去重后再计数）。滑窗会在同一串字符里
    重复命中同一个 n-gram："哈哈哈哈" 一次就贡献两个 "哈哈哈"，次数直接翻倍，
    结果 "哈哈哈" 反而压过 "哈哈哈哈" 排到前面去——榜单一读就是错的。改成
    按条去重后，词频的含义变成"出现在多少条弹幕里"，跟高频弹幕榜的"多少条
    是这句话"口径一致，两张表能对着看。
    """
    counter = Counter()
    body = EMOJI_RE.sub(" ", str(value if value is not None else ""))
    for run in _CJK_RE.findall(body):
        for size in sizes:
            if len(run) < size:
                continue
            for i in range(len(run) - size + 1):
                tok = run[i:i + size]
                if tok in STOP or any(c in _FILLER_CHARS for c in tok):
                    continue
                counter[tok] += 1
    for run in _ALNUM_RE.findall(body):
        tok = run.lower()
        if is_meaningful(tok):
            counter[tok] += 1
    # 同段文本内的重复只算一次
    for tok in counter:
        counter[tok] = 1
    return counter


def ranked(counter, min_count=2, limit=None):
    """按 (-次数, 词) 稳定排序后取前 limit 个。

    排序键带上词本身，是为了让同频次的输出顺序稳定——只按次数排序时并列项
    的先后取决于 Counter 的插入顺序（也就是原文本里谁先出现），同一份数据
    换个输入顺序结果就变，测试会飘。
    """
    items = sorted(((t, c) for t, c in (counter or {}).items() if c >= min_count),
                   key=lambda kv: (-kv[1], kv[0]))
    return items[:limit] if limit else items


def top_phrases(counter, limit=20, min_count=2, window=150, suppress_ratio=0.6):
    """词频 → 榜单，做长词抑制后取前 limit 个。

    长词抑制：滑窗天然会把"前方高能"同时切出"前方""高能""方高能"……不抑制
    的话一张榜上会出现同一句话的好几个碎片。规则沿用评论工具里已经跑出效果
    的做法：某个候选被一个更长、且同样高频（≥60%）的候选包含时，丢掉短的。
    """
    head = ranked(counter, min_count, window)
    out = []
    for tok, count in head:
        if any(o != tok and len(o) > len(tok) and tok in o
               and oc >= count * suppress_ratio
               for o, oc in head):
            continue
        out.append((tok, count))
        if len(out) >= limit:
            break
    return out
