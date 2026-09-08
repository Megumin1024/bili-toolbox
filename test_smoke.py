# -*- coding: utf-8 -*-
"""端到端冒烟测试：真实网络验证统一风控栈与三工具流水线。

运行（在仓库根目录）：

    set BILITOOLBOX_TEST_DYNAMIC=<动态链接或纯数字ID>        # 评论区较活跃的任意动态
    set BILITOOLBOX_TEST_VIDEO=<BV号>[,<第二个BV号>]          # 1~2 个任意公开视频
    python test_smoke.py

输出落在 data/ 目录（gitignore），测试完成后可整体删除。
"""
import json
import os
import re
import shutil
import time
import urllib.request


def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"请先设置环境变量 {name}（任意公开目标）")
    return value


DYN_RAW = _require_env("BILITOOLBOX_TEST_DYNAMIC")
VID_RAW = _require_env("BILITOOLBOX_TEST_VIDEO")

_m = re.search(r"(\d{10,})", DYN_RAW)
if not _m:
    raise SystemExit("BILITOOLBOX_TEST_DYNAMIC 未解析出动态 ID")
DYN_ID = int(_m.group(1))
BVIDS = re.findall(r"BV[0-9A-Za-z]{10}", VID_RAW)
if not BVIDS:
    raise SystemExit("BILITOOLBOX_TEST_VIDEO 未解析出 BV 号（逗号分隔 1~2 个）")


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

aid, info = links.bvid_to_aid(BVIDS[0])
print(f"aid={aid} title={info['title'][:40]}", flush=True)

print("== 3. 动态元信息 ==", flush=True)
meta = links.get_dynamic_meta(DYN_ID)
print(f"author={meta['author']} claimed={meta['claimed_comment_count']}", flush=True)

print("== 4. 评论抓取（gRPC，限 3 页）==", flush=True)
shutil.rmtree("data/test_comments", ignore_errors=True)
from tools.comments.pipeline import run_pipeline

res = run_pipeline(str(DYN_ID), "data/test_comments",
                   sleep=0.2, max_pages=3,
                   progress=lambda **k: glog(k.get("text") or str(k)))
print(f"评论: {res['rows']} 条, Excel: {res['xlsx']}", flush=True)
assert res["rows"] > 0

print("== 5. 实验通道：Chrome-TLS gRPC 单次调用 ==", flush=True)
from tools.comments import grpc_tls, reply_pb2

req = reply_pb2.MainListReq(oid=DYN_ID, type=17,
                            cursor=reply_pb2.CursorReq(next=0, mode=2))
try:
    data = grpc_tls.call(grpc_tls.PATH_MAIN, req.SerializeToString(),
                         session.grpc_metadata())
    r = reply_pb2.MainListReply.FromString(data)
    print(f"TLS gRPC OK: {len(r.replies)} 条/页, isEnd={r.cursor.isEnd}", flush=True)
except Exception as e:  # noqa: BLE001
    print(f"TLS gRPC 不可用（符合预期可能性）: {type(e).__name__}: {e}", flush=True)

print("== 6. 视频批量采集（单次快照）==", flush=True)
shutil.rmtree("data/test_collect", ignore_errors=True)
from tools.collector.pipeline import run_pipeline as vp

res2 = vp(BVIDS, "data/test_collect", sleep=0.3,
          progress=lambda **k: glog(k.get("text") or str(k)))
print(f"采集: {res2['videos']} 视频, fail={res2['fail']}, Excel: {res2['xlsx']}", flush=True)
assert res2["videos"] == len(BVIDS)

print("== 7. 监控服务（启动→取一次样本→停止）==", flush=True)
shutil.rmtree("data/test_monitor", ignore_errors=True)
from tools.monitor.server import MonitorServer

srv = MonitorServer(bvid=BVIDS[0], interval=60, data_dir="data/test_monitor",
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
