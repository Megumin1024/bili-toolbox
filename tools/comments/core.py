# -*- coding: utf-8 -*-
"""评论抓取核心：gRPC 全量抓取（主楼+楼中楼、断点续传）+ 精确分析 + Excel。

- Crawler 的 gRPC metadata 由 core.session.grpc_metadata() 注入（预热 cookie
  伪装）
- 可选实验通道 grpc_tls（Chrome TLS 指纹 gRPC），失败自动回退 grpcio
- Excel 导出基于 core.xlsx 共享基建
- 本模块自建 gRPC 通道、不走 BiliClient，因此**自己过全局闸门**
  （core.gate.shared_gate）：这是全项目请求速率最高的一条流。
"""
import json
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import grpc

from core import xlsx as xlsx_mod
from core.cancel import wait as cancel_wait
from core.gate import shared_gate

from . import grpc_tls
from . import reply_pb2, reply_pb2_grpc

UA_APP = "Mozilla/5.0 BiliDroid/8.16.0 (bbcall@126.com)"
GRPC_HOST = "grpc.biliapi.net:443"

try:  # 有 openpyxl 才能导出
    from openpyxl.utils import get_column_letter
    HAVE_XLSX = True
except ImportError:  # noqa: F841
    HAVE_XLSX = False


def random_jitter():
    return random.random() * 0.1


class TaskCancelled(Exception):
    """表示调用方主动取消当前任务。"""


# ============================ gRPC 抓取 ============================

def norm_grpc(r, root_rpid=None):
    member = r.member
    ctrl = r.reply_control
    return {
        "rpid": r.id,
        "parent": r.parent,
        "root": r.root or (root_rpid or 0),
        "is_main": root_rpid is None,
        "uname": member.name,
        "mid": member.mid,
        "sex": member.sex,
        "level": member.level,
        "vip": member.vip_status,
        "message": r.content.message,
        "like": r.like,
        "rcount": r.count,
        "ctime": r.ctime,
        "location": ctrl.location,
        "is_top": bool(ctrl.is_up_top or ctrl.is_admin_top),
    }


class Crawler:
    """全量评论抓取（gRPC 游客通道，可取消、可限页、断点续传）。"""

    def __init__(self, oid, rtype, out_dir, sleep=0.2, max_pages=0,
                 progress=None, cancel=None, metadata=None, use_tls_grpc=False,
                 gate=None, sleeper=None):
        self.oid, self.rtype = int(oid), int(rtype)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # sleep 是**业务间隔**（秒/页，用户可调）；sleeper 是等待实现，仅供注入。
        self.sleep = sleep
        self._sleep_fn = sleeper or time.sleep
        self.max_pages = int(max_pages or 0)  # 0=不限
        self.progress = progress or (lambda **k: None)
        self.cancel = cancel or (lambda: False)
        # 全局限速 + 熔断：本模块自建 gRPC 通道绕过 BiliClient，须自己过闸门
        # 才能与其它工具共享同一份背压（生产路径共用进程级单例）。
        self.gate = shared_gate() if gate is None else gate
        self.out_path = self.out_dir / "comments.jsonl"
        self.ckpt_path = self.out_dir / "checkpoint.json"
        self.ckpt = self._load_ckpt()
        # ckpt 中的字段是持久化的上次中断标记；以下两个字段只表示本次 crawl()。
        self._run_aborted = False
        self._run_cancelled = False
        self._run_error = None
        self.channel = grpc.secure_channel(GRPC_HOST, grpc.ssl_channel_credentials())
        self.stub = reply_pb2_grpc.ReplyStub(self.channel)
        self.md = metadata or [("user-agent", UA_APP)]
        self._tls_ok = bool(use_tls_grpc)

    # ---- 断点 ----
    def _load_ckpt(self):
        if self.ckpt_path.exists():
            try:
                data = json.loads(self.ckpt_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("aborted", False)
                    data.setdefault("cancelled", False)
                    return data
            except ValueError:
                pass
        return {"phase": "main", "cursor_next": 0, "main_done": 0, "sub_done": 0,
                "pending_roots": [], "next_root_index": 0, "sub_cursor_next": 0,
                "pages": 0, "aborted": False, "cancelled": False}

    def _save_ckpt(self):
        tmp = self.ckpt_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ckpt, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.ckpt_path)

    def _call_tls(self, fn, req):
        if fn is self.stub.MainList:
            path, cls = grpc_tls.PATH_MAIN, reply_pb2.MainListReply
        else:
            path, cls = grpc_tls.PATH_DETAIL, reply_pb2.DetailListReply
        data = grpc_tls.call(path, req.SerializeToString(), self.md)
        return cls.FromString(data)

    def _call(self, fn, req, tries=4):
        last = None
        for i in range(tries):
            if self.cancel():
                raise TaskCancelled()
            if not self.gate.acquire(self.cancel, on_wait=self._log_gate_wait):
                raise TaskCancelled()
            try:
                if self._tls_ok:
                    reply = self._call_tls(fn, req)
                else:
                    reply = fn(req, metadata=self.md, timeout=20)
                self.gate.record_success()
                return reply
            except grpc.RpcError as e:
                last = e
                # 只有明确的服务端限流（RESOURCE_EXHAUSTED）才算"平台在拦我们"，
                # 用来开全局熔断；其余（UNAVAILABLE/DEADLINE_EXCEEDED 等）是传输层
                # 故障，交给本方法自己的退避重试，不能污染熔断状态。
                if e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    self.gate.record_block(f"grpc:{e.code()}")
                else:
                    self.gate.record_neutral()
                wait = min(2 ** i * 2, 30)
                self.progress(level="warn", text=f"gRPC {e.code()}，{wait:.0f}s 后重试")
                # 退避改为可中断：旧实现是 time.sleep 完才检查取消，用户按下停止
                # 后最长仍要空等 30s。
                if not cancel_wait(wait, self.cancel, sleep=self._sleep_fn):
                    raise TaskCancelled()
            except grpc_tls.GrpcTlsError as e:
                self._tls_ok = False  # 实验通道失败即回退 grpcio，不消耗后续重试
                self.gate.record_neutral()
                self.progress(level="warn",
                              text=f"Chrome-TLS gRPC 通道不可用({e})，已回退 grpcio 通道")
        raise RuntimeError(f"gRPC 连续失败: {last}")

    def _pace(self, seconds):
        """业务间隔等待；被取消返回 False（不再靠 sleep 完再检查）。"""
        return cancel_wait(seconds, self.cancel, sleep=self._sleep_fn)

    def _log_gate_wait(self, seconds, reason):
        """闸门即将暂停时写进进度——否则抓取会静默卡住，用户不知道发生了什么。"""
        self.progress(level="warn",
                      text=f"全局闸门：{reason}，暂停 {seconds:.0f}s（全局限速/熔断）")

    def reset(self):
        """清空断点（新一轮抓取）。"""
        self.ckpt = {"phase": "main", "cursor_next": 0, "main_done": 0, "sub_done": 0,
                     "pending_roots": [], "next_root_index": 0, "sub_cursor_next": 0,
                     "pages": 0, "aborted": False, "cancelled": False}
        self._run_aborted = False
        self._run_cancelled = False
        self._run_error = None
        self._save_ckpt()

    def _mark_aborted(self, cancelled=False, error=None):
        """同时保存断点，并区分本次运行状态与断点中的上次中断标记。"""
        self._run_aborted = True
        self._run_cancelled = bool(cancelled)
        self._run_error = str(error) if error is not None else None
        self.ckpt["aborted"] = True
        self.ckpt["cancelled"] = bool(cancelled)
        self._save_ckpt()

    def crawl(self):
        """执行两阶段抓取，返回统计 dict。中断(取消/限页)时标记 aborted。"""
        # 上一次取消/限页/异常只属于旧运行，不能阻止本次从断点继续。
        self._run_aborted = False
        self._run_cancelled = False
        self._run_error = None
        self.ckpt["aborted"] = False
        self.ckpt["cancelled"] = False
        with open(self.out_path, "a", encoding="utf-8") as out:
            self._phase_main(out)
            if not self._run_aborted:
                self._phase_sub(out)
        self.channel.close()
        self._save_ckpt()
        if self._run_cancelled:
            status = "cancelled"
        elif self._run_error:
            status = "error"
        elif self._run_aborted:
            status = "interrupted"
        else:
            status = "completed"
        return {"main": self.ckpt["main_done"], "sub": self.ckpt["sub_done"],
                "pages": self.ckpt["pages"],
                "aborted": self._run_aborted,
                "cancelled": self._run_cancelled,
                "status": status,
                "error": self._run_error}

    def _phase_main(self, out):
        ck = self.ckpt
        if ck["phase"] != "main":
            return
        self.progress(phase="主楼", text=f"从游标 {ck['cursor_next']} 续传")
        while True:
            if self.cancel():
                self._mark_aborted(cancelled=True)
                return
            req = reply_pb2.MainListReq(oid=self.oid, type=self.rtype,
                                        cursor=reply_pb2.CursorReq(next=ck["cursor_next"],
                                                                   mode=2))
            try:
                resp = self._call(self.stub.MainList, req)
            except TaskCancelled:
                self._mark_aborted(cancelled=True)
                return
            except RuntimeError as e:
                self.progress(level="error", text=f"主楼阶段终止: {e}")
                self._mark_aborted(error=f"主楼阶段终止：{e}")
                return
            for r in resp.replies:
                out.write(json.dumps(norm_grpc(r), ensure_ascii=False) + "\n")
                ck["main_done"] += 1
                if r.count > 0:
                    ck["pending_roots"].append([r.id, r.count])
            ck["pages"] += 1
            is_end = resp.cursor.isEnd
            ck["cursor_next"] = resp.cursor.next
            self.progress(phase="主楼", pages=ck["pages"], done=ck["main_done"],
                          next=ck["cursor_next"], is_end=is_end,
                          pending=len(ck["pending_roots"]))
            if self.max_pages and ck["pages"] >= self.max_pages:
                self._mark_aborted()
                return
            if is_end:
                ck["phase"] = "sub"
                self._save_ckpt()
                return
            if not resp.replies:
                ck["phase"] = "sub"
                self._save_ckpt()
                return
            if not self._pace(self.sleep + random_jitter()):
                self._mark_aborted(cancelled=True)
                return

    def _phase_sub(self, out):
        ck = self.ckpt
        idx = ck.get("next_root_index", 0)
        pending = ck.get("pending_roots", [])
        while idx < len(pending):
            if self.cancel():
                self._mark_aborted(cancelled=True)
                return
            rpid, _count = pending[idx]
            sub_next = ck.get("sub_cursor_next", 0)
            while True:
                req = reply_pb2.DetailListReq(oid=self.oid, type=self.rtype, root=rpid,
                                              cursor=reply_pb2.CursorReq(next=sub_next,
                                                                         mode=2))
                try:
                    resp = self._call(self.stub.DetailList, req)
                except TaskCancelled:
                    self._mark_aborted(cancelled=True)
                    return
                except RuntimeError as e:
                    self.progress(level="error", text=f"楼中楼 {rpid} 终止: {e}")
                    self._mark_aborted(error=f"楼中楼 {rpid} 终止：{e}")
                    return
                for sub in (resp.root.replies if resp.root else []):
                    out.write(json.dumps(norm_grpc(sub, root_rpid=rpid),
                                         ensure_ascii=False) + "\n")
                    ck["sub_done"] += 1
                ck["pages"] += 1
                sub_next = resp.cursor.next
                ck["sub_cursor_next"] = sub_next
                self.progress(phase="楼中楼", pages=ck["pages"], done=ck["sub_done"],
                              root_idx=idx + 1, root_total=len(pending))
                if resp.cursor.isEnd:
                    break
                if not self._pace(self.sleep + random_jitter()):
                    self._mark_aborted(cancelled=True)
                    return
            idx += 1
            ck["next_root_index"] = idx
            ck["sub_cursor_next"] = 0
            if self.max_pages and ck["pages"] >= self.max_pages:
                self._mark_aborted()
                return
            if not self._pace(self.sleep):
                self._mark_aborted(cancelled=True)
                return
        self._save_ckpt()


# ============================ 分析 ============================

EMOJI_RE = re.compile(r"\[([^\[\]]{1,12})\]")
STOP = set(("的 了 是 我 你 他 她 它 我们 你们 他们 这那 就是 也 都 还有 但是 因为 所以 如果 现在 "
            "什么 怎么 一个 这个 那个 自己 没有 可以 不是 已经 知道 觉得 时候 直接 还是 只是 真的").split())


def analyze(rows, meta):
    """返回 (报告文本, 概览指标 dict)。"""
    mains = [r for r in rows if r.get("is_main")]
    subs = [r for r in rows if not r.get("is_main")]
    n = len(rows)
    mids = defaultdict(lambda: {"n": 0, "uname": "", "like": 0})
    for r in rows:
        m = mids[r.get("mid")]
        m["n"] += 1
        m["uname"] = r.get("uname")
        m["like"] += r.get("like") or 0
    ts_list = sorted(r["ctime"] for r in rows if r.get("ctime"))
    by_day = Counter(datetime.fromtimestamp(x).strftime("%m-%d") for x in ts_list)
    by_hour = Counter(datetime.fromtimestamp(x).hour for x in ts_list)
    likes_all = sorted((r["like"] for r in rows), reverse=True)
    zero_like = sum(1 for v in likes_all if v == 0)
    vip = sum(1 for r in rows if r.get("vip"))
    levels = Counter(r.get("level") for r in rows)
    emo, uni = Counter(), Counter()
    build_cnt = apology = defense = stop_pay = genshin = spam = 0
    dup = Counter((r["message"] or "").strip() for r in rows)
    SPAM_WORDS = ("代练", "修两层", "接单", "上号", "低价", "变昔涟", "神游")
    for r in rows:
        msg = r["message"] or ""
        for e in set(EMOJI_RE.findall(msg)):
            emo[e] += 1
        for ch in set(c for c in msg if ord(c) > 0x1F000):
            uni[ch] += 1
        build_cnt += ("🧱" in msg or "🔨" in msg)
        apology += ("道歉" in msg)
        defense += ("这么多喷崩铁" in msg or "有什么错" in msg)
        stop_pay += ("停氪" in msg or "退款" in msg)
        genshin += ("原神" in msg)
        spam += any(w in msg for w in SPAM_WORDS)
    toks = Counter()
    for r in rows:
        text = EMOJI_RE.sub("", r["message"] or "")
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            for size in (2, 3, 4):
                for i in range(len(seg) - size + 1):
                    tk = seg[i:i + size]
                    if tk not in STOP and not any(c in tk for c in "的一是在不了有和人这"):
                        toks[tk] += 1
    top_words = []
    for tk, c in toks.most_common(150):
        if any(o != tk and len(o) > len(tk) and tk in o and oc >= c * 0.6
               for o, oc in toks.most_common(150)):
            continue
        top_words.append((tk, c))
        if len(top_words) >= 20:
            break
    hours_span = (max(ts_list) - min(ts_list)) / 3600 if ts_list else 0
    claimed = meta.get("claimed_comment_count") or 0

    kpi = {
        "claimed": claimed, "fetched": n,
        "coverage": f"{n / claimed * 100:.1f}%" if claimed else "-",
        "main": len(mains), "sub": len(subs), "users": len(mids),
        "per_user": f"{n / max(len(mids), 1):.2f}",
        "vip_pct": f"{vip / n * 100:.1f}%", "zero_like_pct": f"{zero_like / n * 100:.1f}%",
        "hours": f"{hours_span:.0f}", "build": build_cnt,
        "build_pct": f"{build_cnt / n * 100:.1f}%",
        "apology": apology, "defense": defense, "stop_pay": stop_pay,
        "genshin": genshin, "spam": spam,
        "by_day": sorted(by_day.items()), "by_hour": sorted(by_hour.items()),
        "top_words": top_words, "emo_top": emo.most_common(10),
        "uni_top": uni.most_common(10),
        "levels": sorted(levels.items(), key=lambda x: (x[0] is None, x[0])),
    }

    top_likes = sorted(rows, key=lambda r: -(r["like"] or 0))[:10]
    top_users = sorted(mids.items(), key=lambda kv: -kv[1]["n"])[:10]
    top_dups = [(m, c) for m, c in dup.most_common(6) if c >= 3][:5]

    L = []
    a = L.append
    a("# 评论精确分析\n")
    a(f"- 对象：{meta.get('author', '')} 《{meta.get('title', '')}》"
      f"（{datetime.fromtimestamp(meta['pub_ts']).strftime('%Y-%m-%d %H:%M') if meta.get('pub_ts') else '-'} 发布）")
    a(f"- B站声称评论总数：{claimed:,}；抓取 {n:,} 条（{kpi['coverage']}）"
      f" = 主楼 {len(mains):,} + 楼中楼 {len(subs):,}\n")
    a("## 用户结构")
    a(f"- 独立用户 {len(mids):,}（人均 {kpi['per_user']}）；大会员 {kpi['vip_pct']}%；"
      f"0赞占比 {kpi['zero_like_pct']}")
    a("- 等级：" + "，".join(f"Lv{k}×{v}" for k, v in kpi["levels"]))
    a("- 刷屏 Top：" + "，".join(f"{m['uname']}({m['n']}条)" for _, m in top_users[:5]) + "\n")
    a("## 时间")
    a("- 按日：" + "，".join(f"{d}×{c:,}" for d, c in kpi["by_day"]))
    a("- 高峰：" + "，".join(f"{h}点" for h, _ in sorted(by_hour.items(), key=lambda x: -x[1])[:3]) + "\n")
    a("## 点赞 Top10")
    for r in top_likes:
        tag = "主" if r.get("is_main") else "楼"
        a(f"- [{tag}] {r['uname']}（赞{r['like']:,}）：{(r['message'] or '')[:60]}")
    a("\n## 表情")
    a("- 小表情：" + "，".join(f"[{k}]{v}条" for k, v in kpi["emo_top"]))
    a("- emoji：" + " ".join(f"{k}{v}条" for k, v in kpi["uni_top"]) + "\n")
    a("## 高频词 Top20")
    a("；".join(f"{k}({v:,})" for k, v in top_words) + "\n")
    a("## 话题与阵营")
    a(f"- 盖楼抗议（🧱🔨）：{build_cnt:,} 条（{kpi['build_pct']}）；要求道歉 {apology:,}；"
      f"护官反呛 {defense:,}；停氪/退款 {stop_pay:,}；提及原神 {genshin:,}；疑似引流 {spam:,}\n")
    a("## 重复复读 Top")
    for m, c in top_dups:
        a(f"- ×{c}：{m[:60]}")
    report = "\n".join(L)
    return report, kpi


# ============================ Excel 导出 ============================

def export_xlsx(rows, meta, kpi, out_path, progress=None):
    if not xlsx_mod or not HAVE_XLSX:
        raise RuntimeError("缺少 openpyxl（pip install openpyxl）")
    n = len(rows)
    wb = xlsx_mod.new_workbook()
    sw = xlsx_mod.SheetWriter(wb)
    W = 8

    # Sheet1 统计概览
    ws = wb.create_sheet("统计概览")
    sw.ws = ws
    sw.title_row(ws, f"评论精确分析 | {meta.get('author', '')} 《{meta.get('title', '')}》", W)
    sw.kv(ws, [
        ("发布时间", datetime.fromtimestamp(meta["pub_ts"]).strftime("%Y-%m-%d %H:%M")
         if meta.get("pub_ts") else "-", meta.get("author", "")),
        ("声称评论总数", meta.get("claimed_comment_count", "-"), "含楼中楼"),
        ("实际抓取", n, f"覆盖率 {kpi['coverage']}（差额=已删除/折叠/不可见）"),
        ("主楼 / 楼中楼", f"{kpi['main']:,} / {kpi['sub']:,}", ""),
        ("独立用户", kpi["users"], f"人均 {kpi['per_user']} 条"),
        ("时间跨度", f"{kpi['hours']} 小时",
         f"{datetime.fromtimestamp(min(r['ctime'] for r in rows if r.get('ctime'))):%m-%d %H:%M}"
         f" ~ {datetime.fromtimestamp(max(r['ctime'] for r in rows if r.get('ctime'))):%m-%d %H:%M}"),
        ("0赞占比 / 大会员", f"{kpi['zero_like_pct']} / {kpi['vip_pct']}", "长尾结构与老玩家占比"),
        ("盖楼抗议（🧱🔨）", f"{kpi['build']:,}（{kpi['build_pct']}）", "复读接龙抗议规模"),
        ("要求道歉 / 护官反呛", f"{kpi['apology']:,} / {kpi['defense']:,}", "阵营对喷比值"),
        ("停氪退款 / 提及原神 / 疑似引流", f"{kpi['stop_pay']:,} / {kpi['genshin']:,} / {kpi['spam']:,}", ""),
    ], W)
    sw.header_row(ws, ["排名", "高频词", "出现次数"])
    for i, (tk, c) in enumerate(kpi["top_words"]):
        ws.append([None, sw.wc(i + 1), sw.wc(tk), sw.wc(c)])
    ws.append([None])
    sw.header_row(ws, ["排名", "B站小表情", "评论数", "", "排名", "emoji", "评论数"])
    for i in range(10):
        e1 = f"[{kpi['emo_top'][i][0]}]" if i < len(kpi["emo_top"]) else ""
        v1 = kpi["emo_top"][i][1] if i < len(kpi["emo_top"]) else ""
        e2 = kpi["uni_top"][i][0] if i < len(kpi["uni_top"]) else ""
        v2 = kpi["uni_top"][i][1] if i < len(kpi["uni_top"]) else ""
        ws.append([None, sw.wc(i + 1), sw.wc(e1), sw.wc(v1), None,
                   sw.wc(i + 1), sw.wc(e2), sw.wc(v2)])
    ws.append([None])
    ws.append([None, sw.wc("口径：游客 gRPC 通道全量抓取；情感/主题为规则词典法粗判。",
                           font=xlsx_mod.F_CAPTION)])

    # Sheet2 按日/按小时
    ws2 = wb.create_sheet("分布统计")
    sw.ws = ws2
    sw.title_row(ws2, "时间分布", 4)
    sw.header_row(ws2, ["日期", "评论条数", "占比"])
    for d, c in kpi["by_day"]:
        ws2.append([None, sw.wc(d), sw.wc(c), sw.wc(f"{c / n * 100:.1f}%")])
    ws2.append([None])
    sw.header_row(ws2, ["小时", "评论条数", "占比"])
    for h, c in kpi["by_hour"]:
        ws2.append([None, sw.wc(f"{h:02d}:00-{h:02d}:59"), sw.wc(c),
                    sw.wc(f"{c / n * 100:.1f}%")])

    # Sheet3 点赞Top100
    ws3 = wb.create_sheet("点赞Top100")
    sw.ws = ws3
    sw.title_row(ws3, "点赞 Top100", 6)
    sw.header_row(ws3, ["排名", "层级", "用户", "点赞", "内容", "楼中楼数"])
    for i, r in enumerate(sorted(rows, key=lambda r: -(r["like"] or 0))[:100]):
        ws3.append([None, sw.wc(i + 1), sw.wc("主楼" if r.get("is_main") else "楼中楼"),
                    sw.wc(r.get("uname")), sw.wc(r.get("like") or 0),
                    sw.wc((r.get("message") or "")[:120]), sw.wc(r.get("rcount") or 0)])

    # Sheet4 全量评论
    ws4 = wb.create_sheet("全量评论")
    sw.ws = ws4
    headers = ["序号", "rpid", "层级", "用户昵称", "用户mid", "等级", "大会员",
               "性别", "评论内容", "点赞数", "楼中楼数", "发布时间", "IP属地"]
    ws4.column_dimensions["A"].width = 3
    for i, h in enumerate(headers):
        ws4.column_dimensions[get_column_letter(i + 2)].width = {
            "序号": 8, "rpid": 16, "层级": 8, "用户昵称": 20, "用户mid": 13, "等级": 6,
            "大会员": 8, "性别": 6, "评论内容": 60, "点赞数": 10, "楼中楼数": 10,
            "发布时间": 17, "IP属地": 12}.get(h, 12)
    ws4.append([None] + [sw.wc(h, font=xlsx_mod.F_HEADER, fill=xlsx_mod.FILL_HEADER,
                                align=xlsx_mod.A_HEADER, border=xlsx_mod.B_HEADER)
                         for h in headers])
    ws4.freeze_panes = "C2"
    ws4.auto_filter.ref = f"B1:{get_column_letter(len(headers) + 1)}{n + 1}"
    for i, r in enumerate(rows):
        if progress and i % 20000 == 0:
            progress(text=f"写入 Excel {i}/{n}")
        dt = datetime.fromtimestamp(r["ctime"]) if r.get("ctime") else None
        ws4.append([None, i + 1, xlsx_mod.clean(r.get("rpid")),
                    "主楼" if r.get("is_main") else "楼中楼",
                    xlsx_mod.clean(r.get("uname")), r.get("mid"), r.get("level"),
                    "是" if r.get("vip") else "否", xlsx_mod.clean(r.get("sex")),
                    xlsx_mod.clean(r.get("message") or ""), r.get("like") or 0,
                    r.get("rcount") or 0, dt,
                    xlsx_mod.clean(str(r.get("location") or "").replace("IP属地：", ""))])
    wb.save(str(out_path))
