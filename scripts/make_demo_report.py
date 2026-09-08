# -*- coding: utf-8 -*-
"""生成演示版评论精确分析 Excel（README 截图用，无真实目标信息）。开发期工具。"""
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.comments.core import analyze, export_xlsx

random.seed(20260906)

N_MAIN, N_SUB = 180, 150
MSGS = [
    "前排围观，质量真的很高", "来了来了，第一时间赶到", "这个转场太丝滑了",
    "收藏了，回头二刷", "BGM 选得真好", "三连走起", "画面质感没得说",
    "已推荐给朋友", "更新很勤，追定了", "细节拉满，逐帧看的",
    "每次都能带来惊喜", "这期剪辑工作量不小吧", "蹲一个下期",
    "弹幕氛围也很好", "看完立刻三连", "讲解思路很清晰",
    "结尾留了伏笔？", "已投两个币", "小破站难得的良心内容", "冲着封面点进来的",
]
UNAME_POOL = ["星河", "晚风", "白桃", "青柠", "拾光", "南栀", "云野", "沐辰",
              "橘猫", "山月", "鹿鸣", "昼行", "灯塔", "落雪", "初晴", "夏至"]
PROVINCES = ["IP属地：广东", "IP属地：江苏", "IP属地：浙江", "IP属地：山东",
             "IP属地：四川", "IP属地：北京", "IP属地：上海", "IP属地：湖北"]

rows = []
base_ts = 1756684800
for i in range(N_MAIN):
    uname = random.choice(UNAME_POOL) + "_" + random.choice("abcxyz0123")
    rows.append({
        "rpid": 900000000 + i, "parent": 0, "root": 0, "is_main": True,
        "uname": uname, "mid": 1000000 + i * 37 % 900000, "sex": random.choice(["男", "女", "保密"]),
        "level": random.choices([2, 3, 4, 5, 6], weights=[8, 22, 34, 26, 10])[0],
        "vip": random.random() < 0.14,
        "message": random.choice(MSGS), "like": int(random.paretovariate(1.6) * 6),
        "rcount": random.choices([0, 1, 2, 3, 5], weights=[55, 20, 12, 8, 5])[0],
        "ctime": base_ts + random.randint(0, 3600 * 72),
        "location": random.choice(PROVINCES), "is_top": i == 0,
    })
for i in range(N_SUB):
    root = random.choice([r["rpid"] for r in rows[:40]])
    rows.append({
        "rpid": 800000000 + i, "parent": root, "root": root, "is_main": False,
        "uname": random.choice(UNAME_POOL) + "_" + random.choice("defuvw456"),
        "mid": 2000000 + i * 53 % 900000, "sex": random.choice(["男", "女", "保密"]),
        "level": random.choices([2, 3, 4, 5, 6], weights=[10, 26, 36, 22, 6])[0],
        "vip": random.random() < 0.1,
        "message": random.choice(MSGS), "like": int(random.paretovariate(2.2) * 3),
        "rcount": 0, "ctime": base_ts + random.randint(0, 3600 * 72),
        "location": random.choice(PROVINCES), "is_top": False,
    })
rows.sort(key=lambda r: r["ctime"])

meta = {"title": "示例视频标题（演示数据）", "author": "UP主", "pub_ts": base_ts - 3600 * 24,
        "claimed_comment_count": 342}
report, kpi = analyze(rows, meta)

out = Path("data/demo_report")
out.mkdir(parents=True, exist_ok=True)
(out / "分析报告.md").write_text(report, encoding="utf-8")
xlsx = out / "评论分析_demo.xlsx"
export_xlsx(rows, meta, kpi, xlsx)
print("demo xlsx ->", xlsx.resolve())
