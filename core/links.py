# -*- coding: utf-8 -*-
"""链接/来源解析。

- parse_link / bvid_to_aid / get_dynamic_meta：动态、opus、BV、av、b23 短链
- expand_source / _expand_fav / _expand_season / _expand_series：视频链接、
  收藏夹、合集、系列、.txt 列表文件

所有 API 请求统一走 core.session（四层风控栈）。
"""
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import session

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


def resolve_url(url):
    """b23.tv 短链跟随 30x 解析为最终 URL。"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(urllib.request.Request(url, headers={"User-Agent": UA_WEB}),
                    timeout=10)
        return url
    except urllib.error.HTTPError as e:
        return e.headers.get("Location") or url


# ============================ 动态/视频（评论工具入口） ============================

def parse_link(text):
    """解析输入文本 → (kind, oid, info_dict)。kind: dynamic|video；失败抛 ValueError。"""
    text = (text or "").strip().strip("\"'“”")
    m = re.search(r"https?://[^\s\"'“”]+", text)
    if m:
        text = m.group(0)
    if B23_RE.search(text):
        text = resolve_url(text)
    m = DYN_RE.search(text)
    if m:
        return "dynamic", int(m.group(1)), {"source": text}
    m = BV_RE.search(text)
    if m:
        bvid = m.group(0)
        aid, info = bvid_to_aid(bvid)
        info["source"] = text
        return "video", aid, info
    m = AV_RE.search(text)
    if m:
        return "video", int(m.group(1)), {"source": text}
    if re.fullmatch(r"\d{10,}", text):
        return "dynamic", int(text), {"source": text}
    raise ValueError(f"无法识别的链接或ID: {text[:80]}")


def bvid_to_aid(bvid):
    """BV 号 → (aid, 视频元信息)（web view 接口，游客可用）。"""
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    data = session.http_get_json(url)
    if data.get("code") != 0:
        raise ValueError(f"视频信息获取失败: code={data.get('code')} {data.get('message')}")
    d = data["data"]
    info = {"bvid": bvid, "title": d.get("title", ""),
            "owner": (d.get("owner") or {}).get("name", ""),
            "pubdate": d.get("pubdate"),
            "claimed_comment_count": (d.get("stat") or {}).get("reply")}
    return int(d["aid"]), info


def get_dynamic_meta(dyn_id):
    """动态详情（游客可用）：标题/作者/发布时间/声称评论数。"""
    url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dyn_id}"
    data = session.http_get_json(url)
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


def expand_source(line, progress=None):
    """单行来源 → [(bvid, 备注), ...]。支持视频链接/ID、收藏夹、合集、系列、txt 文件路径。"""
    line = (line or "").strip().strip("\"'“”")
    if not line:
        return []
    p = Path(line)
    if p.suffix.lower() == ".txt" and p.is_file():
        out = []
        for sub in p.read_text(encoding="utf-8").splitlines():
            out += expand_source(sub, progress)
        return out

    text2 = line
    m = re.search(r"https?://[^\s\"'“”]+", text2)
    if m:
        text2 = m.group(0)
    if re.search(r"b23\.tv/", text2, re.I):
        text2 = resolve_url(text2)

    m = FAV_RE.search(text2)
    if m:
        fid = m.group(1) or m.group(2)
        return _expand_fav(fid, progress)
    m = SEASON_RE.search(text2)
    if m and MID_RE.search(text2):
        mid = MID_RE.search(text2).group(1)
        sid = m.group(1) or m.group(2)
        return _expand_season(mid, sid, progress)
    m = SERIES_RE.search(text2)
    if m and MID_RE.search(text2):
        mid = MID_RE.search(text2).group(1)
        return _expand_series(mid, m.group(1), progress)
    if "space.bilibili.com" in text2 and "favlist" not in text2:
        raise ValueError(f"UP主空间链接需要合集/收藏夹参数，或直接给出视频列表"
                         f"（UP全量列表接口需登录态，暂不支持）: {line[:60]}")

    bv = _bv_or_av_from(text2)
    if bv:
        return [(bv, "")]
    raise ValueError(f"无法识别的输入行: {line[:70]}")


def _expand_fav(fid, progress=None):
    out, pn = [], 1
    while pn <= 100:
        r = session.http_get_json(
            f"https://api.bilibili.com/x/v3/fav/resource/list"
            f"?media_id={fid}&pn={pn}&ps=20&order=mtime&type=0&tid=0&platform=web")
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
        time.sleep(0.3 + random.random() * 0.2)
    return out


def _expand_season(mid, sid, progress=None):
    out, pn = [], 1
    while pn <= 200:
        r = session.http_get_json(
            f"https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
            f"?mid={mid}&season_id={sid}&page_num={pn}&page_size=30")
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
        time.sleep(0.3 + random.random() * 0.2)
    return out


def _expand_series(mid, series_id, progress=None):
    out, pn = [], 1
    while pn <= 200:
        r = session.http_get_json(
            f"https://api.bilibili.com/x/series/archives"
            f"?mid={mid}&series_id={series_id}&pn={pn}&ps=30")
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
        time.sleep(0.3 + random.random() * 0.2)
    return out
