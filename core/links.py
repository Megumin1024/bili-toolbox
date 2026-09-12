# -*- coding: utf-8 -*-
"""链接/来源解析。

- parse_link / bvid_to_aid / get_dynamic_meta：动态、opus、BV、av、b23 短链
- expand_source / _expand_fav / _expand_season / _expand_series：视频链接、
  收藏夹、合集、系列、.txt 列表文件

所有 API 请求统一走 core.session（四层风控栈）；resolve_url 的 30x 直连也
先过全局闸门（shared_gate），页间等待走 core.cancel.wait，全链路可取消。
"""
import random
import re
import urllib.error
import urllib.request
from pathlib import Path

from . import session
from .cancel import TaskCancelledError, is_cancelled, wait as cancel_wait
from .gate import shared_gate

UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

B23_RE = re.compile(r"b23\.tv/", re.I)
DYN_RE = re.compile(r"(?:t\.bilibili\.com|bilibili\.com/opus(?:/v2)?)/(\d{10,})", re.I)
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.I)  # BV号区分大小写，匹配保留原样
AV_RE = re.compile(r"av(\d{4,})", re.I)
FAV_RE = re.compile(r"media_id=(\d+)|fid=(\d+)", re.I)
SEASON_RE = re.compile(r"[?&]sid=(\d+)|season_id=(\d+)", re.I)
SERIES_RE = re.compile(r"series_id=(\d+)", re.I)
MID_RE = re.compile(r"space\.bilibili\.com/(\d+)", re.I)


def _log_gate_wait(seconds, reason):
    """闸门即将暂停时把它写进日志——否则任务会静默卡住（与 client 同一范式）。"""
    session.log(f"[gate] {reason}，暂停 {seconds:.0f}s（全局限速/熔断）")


def resolve_url(url, cancel=None):
    """b23.tv 短链跟随 30x 解析为最终 URL。

    单次直连（10s 超时、固定 UA，无重试/代理/指纹等任何通道升级）。发请求前
    过全局闸门：发起前与全局限速/熔断冷却的等待中均可被 cancel 打断（抛
    TaskCancelledError，不计入任何失败统计）；拿到响应（含 30x）回报
    record_success，其余异常回报 record_neutral 后原样上抛——半开探针恰好
    回报一次，与 client 的 reported 标志同一契约。
    """
    if is_cancelled(cancel):
        raise TaskCancelledError()
    gate = shared_gate()
    if not gate.acquire(cancel, on_wait=_log_gate_wait):
        raise TaskCancelledError()

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    reported = False
    try:
        opener.open(urllib.request.Request(url, headers={"User-Agent": UA_WEB}),
                    timeout=10)
        gate.record_success()
        reported = True
        return url
    except urllib.error.HTTPError as e:
        gate.record_success()
        reported = True
        return e.headers.get("Location") or url
    finally:
        if not reported:
            gate.record_neutral()


# ============================ 动态/视频（评论工具入口） ============================

def parse_link(text, cancel=None):
    """解析输入文本 → (kind, oid, info_dict)。kind: dynamic|video；失败抛 ValueError。

    cancel 仅作用于 b23 短链解析（resolve_url）的闸门等待。
    """
    text = (text or "").strip().strip("\"'“”")
    m = re.search(r"https?://[^\s\"'“”]+", text)
    if m:
        text = m.group(0)
    if B23_RE.search(text):
        text = resolve_url(text, cancel=cancel)
    m = DYN_RE.search(text)
    if m:
        return "dynamic", int(m.group(1)), {"source": text}
    m = BV_RE.search(text)
    if m:
        bvid = m.group(0)
        aid, info = bvid_to_aid(bvid, cancel=cancel)
        info["source"] = text
        return "video", aid, info
    m = AV_RE.search(text)
    if m:
        return "video", int(m.group(1)), {"source": text}
    if re.fullmatch(r"\d{10,}", text):
        return "dynamic", int(text), {"source": text}
    raise ValueError(f"无法识别的链接或ID: {text[:80]}")


def bvid_to_aid(bvid, cancel=None):
    """BV 号 → (aid, 视频元信息)（web view 接口，游客可用）。

    cancel 透传统一会话：闸门冷却/退避等待可被取消打断。
    """
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    data = session.http_get_json(url, cancel=cancel)
    if data.get("code") != 0:
        raise ValueError(f"视频信息获取失败: code={data.get('code')} {data.get('message')}")
    d = data["data"]
    info = {"bvid": bvid, "title": d.get("title", ""),
            "owner": (d.get("owner") or {}).get("name", ""),
            "pubdate": d.get("pubdate"),
            "claimed_comment_count": (d.get("stat") or {}).get("reply")}
    return int(d["aid"]), info


def get_dynamic_meta(dyn_id, cancel=None):
    """动态详情（游客可用）：标题/作者/发布时间/声称评论数。cancel 透传统一会话。"""
    url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dyn_id}"
    data = session.http_get_json(url, cancel=cancel)
    if data.get("code") != 0:
        raise ValueError(f"动态信息获取失败: code={data.get('code')} {data.get('message')}")
    item = (data.get("data") or {}).get("item") or {}
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    md = modules.get("module_dynamic") or {}
    major = md.get("major") or {}
    archive = major.get("archive") or {}
    opus = major.get("opus") or {}
    stat = modules.get("module_stat") or {}
    return {
        "title": archive.get("title") or opus.get("title") or "",
        "text": (md.get("desc") or {}).get("text", "")[:200],
        "author": author.get("name", ""),
        "pub_ts": author.get("pub_ts"),
        "claimed_comment_count": (stat.get("comment") or {}).get("count"),
    }


# ============================ 批量来源（采集工具入口） ============================

def _bv_or_av_from(text):
    m = re.search(r"(?:BV[0-9A-Za-z]{10})|(?:av(\d{4,}))", text, re.I)
    if not m:
        return None
    if m.group(1):
        return "av" + m.group(1)
    return m.group(0)


def expand_source(line, progress=None, cancel=None, budget=None):
    """单行来源 → [(bvid, 备注), ...]。支持视频链接/ID、收藏夹、合集、系列、txt 文件路径。

    cancel 为取消谓词：页间等待被打断时返回已展开的部分（不是失败）；
    cancel/budget 透传给页间等待与经 session 发起的展开请求（budget 为
    core.budget.TaskBudget，透传即计入调用方任务预算）。
    """
    line = (line or "").strip().strip("\"'“”")
    if not line:
        return []
    p = Path(line)
    if p.suffix.lower() == ".txt" and p.is_file():
        out = []
        for sub in p.read_text(encoding="utf-8").splitlines():
            out += expand_source(sub, progress, cancel=cancel, budget=budget)
        return out

    text2 = line
    m = re.search(r"https?://[^\s\"'“”]+", text2)
    if m:
        text2 = m.group(0)
    if re.search(r"b23\.tv/", text2, re.I):
        text2 = resolve_url(text2, cancel=cancel)

    m = FAV_RE.search(text2)
    if m:
        fid = m.group(1) or m.group(2)
        return _expand_fav(fid, progress, cancel=cancel, budget=budget)
    m = SEASON_RE.search(text2)
    if m and MID_RE.search(text2):
        mid = MID_RE.search(text2).group(1)
        sid = m.group(1) or m.group(2)
        return _expand_season(mid, sid, progress, cancel=cancel, budget=budget)
    m = SERIES_RE.search(text2)
    if m and MID_RE.search(text2):
        mid = MID_RE.search(text2).group(1)
        return _expand_series(mid, m.group(1), progress, cancel=cancel,
                              budget=budget)
    if "space.bilibili.com" in text2 and "favlist" not in text2:
        raise ValueError(f"UP主空间链接需要合集/收藏夹参数，或直接给出视频列表"
                         f"（UP全量列表接口需登录态，暂不支持）: {line[:60]}")

    bv = _bv_or_av_from(text2)
    if bv:
        return [(bv, "")]
    raise ValueError(f"无法识别的输入行: {line[:70]}")


def _expand_fav(fid, progress=None, cancel=None, budget=None):
    out, pn = [], 1
    while pn <= 100:
        r = session.http_get_json(
            f"https://api.bilibili.com/x/v3/fav/resource/list"
            f"?media_id={fid}&pn={pn}&ps=20&order=mtime&type=0&tid=0&platform=web",
            cancel=cancel, budget=budget)
        if r.get("code") != 0:
            raise ValueError(f"收藏夹 {fid} 获取失败: code={r.get('code')} {r.get('message')}")
        medias = (r.get("data") or {}).get("medias") or []
        for v in medias:
            if v.get("bv"):
                out.append((v["bv"], v.get("title", "")))
        info = (r.get("data") or {}).get("info") or {}
        if progress:
            progress(text=f"收藏夹 {fid}: 第{pn}页，累计 {len(out)} 个视频")
        if len(out) >= ((info.get("media_count") or 0)) or len(medias) < 20:
            break
        pn += 1
        if not cancel_wait(0.3 + random.random() * 0.2, cancel):
            break  # 页间等待被取消：返回已展开的部分，不是失败
    return out


def _expand_season(mid, sid, progress=None, cancel=None, budget=None):
    out, pn = [], 1
    while pn <= 200:
        r = session.http_get_json(
            f"https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
            f"?mid={mid}&season_id={sid}&page_num={pn}&page_size=30",
            cancel=cancel, budget=budget)
        if r.get("code") != 0:
            raise ValueError(f"合集 {sid} 获取失败: code={r.get('code')} {r.get('message')}")
        items = (r.get("data") or {}).get("items") or {}
        archives = items.get("archives") or []
        for v in archives:
            if v.get("bvid"):
                out.append((v["bvid"], v.get("title", "")))
        if progress:
            progress(text=f"合集 {sid}: 第{pn}页，累计 {len(out)} 个视频")
        if not archives or len(archives) < 30:
            break
        pn += 1
        if not cancel_wait(0.3 + random.random() * 0.2, cancel):
            break  # 页间等待被取消：返回已展开的部分，不是失败
    return out


def _expand_series(mid, series_id, progress=None, cancel=None, budget=None):
    out, pn = [], 1
    while pn <= 200:
        r = session.http_get_json(
            f"https://api.bilibili.com/x/series/archives"
            f"?mid={mid}&series_id={series_id}&pn={pn}&ps=30",
            cancel=cancel, budget=budget)
        if r.get("code") != 0:
            raise ValueError(f"系列 {series_id} 获取失败: code={r.get('code')} {r.get('message')}")
        archives = ((r.get("data") or {}).get("archives")) or []
        for v in archives:
            if v.get("bvid"):
                out.append((v["bvid"], v.get("title", "")))
        if progress:
            progress(text=f"系列 {series_id}: 第{pn}页，累计 {len(out)} 个视频")
        if not archives or len(archives) < 30:
            break
        pn += 1
        if not cancel_wait(0.3 + random.random() * 0.2, cancel):
            break  # 页间等待被取消：返回已展开的部分，不是失败
    return out
