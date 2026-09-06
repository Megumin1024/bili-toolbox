# -*- coding: utf-8 -*-
"""-352 风控挑战人工恢复（register → 浏览器人工验证 → validate → 通行证注入）。

gaia 端点请求走统一风控栈的当前通道（继承预热 cookie）；验证成功后任务
自动从断点续跑。
"""
import http.server
import json
import threading
import urllib.parse
import webbrowser

GAIA_REGISTER_URL = "https://api.bilibili.com/x/gaia-vgate/v1/register"
GAIA_VALIDATE_URL = "https://api.bilibili.com/x/gaia-vgate/v1/validate"


class RiskChallengeError(Exception):
    """B站 -352 风控挑战，携带 v_voucher，可走人工验证流程恢复。"""

    def __init__(self, v_voucher):
        super().__init__(f"风控挑战(-352)，v_voucher={v_voucher}")
        self.v_voucher = v_voucher


def _log(msg):
    from . import session
    session.log(msg)


def gaia_register(v_voucher):
    """v_voucher → 极验挑战 {token, gt, challenge}。失败返回 None。"""
    from . import session
    try:
        d = session.post_form(GAIA_REGISTER_URL, {"v_voucher": v_voucher})
        if d.get("code") == 0 and (d.get("data") or {}).get("geetest"):
            gd = d["data"]["geetest"]
            return {"token": d["data"]["token"], "gt": gd["gt"],
                    "challenge": gd["challenge"]}
        _log(f"[risk] register 失败: code={d.get('code')} {str(d.get('message'))[:60]}")
    except Exception as exc:  # noqa: BLE001
        _log(f"[risk] register 异常: {exc}")
    return None


def gaia_validate(token, challenge, validate, seccode):
    """验证结果 → grisk_id（即 gaia_vtoken）。失败返回 None。"""
    from . import session
    try:
        d = session.post_form(GAIA_VALIDATE_URL, {
            "challenge": challenge, "token": token,
            "validate": validate, "seccode": seccode})
        grisk = (d.get("data") or {}).get("grisk_id")
        return grisk or None
    except Exception as exc:  # noqa: BLE001
        _log(f"[risk] validate 异常: {exc}")
        return None


_CAPTCHA_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>B站风控验证</title></head>
<body style="font-family:'Microsoft YaHei',sans-serif;background:#f7f7f5;text-align:center;padding-top:48px">
<h2>检测到B站风控挑战</h2><p>请完成下方滑块验证，成功后工具会自动继续抓取</p>
<div id="cap" style="display:inline-block;padding:12px"></div>
<p id="status" style="color:#1b7d46"></p>
<script src="https://static.geetest.com/static/tools/gt.js"></script>
<script>
initGeetest({gt: __GT__, challenge: __CH__, offline: false, new_captcha: true,
             product: "bind", width: "300px"}, function (captchaObj) {
  captchaObj.onReady(function () { captchaObj.verify(); });
  captchaObj.onSuccess(function () {
    var res = captchaObj.getValidate();
    document.getElementById("status").innerText = "验证成功，正在返回工具…（可关闭此页）";
    location.href = "/done?challenge=" + encodeURIComponent(res.geetest_challenge)
      + "&validate=" + encodeURIComponent(res.geetest_validate)
      + "&seccode=" + encodeURIComponent(res.geetest_seccode);
  });
});
</script></body></html>"""


def solve_captcha_in_browser(gt, challenge, timeout=300, progress=None):
    """本机起一个验证页（极验 gt.js）+ 打开系统浏览器等待人工完成。

    返回 {challenge, validate, seccode}；超时/失败返回 None。零第三方依赖。
    """
    result = {}
    done = threading.Event()
    html = _CAPTCHA_HTML.replace("__GT__", json.dumps(gt)).replace(
        "__CH__", json.dumps(challenge))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/done"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                for k in ("challenge", "validate", "seccode"):
                    if qs.get(k):
                        result[k] = qs[k][0]
                body = "验证成功，请返回工具继续（本页可关闭）"
                done.set()
            else:
                body = html
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *a):  # noqa: N802
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    if progress:
        progress(text=f"打开浏览器进行人工验证（{timeout}秒内完成）: {url}")
    webbrowser.open(url)
    ok = done.wait(timeout)
    server.shutdown()
    return result if (ok and result.get("validate")) else None


def risk_recovery_flow(v_voucher, progress=None, timeout=300):
    """P2 恢复编排：register → 浏览器人工验证 → validate → 注入 gaia_vtoken。

    返回 grisk_id（成功）或 None。供工具流水线调用；成功后后续请求自动附带
    通行证参数与 cookie，任务从断点继续。
    """

    def p(text):
        if progress:
            progress(level="warn", text=text)

    p("检测到B站风控挑战(-352)，启动人工验证恢复流程…")
    info = gaia_register(v_voucher)
    if not info:
        p("register 失败（该风控可能无法通过验证码解除）")
        return None
    p("已获取极验挑战，正在打开浏览器…")
    res = solve_captcha_in_browser(info["gt"], info["challenge"],
                                   timeout=timeout, progress=p)
    if not res:
        p("超时未完成人工验证")
        return None
    grisk = gaia_validate(info["token"], res["challenge"], res["validate"],
                          res["seccode"])
    if grisk:
        from . import session
        session.set_gaia_vtoken(grisk)
        p("风控验证成功，已获得通行证（gaia_vtoken），继续抓取")
        return grisk
    p("validate 未返回 grisk_id")
    return None
