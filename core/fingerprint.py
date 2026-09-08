# -*- coding: utf-8 -*-
"""会话级浏览器指纹身份。

每次进程启动生成一套内部自洽的随机指纹：窗口/屏幕/硬件参数、Chrome UA
（版本池 124~131、构建号随机）、WebGL 渲染器、canvas 哈希、语言时区等，
并由 build_headers_from_browser_state 生成成套请求头。随机化防跨请求聚类，
自洽性（UA ↔ sec-ch-ua ↔ platform ↔ impersonate 目标）防特征矛盾。
"""
import random
import secrets

CHROME_MAJOR_POOL = [124, 125, 126, 127, 128, 129, 130, 131]

# 与 UA 大版本对应的 curl_cffi impersonate 目标（运行时探测可用性后取交集）
IMPERSONATE_BY_MAJOR = {
    124: "chrome124",
    131: "chrome131",
    120: "chrome120",
    116: "chrome116",
    110: "chrome110",
}

SCREEN_POOL = [(1920, 1080), (2560, 1440), (1366, 768), (1600, 900), (2192, 1232)]
DPR_POOL = [1.0, 1.0, 1.25, 1.5, 2.0]
CPU_POOL = [4, 6, 8, 8, 12, 16]
MEM_POOL = [4, 8, 8, 16]

WEBGL_PROFILES = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER (0x000021C4) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00009BC8) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]


def random_hex(length):
    return secrets.token_hex((length + 1) // 2)[:length]


def build_chrome_user_agent(chrome_major=None, os_name="windows"):
    """Chrome UA：主版本 + 随机构建号，形如 Chrome/124.0.6325.119。"""
    if chrome_major is None:
        chrome_major = random.choice(CHROME_MAJOR_POOL)
    chrome_version = f"{chrome_major}.0.{random.randint(6000, 6900)}.{random.randint(80, 180)}"
    if os_name == "windows":
        system = "Windows NT 10.0; Win64; x64"
    elif os_name == "macos":
        system = random.choice([
            "Macintosh; Intel Mac OS X 10_15_7",
            "Macintosh; Intel Mac OS X 13_6_1",
            "Macintosh; Intel Mac OS X 14_5_0",
        ])
    else:
        system = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({system}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36"), chrome_major


def _ua_major(user_agent):
    try:
        return int(user_agent.split("Chrome/")[1].split(".")[0])
    except (IndexError, ValueError):
        return 126


def _build_sec_ch_ua(user_agent):
    major = _ua_major(user_agent)
    if "Edg/" in user_agent:
        return f'"Microsoft Edge";v="{major}", "Chromium";v="{major}", "Not/A)Brand";v="24"'
    return f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not/A)Brand";v="8"'


def _build_sec_ch_ua_platform(platform):
    return {"Win32": '"Windows"', "MacIntel": '"macOS"'}.get(platform, '"Linux"')


def generate_browser_fingerprint_state():
    """生成一套自洽的随机指纹状态（窗口/显示/导航/时区/WebGL/canvas/存储）。"""
    screen_w, screen_h = random.choice(SCREEN_POOL)
    dpr = random.choice(DPR_POOL)
    inner_w = screen_w - random.choice([0, 16, 24])
    inner_h = screen_h - random.choice([72, 80, 88, 120])
    ua, major = build_chrome_user_agent()
    webgl_vendor, webgl_renderer = random.choice(WEBGL_PROFILES)
    languages = ["zh-CN", "zh", "en"]

    state = {
        "window": {
            "scrollX": 0, "scrollY": 0,
            "innerWidth": inner_w, "innerHeight": inner_h,
            "outerWidth": screen_w, "outerHeight": screen_h,
            "screenX": 0, "screenY": 0,
            "screenWidth": screen_w, "screenHeight": screen_h,
            "screenAvailWidth": screen_w, "screenAvailHeight": screen_h - 40,
        },
        "display": {"devicePixelRatio": dpr, "colorDepth": 24, "pixelDepth": 24},
        "navigator": {
            "userAgent": ua,
            "appCodeName": "Mozilla",
            "appName": "Netscape",
            "appVersion": ua.split("Mozilla/", 1)[-1],
            "platform": "Win32",
            "product": "Gecko",
            "productSub": "20030107",
            "vendor": "",
            "vendorSub": "",
            "language": "zh-CN",
            "languages": languages,
            "cookieEnabled": True,
            "hardwareConcurrency": random.choice(CPU_POOL),
            "deviceMemory": random.choice(MEM_POOL),
            "maxTouchPoints": 0,
            "webdriver": False,
        },
        "locale": {"locale": "zh-CN", "timezone": "Asia/Shanghai", "timezoneOffset": -480},
        "location": {
            "href": "https://www.bilibili.com/", "origin": "https://www.bilibili.com",
            "protocol": "https:", "host": "www.bilibili.com", "hostname": "www.bilibili.com",
            "port": "", "pathname": "/", "search": "", "hash": "",
            "hrefLength": 30, "historyLength": random.randint(1, 6),
        },
        "webgl": {
            "vendor": webgl_vendor,
            "renderer": webgl_renderer,
            "unmaskedVendor": webgl_vendor,
            "unmaskedRenderer": webgl_renderer,
        },
        "canvas": {"winding": random.choice(["yes", "no"]), "x64hash128": random_hex(32)},
        "storage": {"localStorage": {}, "sessionStorage": {}, "cookies": {}},
    }
    return state


def finalize_device_id(raw_device_id):
    """32 位 hex 中随机一位替换为随机 hex 字符，得到最终 deviceId。"""
    raw = (raw_device_id or "").strip()
    if len(raw) < 1:
        raw = random_hex(32)
    idx = random.randrange(len(raw))
    return raw[:idx] + random_hex(1) + raw[idx + 1:]


def build_headers_from_browser_state(state, *, referer="https://www.bilibili.com/",
                                     origin="https://www.bilibili.com",
                                     content_type=None):
    """成套请求头配方（GET 场景去掉 content-type，保留 priority）。"""
    navigator = state["navigator"]
    user_agent = navigator["userAgent"]
    langs = navigator.get("languages") or ["zh-CN"]
    accept_language = ",".join(
        f"{lang};q={max(1.0 - i * 0.1, 0.1):.1f}" if i else lang
        for i, lang in enumerate(langs)
    )
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": accept_language,
        "referer": referer,
        "origin": origin,
        "priority": "u=1, i",
        "user-agent": user_agent,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "sec-ch-ua": _build_sec_ch_ua(user_agent),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": _build_sec_ch_ua_platform(navigator.get("platform", "Win32")),
    }
    if content_type:
        headers["content-type"] = content_type
    cookie_header = "; ".join(f"{k}={v}" for k, v in state["storage"].get("cookies", {}).items())
    if cookie_header:
        headers["cookie"] = cookie_header
    return headers


class BrowserIdentity:
    """一次进程生命周期的浏览器身份：指纹状态 + deviceId + 成套头部。

    impersonate 目标由 transport 按本身份的 chrome_major 探测注入。
    """

    def __init__(self):
        self.state = generate_browser_fingerprint_state()
        self.device_id = finalize_device_id(random_hex(32))
        self.chrome_major = _ua_major(self.state["navigator"]["userAgent"])
        self.browser_headers = build_headers_from_browser_state(self.state)
        self.impersonate = None  # transport 探测后回填

    def summary(self):
        nav = self.state["navigator"]
        renderer = self.state["webgl"]["renderer"]
        gpu = renderer.split(", ")[1] if ", " in renderer else renderer
        gpu = gpu.split(" (0x")[0]
        return {
            "chrome_major": self.chrome_major,
            "user_agent": nav["userAgent"],
            "device_id": self.device_id,
            "screen": f'{self.state["window"]["screenWidth"]}x{self.state["window"]["screenHeight"]}@{self.state["display"]["devicePixelRatio"]}',
            "cpu_cores": nav["hardwareConcurrency"],
            "webgl": gpu,
            "canvas_hash": self.state["canvas"]["x64hash128"][:8] + "…",
        }
