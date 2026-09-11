# -*- coding: utf-8 -*-
"""监控服务：采集线程 + 本地 HTTP 仪表盘（标准库实现）。

MonitorServer 生命周期：
- 每实例独立 history 文件（按 BV 号），重启自动续接趋势
- 网络层复用 core 的 BiliClient（通道降级/凭证预热激活/代理池），与采集
  工具共用 cookie 存储与身份管理
- 端口默认随机分配（0）

采集:  /x/web-interface/view       播放/弹幕/评论/点赞/投币/收藏/分享
       /x/player/online/total      各分P实时"正在看"人数
服务:  /                仪表盘页面（tools/monitor/static）
       /api/latest      最新样本 + 视频元信息 + 网络通道状态
       /api/history     全部历史样本
"""
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.client import BiliClient
from core.proxy import ProxyPool
from core.redact import sanitize_text

STATIC_DIR = Path(__file__).resolve().parent / "static"

MAX_SAMPLES = 50000  # 内存/加载上限，防止长期挂机无限膨胀

VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
ONLINE_API = "https://api.bilibili.com/x/player/online/total?bvid={bvid}&cid={cid}"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def to_int(v):
    """解析整数值；兼容 B站返回的 '1000+' 等区间字符串（取下界）。"""
    m = re.match(r"\s*(\d+)", str(v))
    return int(m.group(1)) if m else None


class _State:
    def __init__(self, bvid, interval):
        self.lock = threading.Lock()
        self.bvid = bvid
        self.interval = interval
        self.meta = None
        self.samples = []
        self.last_ok_ts = None
        self.last_error = None


class MonitorServer:
    """一个实例 = 一个视频的采集 + 仪表盘服务。start() 后通过 url 访问。"""

    def __init__(self, bvid, interval=60, transport="auto", proxy_spec=None,
                 data_dir=None, cookie_path=None, log=None,
                 event_callback=None, session_id=None):
        self.bvid = bvid
        self.interval = max(5, min(3600, int(interval)))
        self.log = log or (lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}",
                                             flush=True))
        self.event_callback = event_callback
        self.session_id = str(session_id or uuid.uuid4().hex)
        self.data_dir = Path(data_dir) if data_dir else Path("data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.data_dir / f"history_{bvid}.jsonl"
        self.state = _State(bvid, self.interval)
        self.pool = ProxyPool(proxy_spec or None)
        self.client = BiliClient(self.pool, preferred_transport=transport,
                                 cookie_path=str(cookie_path) if cookie_path else None,
                                 log=self.log)
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    def _emit_event(self, event):
        """把安全事件交给页面；回调异常绝不能影响采集循环。"""
        if self.event_callback is None:
            return
        try:
            self.event_callback(dict(event))
        except Exception:  # noqa: BLE001 - 外部回调隔离且不得触碰 Qt
            pass

    # ---------- 采集 ----------

    def _http_json(self, url, retries=3):
        return self.client.fetch_json(url, retries=retries)

    def collect_once(self, bvid):
        """采集一次，返回 (meta, sample)。"""
        view = self._http_json(VIEW_API.format(bvid=bvid))["data"]
        stat = view["stat"]

        pages = []
        for p in view.get("pages", []):
            pages.append({
                "cid": p["cid"],
                "page": p.get("page", len(pages) + 1),
                "part": p.get("part") or f"P{p.get('page', len(pages) + 1)}",
                "duration": p.get("duration"),
            })

        meta = {
            "bvid": view["bvid"],
            "aid": view.get("aid"),
            "title": view["title"],
            "owner": view.get("owner", {}).get("name", ""),
            "owner_mid": view.get("owner", {}).get("mid"),
            "pubdate": view.get("pubdate"),
            "duration": view.get("duration"),
            "desc": (view.get("desc") or "").strip(),
            "pic": view.get("pic"),
            "videos": view.get("videos", len(pages)),
            "pages": pages,
        }

        online_pages = []
        for p in pages:
            od = None
            try:
                od = self._http_json(ONLINE_API.format(bvid=bvid, cid=p["cid"]),
                                     retries=2)["data"]
            except Exception as exc:  # noqa: BLE001 - 单个分P失败不拖垮整个样本
                self.log(f"在线接口失败 cid={p['cid']}: {exc}")
            online_pages.append({
                "cid": p["cid"],
                "part": p["part"],
                "total": to_int(od.get("total")) if od else None,
                "count": to_int(od.get("count")) if od else None,
            })

        sample = {
            "ts": int(time.time()),
            "bvid": bvid,
            "view": stat.get("view"),
            "danmaku": stat.get("danmaku"),
            "reply": stat.get("reply"),
            "favorite": stat.get("favorite"),
            "coin": stat.get("coin"),
            "share": stat.get("share"),
            "like": stat.get("like"),
            # 页面默认展示 P1（中文版）在线人数，与B站播放页口径一致
            "online": online_pages[0]["total"] if online_pages else None,
            "online_pages": online_pages,
        }
        return meta, sample

    def _load_history(self):
        if not self.history_file.is_file():
            return
        loaded = 0
        try:
            with open(self.history_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("bvid") == self.bvid:
                        self.state.samples.append(item)
                        loaded += 1
        except OSError as exc:
            self.log(f"读取历史数据失败: {exc}")
        if len(self.state.samples) > MAX_SAMPLES:
            self.state.samples = self.state.samples[-MAX_SAMPLES:]
        if loaded:
            self.state.last_ok_ts = self.state.samples[-1]["ts"]
            self.log(f"已加载历史样本 {loaded} 条")

    def _append_sample(self, sample):
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample, ensure_ascii=False) + "\n")

    def _poller_loop(self):
        fails = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                meta, sample = self.collect_once(self.bvid)
                with self.state.lock:
                    self.state.meta = meta
                    self.state.samples.append(sample)
                    if len(self.state.samples) > MAX_SAMPLES:
                        self.state.samples = self.state.samples[-MAX_SAMPLES:]
                    self.state.last_ok_ts = sample["ts"]
                    self.state.last_error = None
                try:
                    self._append_sample(sample)
                except OSError as exc:
                    self.log(f"写入历史数据失败: {exc}")
                fails = 0
                self.log(f"采集成功 播放={sample['view']} 点赞={sample['like']} "
                         f"正在看={sample['online']} | "
                         f"通道={self.client.stats['last_transport']} "
                         f"{self.client.stats['last_latency_ms']}ms")
                self._emit_event({
                    "type": "sample_success",
                    "session_id": self.session_id,
                    "ts": sample.get("ts"),
                    "view": sample.get("view"),
                })
            except Exception as exc:  # noqa: BLE001
                fails += 1
                # last_error 会经 GET /api/latest 离开进程，出口处强制脱敏
                with self.state.lock:
                    self.state.last_error = sanitize_text(
                        f"{type(exc).__name__}: {exc}")
                self.log(f"采集失败(连续{fails}次): {sanitize_text(exc)}")
                self._emit_event({
                    "type": "sample_failure",
                    "session_id": self.session_id,
                    "ts": int(time.time()),
                    "consecutive_failures": fails,
                })

            sleep_for = min(15 * fails, self.interval) if fails else self.interval
            end = time.time() + max(0.0, sleep_for - (time.time() - started))
            while time.time() < end and not self._stop.is_set():
                time.sleep(min(1.0, max(0.05, end - time.time())))

    # ---------- HTTP 服务 ----------

    def _make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BiliToolbox-Monitor/1.0"

            def log_message(self, fmt, *args):  # 静默访问日志
                pass

            def _send_bytes(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send_bytes(code, "application/json; charset=utf-8",
                                 json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def _static(self, rel):
                root = STATIC_DIR.resolve()
                fp = (root / rel).resolve()
                if not str(fp).startswith(str(root)) or not fp.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                ctype = CONTENT_TYPES.get(fp.suffix.lower(),
                                          "application/octet-stream")
                try:
                    self._send_bytes(200, ctype, fp.read_bytes())
                except OSError:
                    self._json({"error": "read failed"}, 500)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._static("index.html")
                elif path.startswith("/vendor/"):
                    self._static(path[1:])  # 含目录穿越防护
                elif path == "/api/latest":
                    self._api_latest()
                elif path == "/api/history":
                    self._api_history()
                else:
                    self._json({"error": "not found"}, 404)

            def _api_latest(self):
                st = app.state
                with st.lock:
                    samples = st.samples
                    payload = {
                        "ok": bool(samples) and st.last_error is None,
                        "server_time": int(time.time()),
                        "interval": st.interval,
                        "bvid": st.bvid,
                        "meta": st.meta,
                        "latest": samples[-1] if samples else None,
                        "session_start_ts": samples[0]["ts"] if samples else None,
                        "sample_count": len(samples),
                        "last_ok_ts": st.last_ok_ts,
                        "error": st.last_error,
                    }
                try:
                    payload["net"] = app.client.info()
                except Exception:  # noqa: BLE001 - 面板信息不因统计失败而失败
                    payload["net"] = None
                self._json(payload)

            def _api_history(self):
                with app.state.lock:
                    payload = {"samples": list(app.state.samples)}
                self._json(payload)

        return Handler

    # ---------- 生命周期 ----------

    @property
    def running(self):
        return self._server is not None

    @property
    def url(self):
        if self._server is None:
            return ""
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def start(self):
        if self.running:
            return self.url
        self._stop.clear()
        self._load_history()
        threading.Thread(target=self._poller_loop, daemon=True).start()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.log(f"监控 {self.bvid} | 间隔 {self.interval}s | "
                 f"代理池 {self.pool.status()['size']} 项 | {self.url}")
        return self.url

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass
        self.log("监控已停止")
