# -*- coding: utf-8 -*-
"""实验通道：Chrome TLS 指纹 gRPC（gRPC-over-HTTP2 手工帧封装）。

grpcio 的 TLS 栈固定、JA3 可被指纹识别；本模块改用 curl_cffi（Chrome
impersonation，JA3/JA4 真实指纹）直接发 HTTP/2 POST，并手工封装 gRPC 帧：
请求体 = 1 字节压缩标志(0) + 4 字节大端长度 + protobuf；响应按同样帧格式
切分取数据帧。

grpc-status 通常在 HTTP/2 trailers 中，curl 侧不可读，因此以「能否解析出
非空 protobuf 数据帧」为成功判据；任何失败由 Crawler 自动回退 grpcio 通道。
"""
import struct

GRPC_HOST = "https://grpc.biliapi.net"
PATH_MAIN = "/bilibili.main.community.reply.v1.Reply/MainList"
PATH_DETAIL = "/bilibili.main.community.reply.v1.Reply/DetailList"


class GrpcTlsError(Exception):
    pass


_SESSION = None


def _get_session():
    global _SESSION
    if _SESSION is None:
        from curl_cffi import requests as cr
        _SESSION = cr.Session(impersonate="chrome", timeout=20)
    return _SESSION


def call(path, req_bytes, metadata=None):
    """发送一次 gRPC 调用，返回拼接后的 protobuf 数据帧字节。"""
    body = b"\x00" + struct.pack(">I", len(req_bytes)) + req_bytes
    headers = {
        "content-type": "application/grpc",
        "te": "trailers",
        "grpc-accept-encoding": "identity",
    }
    for k, v in (metadata or []):
        headers[k] = v
    try:
        resp = _get_session().post(GRPC_HOST + path, data=body, headers=headers)
    except Exception as exc:  # noqa: BLE001
        raise GrpcTlsError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise GrpcTlsError(f"HTTP {resp.status_code}")
    data = resp.content or b""
    out = bytearray()
    i = 0
    while i + 5 <= len(data):
        flag = data[i]
        length = struct.unpack(">I", data[i + 1:i + 5])[0]
        payload = data[i + 5:i + 5 + length]
        if flag == 0 and payload:
            out += payload
        i += 5 + length
    if not out:
        raise GrpcTlsError("响应无数据帧（可能被服务端拒绝）")
    return bytes(out)
