# -*- coding: utf-8 -*-
"""端到端冒烟测试：真实网络验证统一风控栈与三工具流水线。

运行（在仓库根目录）: python test_smoke.py
输出落在 data/ 目录（gitignore），测试完成后可整体删除。
"""
import json
import shutil
import time
import urllib.request


def glog(msg):
    print(f"[T] {msg}", flush=True)


from core import session

session.configure(proxy_spec=None, transport="auto",
                  cookie_path="data/test_cookies.json", log_fn=glog)

print("== 1. 凭证预热（spi + bili_ticket + buvid 激活）==", flush=True)
ck = session.ensure_ready()
print(f"cookie 串长度: {len(ck)}", flush=True)
assert "buvid3" in ck, "预热未取得 buvid3"

print("== 2. 四层栈 HTTP：视频元信息 ==", flush=True)
from core import links

aid, info = links.bvid_to_aid("BV1zi7Y6BEdS")
print(f"aid={aid} title={info['title'][:40]}", flush=True)

print("== 3. 动态元信息 ==", flush=True)
meta = links.get_dynamic_meta(1242626908874604548)
print(f"author={meta['author']} claimed={meta['claimed_comment_count']}", flush=True)

print("== 4. 评论抓取（gRPC，限 3 页）==", flush=True)
shutil.rmtree("data/test_comments", ignore_errors=True)
from tools.comments.pipeline import run_pipeline

res = run_pipeline("https://t.bilibili.com/1242626908874604548", "data/test_comments",
                   sleep=0.2, max_pages=3,
                   progress=lambda **k: glog(k.get("text") or str(k)))
print(f"评论: {res['rows']} 条, Excel: {res['xlsx']}", flush=True)
assert res["rows"] > 0

print("== 5. 实验通道：Chrome-TLS gRPC 单次调用 ==", flush=True)
from tools.comments import grpc_tls, reply_pb2

req = reply_pb2.MainListReq(oid=1242626908874604548, type=17,
                            cursor=reply_pb2.CursorReq(next=0, mode=2))
try:
    data = grpc_tls.call(grpc_tls.PATH_MAIN, req.SerializeToString(),
                         session.grpc_metadata())
    r = reply_pb2.MainListReply.FromString(data)
    print(f"TLS gRPC OK: {len(r.replies)} 条/页, isEnd={r.cursor.isEnd}", flush=True)
except Exception as e:  # noqa: BLE001
    print(f"TLS gRPC 不可用（符合预期可能性）: {type(e).__name__}: {e}", flush=True)

print("== 6. 视频批量采集（单次快照 2 视频）==", flush=True)
shutil.rmtree("data/test_collect", ignore_errors=True)
from tools.collector.pipeline import run_pipeline as vp

res2 = vp(["BV1zi7Y6BEdS", "BV1GJ411x7h7"], "data/test_collect", sleep=0.3,
          progress=lambda **k: glog(k.get("text") or str(k)))
print(f"采集: {res2['videos']} 视频, fail={res2['fail']}, Excel: {res2['xlsx']}", flush=True)
assert res2["videos"] == 2

print("== 7. 监控服务（启动→取一次样本→停止）==", flush=True)
shutil.rmtree("data/test_monitor", ignore_errors=True)
from tools.monitor.server import MonitorServer

srv = MonitorServer(bvid="BV1zi7Y6BEdS", interval=60, data_dir="data/test_monitor",
                    cookie_path="data/test_cookies.json", log=glog)
url = srv.start()
time.sleep(6)
with urllib.request.urlopen(url + "api/latest", timeout=5) as resp:
    d = json.loads(resp.read())
print(f"监控 ok={d['ok']} view={d['latest']['view'] if d['latest'] else None}", flush=True)
with urllib.request.urlopen(url + "/", timeout=5) as resp:
    html = resp.read().decode("utf-8", "ignore")
print(f"仪表盘 HTML {len(html)} 字节, 含 echarts: {'echarts' in html.lower()}", flush=True)
srv.stop()

print("\n=== SMOKE ALL PASS ===", flush=True)
