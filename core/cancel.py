# -*- coding: utf-8 -*-
"""任务取消信号与可中断等待。

与 core.net_errors 的 ErrorKind 刻意分开：取消是调用方主动行为，不是网络故障，
不应计入任何失败统计，也不应触发重试退避或代理轮换。
"""
import time

# 取消检查粒度：长等待切成小段，保证取消在约 0.25s 内生效。
CANCEL_POLL_SLICE = 0.25


class TaskCancelledError(Exception):
    """调用方已请求取消当前任务。"""


def is_cancelled(cancel):
    """cancel 谓词为真即视为已取消；cancel 为 None 时永不取消。"""
    return bool(cancel()) if cancel else False


def wait(seconds, cancel=None, sleep=None, slice_seconds=CANCEL_POLL_SLICE):
    """可中断的分段等待；被取消返回 False。

    按切片累计而非读取真实时钟，因此注入 no-op sleep 的测试不会空转；
    sleep 在调用时解析（而非定义时绑定），patch time.sleep 依然生效。
    """
    if is_cancelled(cancel):
        return False
    sleep = sleep or time.sleep
    slept = 0.0
    while slept < seconds:
        if is_cancelled(cancel):
            return False
        step = min(slice_seconds, seconds - slept)
        sleep(step)
        slept += step
    return not is_cancelled(cancel)
