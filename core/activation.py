# -*- coding: utf-8 -*-
"""buvid 主动激活（gaia ExClimbWuzhi）。

流程：
  1. GET  /x/frontend/finger/spi                       取 buvid3(b_3)/buvid4(b_4)
  2. 生成设备指纹 payload（4位hex键的紧凑 JSON，内容取自会话指纹身份）
  3. buvid_fp = murmur3_x64_128(payload, seed=31) 的 128 位十六进制
  4. 置 cookie: buvid3/buvid4/buvid_fp/b_nut/_uuid
  5. POST /x/internal/gaia-gateway/ExClimbWuzhi，code=0 即激活

payload 由本会话的 BrowserIdentity 生成（UA/屏幕/硬件/时区/WebGL），使激活
指纹与实际请求指纹一致。
"""
import io
import json
import random
import struct
import time
import urllib.parse

SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
EXCLIMBWUZHI_URL = "https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi"

MOD = 1 << 64
_WIN_FONTS = ["Arial", "Arial Black", "Comic Sans MS", "Courier", "Courier New", "Georgia",
              "Impact", "Lucida Console", "Microsoft Sans Serif", "Segoe UI", "SimSun",
              "Tahoma", "Times", "Times New Roman", "Trebuchet MS", "Verdana", "Webdings",
              "Microsoft YaHei", "微软雅黑", "宋体"]
_WEBGL_PARAMS_TEMPLATE = (
    "extensions:ANGLE_instanced_arrays;EXT_blend_minmax;EXT_color_buffer_half_float;"
    "EXT_float_blend;EXT_frag_depth;EXT_texture_compression_bptc;EXT_texture_compression_rgtc;"
    "EXT_texture_filter_anisotropic;OES_element_index_uint;OES_standard_derivatives;"
    "OES_texture_float;OES_texture_float_linear;OES_texture_half_float;"
    "OES_vertex_array_object;WEBGL_color_buffer_float;WEBGL_compressed_texture_astc;"
    "WEBGL_compressed_texture_etc;WEBGL_debug_renderer_info;WEBGL_debug_shaders;"
    "WEBGL_depth_texture;WEBGL_lose_context;WEBGL_multi_draw",
    "webgl aliased line width range:[1, 1]",
    "webgl aliased point size range:[1, 1024]",
    "webgl alpha bits:8",
    "webgl antialiasing:yes",
    "webgl blue bits:8",
    "webgl depth bits:24",
    "webgl green bits:8",
    "webgl max anisotropy:16",
    "webgl max combined texture image units:32",
    "webgl max cube map texture size:16384",
    "webgl max fragment uniform vectors:1024",
    "webgl max render buffer size:16384",
    "webgl max texture image units:16",
    "webgl max texture size:16384",
    "webgl max varying vectors:30",
    "webgl max vertex attribs:16",
    "webgl max vertex texture image units:16",
    "webgl max viewport dims:[32767, 32767]",
    "webgl red bits:8",
    "webgl renderer:WebKit WebGL",
    "webgl shading language version:WebGL GLSL ES 1.0 (1.0)",
    "webgl stencil bits:0",
    "webgl vendor:WebKit",
    "webgl version:WebGL 1.0",
    "webgl unmasked vendor:{vendor}",
    "webgl unmasked renderer:{renderer}",
    "webgl vertex shader high float precision:23",
    "webgl vertex shader high float precision rangeMin:127",
    "webgl vertex shader high float precision rangeMax:127",
    "webgl vertex shader medium float precision:23",
    "webgl vertex shader medium float precision rangeMin:127",
    "webgl vertex shader medium float precision rangeMax:127",
    "webgl vertex shader low float precision:23",
    "webgl vertex shader low float precision rangeMin:127",
    "webgl vertex shader low float precision rangeMax:127",
    "webgl fragment shader high float precision:23",
    "webgl fragment shader high float precision rangeMin:127",
    "webgl fragment shader high float precision rangeMax:127",
    "webgl fragment shader medium float precision:23",
    "webgl fragment shader medium float precision rangeMin:127",
    "webgl fragment shader medium float precision rangeMax:127",
    "webgl fragment shader low float precision:23",
    "webgl fragment shader low float precision rangeMin:127",
    "webgl fragment shader low float precision rangeMax:127",
    "webgl vertex shader high int precision:0",
    "webgl vertex shader high int precision rangeMin:31",
    "webgl vertex shader high int precision rangeMax:30",
    "webgl fragment shader high int precision:0",
    "webgl fragment shader high int precision rangeMin:31",
    "webgl fragment shader high int precision rangeMax:30",
)


# ---------------- murmur3 x64 128（纯标准库实现） ----------------

def _rotate_left(x, k):
    b = bin(x)[2:].rjust(64, "0")
    return int(b[k:] + b[:k], base=2)


def _fmix64(k):
    c1, c2, r = 0xFF51_AFD7_ED55_8CCD, 0xC4CE_B9FE_1A85_EC53, 33
    tmp = k ^ (k >> r)
    tmp = tmp * c1 % MOD
    tmp ^= tmp >> r
    tmp = tmp * c2 % MOD
    tmp ^= tmp >> r
    return tmp


def murmur3_x64_128(source, seed):
    c1 = 0x87C3_7B91_1142_53D5
    c2 = 0x4CF5_AD43_2745_937F
    c3, c4, m = 0x52DC_E729, 0x3849_5AB5, 5
    r1, r2, r3 = 27, 31, 33
    h1 = h2 = seed
    processed = 0
    while True:
        read = source.read(16)
        processed += len(read)
        if len(read) == 16:
            k1 = struct.unpack("<q", read[:8])[0]
            k2 = struct.unpack("<q", read[8:])[0]
            h1 ^= _rotate_left(k1 * c1 % MOD, r2) * c2 % MOD
            h1 = ((_rotate_left(h1, r1) + h2) * m + c3) % MOD
            h2 ^= _rotate_left(k2 * c2 % MOD, r3) * c1 % MOD
            h2 = ((_rotate_left(h2, r2) + h1) * m + c4) % MOD
        elif len(read) == 0:
            h1 ^= processed
            h2 ^= processed
            h1 = (h1 + h2) % MOD
            h2 = (h2 + h1) % MOD
            h1, h2 = _fmix64(h1), _fmix64(h2)
            h1 = (h1 + h2) % MOD
            h2 = (h2 + h1) % MOD
            return (h2 << 64) | h1
        else:
            k1 = k2 = 0
            if len(read) >= 15:
                k2 ^= int(read[14]) << 48
            if len(read) >= 14:
                k2 ^= int(read[13]) << 40
            if len(read) >= 13:
                k2 ^= int(read[12]) << 32
            if len(read) >= 12:
                k2 ^= int(read[11]) << 24
            if len(read) >= 11:
                k2 ^= int(read[10]) << 16
            if len(read) >= 10:
                k2 ^= int(read[9]) << 8
            if len(read) >= 9:
                k2 ^= int(read[8])
                k2 = _rotate_left(k2 * c2 % MOD, r3) * c1 % MOD
                h2 ^= k2
            if len(read) >= 8:
                k1 ^= int(read[7]) << 56
            if len(read) >= 7:
                k1 ^= int(read[6]) << 48
            if len(read) >= 6:
                k1 ^= int(read[5]) << 40
            if len(read) >= 5:
                k1 ^= int(read[4]) << 32
            if len(read) >= 4:
                k1 ^= int(read[3]) << 24
            if len(read) >= 3:
                k1 ^= int(read[2]) << 16
            if len(read) >= 2:
                k1 ^= int(read[1]) << 8
            if len(read) >= 1:
                k1 ^= int(read[0])
            k1 = _rotate_left(k1 * c1 % MOD, r2) * c2 % MOD
            h1 ^= k1


def gen_buvid_fp(key, seed=31):
    m = murmur3_x64_128(io.BytesIO(key.encode("ascii")), seed)
    return f"{hex(m & (MOD - 1))[2:]}{hex(m >> 64)[2:]}"


def gen_uuid_infoc():
    """B站 infoc 风格 uuid：8-4-4-4-12 + 5位毫秒尾数 + 'infoc'。"""
    t = int(time.time() * 1000) % 100000
    mp = list("123456789ABCDEF") + ["10"]
    gen = lambda n: "".join(random.choice(mp) for _ in range(n))  # noqa: E731
    return "-".join(gen(x) for x in (8, 4, 4, 4, 12)) + str(t).ljust(5, "0") + "infoc"


# ---------------- payload 组装（键为B站 gaia 的4位hex字段名） ----------------

def build_payload(identity):
    """由会话指纹身份生成 ExClimbWuzhi 指纹载荷字符串 {"payload": "<紧凑json>"}。"""
    st = identity.state
    win, nav, disp, webgl = st["window"], st["navigator"], st["display"], st["webgl"]
    home = urllib.parse.quote("https://www.bilibili.com/", safe="")
    vendor = webgl["unmaskedVendor"]
    renderer = webgl["unmaskedRenderer"]
    webgl_params = [p.format(vendor=vendor, renderer=renderer)
                    for p in _WEBGL_PARAMS_TEMPLATE]

    content = {
        "3064": 1,
        "5062": int(time.time() * 1000),
        "03bf": home,
        "39c8": "333.788.fp.risk",
        "34f1": "",
        "d402": "",
        "654a": "",
        "6e7c": f'{win["innerWidth"]}x{win["innerHeight"]}',
        "3c43": {
            "2673": 0,
            "5766": disp["colorDepth"],
            "6527": 0,
            "7003": 1,
            "807e": 1,
            "b8ce": nav["userAgent"],
            "641c": 0,
            "07a4": nav["language"],
            "1c57": "not available",
            "0bd0": nav["hardwareConcurrency"],
            "748e": [win["screenWidth"], win["screenHeight"]],
            "d61f": [win["screenAvailWidth"], win["screenAvailHeight"]],
            "fc9d": st["locale"]["timezoneOffset"],
            "6aa9": st["locale"]["timezone"],
            "75b8": 1,
            "3b21": 1,
            "8a1c": 0,
            "d52f": "not available",
            "adca": nav["platform"],
            "80c9": [[
                "PDF Viewer", "Portable Document Format",
                [["application/pdf", "pdf"], ["text/pdf", "pdf"]],
            ], [
                "Chrome PDF Viewer", "Portable Document Format",
                [["application/pdf", "pdf"], ["text/pdf", "pdf"]],
            ], [
                "Chromium PDF Viewer", "Portable Document Format",
                [["application/pdf", "pdf"], ["text/pdf", "pdf"]],
            ], [
                "Microsoft Edge PDF Viewer", "Portable Document Format",
                [["application/pdf", "pdf"], ["text/pdf", "pdf"]],
            ], [
                "WebKit built-in PDF", "Portable Document Format",
                [["application/pdf", "pdf"], ["text/pdf", "pdf"]],
            ]],
            "13ab": "0dAAAAAASUVORK5CYII=",
            "bfe9": "QgAAEIQAACEIAABCCQN4FXANGq7S8KTZayAAAAAElFTkSuQmCC",
            "a3c1": webgl_params,
            "6bc5": f"{vendor}~{renderer}",
            "ed31": 0,
            "72bd": 0,
            "097b": 0,
            "52cd": [0, 0, 0],
            "a658": _WIN_FONTS,
            "d02f": f"{random.uniform(100, 200):.14f}",
        },
        "54ef": '{"in_new_ab":true,"ab_version":{"remove_back_version":"REMOVE",'
                '"login_dialog_version":"V_PLAYER_PLAY_TOAST","open_recommend_blank":"SELF",'
                '"storage_back_btn":"HIDE","call_pc_app":"FORBID","clean_version_old":"GO_NEW",'
                '"optimize_fmp_version":"LOADED_METADATA","for_ai_home_version":"V_OTHER",'
                '"bmg_fallback_version":"DEFAULT","ai_summary_version":"SHOW",'
                '"weixin_popup_block":"ENABLE","rcmd_tab_version":"DISABLE","in_new_ab":true},'
                '"ab_split_num":{"remove_back_version":11,"login_dialog_version":43,'
                '"open_recommend_blank":90,"storage_back_btn":87,"call_pc_app":47,'
                '"clean_version_old":46,"optimize_fmp_version":28,"for_ai_home_version":38,'
                '"bmg_fallback_version":86,"ai_summary_version":466,"weixin_popup_block":45,'
                '"rcmd_tab_version":90,"in_new_ab":0},"pageVersion":"new_video",'
                '"videoGoOldVersion":-1}',
        "8b94": home,
        "df35": gen_uuid_infoc(),
        "07a4": nav["language"],
        "5f45": None,
        "db46": 0,
    }
    inner = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return json.dumps({"payload": inner}, separators=(",", ":"))


# ---------------- 激活入口 ----------------

def activate_via_transport(transport, identity, cookie_store):
    """走指纹通道完成 buvid 激活。返回 {"activated": bool, "buvid3":..., "buvid4":..., "detail":...}。

    成功后把激活过的 buvid 对写回 transport 会话与 cookie_store（键=身份 deviceId）。
    任何失败都不抛出（激活是增强项，不阻塞采集），失败原因放在 detail 里。
    """
    try:
        spi = transport.get_json(SPI_URL)["data"]
        b3, b4 = spi["b_3"], spi["b_4"]
    except Exception as exc:  # noqa: BLE001
        return {"activated": False, "detail": f"spi 获取失败: {exc}"}

    payload = build_payload(identity)
    cookies = {
        "buvid3": b3,
        "buvid4": b4,
        "buvid_fp": gen_buvid_fp(payload, 31),
        "b_nut": "100",
        "_uuid": gen_uuid_infoc(),
    }
    transport.set_cookies(cookies)
    try:
        resp = transport.post_json(
            EXCLIMBWUZHI_URL,
            json_body=payload,
            headers={"Content-Type": "application/json"},
            cookies=cookies,
        )
    except Exception as exc:  # noqa: BLE001
        return {"activated": False, "buvid3": b3, "buvid4": b4,
                "detail": f"ExClimbWuzhi 请求失败: {exc}"}

    code = resp.get("code", resp.get("errno"))
    if code == 0:
        if cookie_store:
            cookie_store.save(identity.device_id, cookies, activated=True)
        return {"activated": True, "buvid3": b3, "buvid4": b4, "detail": "code=0"}
    return {"activated": False, "buvid3": b3, "buvid4": b4, "detail": f"code={code}"}
