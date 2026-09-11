<div align="center">

<img src="assets/icon.png" width="88" alt="BiliToolbox">

# B站工具箱 BiliToolbox

**现代化的 B 站公开数据采集与分析桌面应用**

评论全量抓取 · 视频批量采集 · 实时数据监控 · 任务历史与运行诊断 —— 免登录、开箱即用

[![Release](https://img.shields.io/github/v/release/Megumin1024/bili-toolbox?style=flat-square&color=fb7299)](https://github.com/Megumin1024/bili-toolbox/releases)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-00aeec?style=flat-square)](https://github.com/Megumin1024/bili-toolbox/releases)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue?style=flat-square)](https://www.python.org/)

[功能特性](#-功能特性) · [下载](#-下载) · [快速开始](#-快速开始) · [截图](#-截图) · [扩展开发](#-扩展开发新工具) · [常见问题](#-常见问题)

</div>

---

## ✨ 功能特性

| 工具 | 说明 |
|---|---|
| 💬 **评论抓取** | 输入动态 / 视频链接，通过 gRPC 游客通道抓取**全量评论**（主楼 + 楼中楼），自动生成精确分析报告（用户结构、时间分布、高频词、表情、点赞榜）与多 sheet Excel。支持断点续传 |
| 📊 **视频采集** | 视频 / 收藏夹 / 合集 / 系列 / txt 列表批量采集公开数据（播放、点赞、投币、收藏、分享、弹幕、评论），支持**定时追踪**模式并生成增速榜 Excel |
| 📡 **实时监控** | 单视频实时数据仪表盘：8 项实时指标 + 5 组趋势图表 + 互动率分析，历史数据跨启动续接，适合长时挂机 |
| 🧾 **本地报告中心** | 只读浏览已有评论结果、视频快照和监控历史；支持手动添加两个 JSONL/XLSX 文件进行稳定业务键对比、双时间段统计与安全重新导出 |
| **数据检查与修复** | 检查本地 Excel / JSONL 数据，生成问题清单、修复副本和合并结果，不覆盖原始文件 |
| **监控提醒** | 针对播放量里程碑、增长停滞、异常突增、连续失败和恢复状态提供会话内提醒 |
| 👤 **用户动态** | 输入 UID 或空间链接，抓取该用户的公开动态（图文 / 转发 / 投稿）并导出 Excel |
| 💬 **弹幕抓取/分析** | 输入视频链接抓取弹幕，分析热词、高频弹幕、热点分钟，并导出密度分布 Excel 与原始 JSONL |
| ⚙️ **设置与诊断** | 深色 / 浅色主题、网络通道、代理池和输出目录配置；提供运行环境诊断、最近错误详情、任务历史和任务预设 |

**所有工具免登录使用**。

## 📸 截图

**💬 评论抓取**

![评论抓取](docs/screenshots/comments.png)

**🧾 本地报告中心**

![本地报告中心](docs/screenshots/report-center.png)

**📈 监控仪表盘 · 精确数据**

![监控仪表盘](docs/screenshots/dashboard.png)

## 📥 下载

前往 [**Releases**](https://github.com/Megumin1024/bili-toolbox/releases) 下载免安装版本：

| 包 | 大小 | 说明 |
|---|---|---|
| `BiliToolbox-v*.zip` | ≈45MB | 解压后双击 `B站工具箱.exe` 运行；监控仪表盘自动在系统浏览器中打开 |

> 系统要求：Windows 10 / 11（64 位）

## 🚀 快速开始

```bash
git clone https://github.com/Megumin1024/bili-toolbox.git
cd bili-toolbox
pip install -r requirements.txt
python main.py
```

## 🛠️ 自行打包

```bash
pip install pyinstaller
pyinstaller build_toolbox.spec --noconfirm
```

产物位于 `dist/`，为 onedir 目录，整体打包为 zip 即可分发。

## 📖 使用说明

**评论抓取**：粘贴动态或视频链接（支持 `t.bilibili.com` / `opus` / `BV` / `av` / `b23.tv` 短链 / 纯数字动态 ID），选择输出目录后开始。产物：`评论分析_*.xlsx`（统计概览 / 分布统计 / 点赞 Top100 / 全量评论四个 sheet）、`分析报告.md`、`comments.jsonl` 原始数据。

**视频采集**：每行一个来源，支持视频链接 / BV / av 号、收藏夹 URL（`favlist?fid=`）、合集（`collectiondetail?sid=`）、系列（`seriesdetail?sid=`）与 `.txt` 列表文件。单次快照生成对比总表；定时追踪模式每轮输出增量并生成**增速榜**（播放 / 小时排序）。

**实时监控**：输入 BV 号与采集间隔即可启动，仪表盘会自动在系统浏览器中打开（可点击「在浏览器打开」再次打开）。

**任务预设**：评论抓取、视频采集和实时监控页面可以保存当前参数，之后从「任务预设」直接回填；应用预设只填充表单，不会自动开始任务。

**数据检查与修复**：选择本地 `.xlsx` / `.jsonl` 文件后先生成检查报告；确认需要处理时再生成修复副本，原文件保持不变。

**本地报告中心**：通过「添加文件」手动选择具体 JSONL/XLSX 文件；目录扫描和已有任务/监控历史仅作为补充来源。评论按 `rpid`、视频快照按 `bvid + fetched_at`、监控历史按 `bvid + ts` 对比。缺少可靠业务键时只生成汇总，不伪造逐条匹配；时间统一显示为带 `+08:00` 的本地时间。报告可重新导出为 JSONL/XLSX，重名自动编号且不会覆盖源文件。

**设置与诊断**：在设置页切换主题、配置网络和默认输出目录；运行诊断会检查 Python、依赖、配置目录、输出目录及打包资源。最近错误与任务历史支持查看、复制、清理和复用参数。

## 🧩 扩展新工具

插件式架构：**在 `tools/` 下新建一个包 + 注册表加一行**，即可获得完整的页面、任务线程、进度 / 取消 / 日志 / 结果展示能力，并自动继承整个网络层。

```
bili-toolbox/
├── main.py            # 入口
├── app/               # GUI 壳：主题 / 主窗口 / 任务运行器 / 通用组件
├── core/              # 共享核心：网络层 + 会话门面 + 风控恢复 + 链接解析 + Excel 基建
└── tools/             # 功能包（每个 = spec + page + pipeline + core）
    ├── comments/      # 评论抓取
    ├── collector/     # 视频采集
    ├── monitor/       # 实时监控
    ├── data_check/    # 本地数据检查与修复
    ├── report_center/ # 本地报告中心
    ├── user_dynamics/ # 用户动态
    └── danmaku/       # 弹幕抓取与分析
```

新工具的 `pipeline.py` 只需实现签名为 `run(**kwargs, progress=..., cancel=...)` 的流水线函数，
`page.py` 继承 `app.task_page.TaskPage` 声明表单字段，然后在 `tools/__init__.py` 的
`TOOLS` 列表中注册即可——侧边栏与页面栈自动生成。

## ❓ 常见问题

<details>
<summary><b>请求被拦截（412 / -352）怎么办？</b></summary>
<br>

程序会自动重新预热凭证并按策略重试；若触发 -352 风控挑战且响应携带凭证，会自动打开浏览器进行人工滑块验证，验证通过后任务自动从断点继续，无需重头再跑。

</details>

<details>
<summary><b>代理如何配置？</b></summary>
<br>

设置 → 网络 → 代理池，逗号分隔多个出口：`direct,socks5://127.0.0.1:7890,http://user:pass@1.2.3.4:8080`。失败次数达到阈值的出口自动冷却并轮换到下一个；仅直连时保持 `direct` 或留空。

</details>

<details>
<summary><b>任务中断后需要重跑吗？</b></summary>
<br>

不需要。评论抓取断点记录在 `checkpoint.json`，视频采集快照在 `snapshots.jsonl`，重新运行会自动衔接之前的进度。

</details>

<details>
<summary><b>监控仪表盘在哪里看？</b></summary>
<br>

启动监控后会自动在系统浏览器中打开仪表盘页面（本地地址，端口随机）；之后可随时点击「在浏览器打开」重新打开，停止监控后页面不再刷新。

</details>

## ⚠️ 免责声明

- 本工具**仅采集游客身份可见的公开数据**，不对任何需要登录的数据接口做请求
- 已内置请求限速，请勿修改限速参数进行高频抓取
- 请遵守 B 站用户协议及相关法律法规，**严禁用于刷量、恶意请求等违规用途**
- 本项目仅供学习与技术交流使用，使用本项目产生的任何后果由使用者自行承担

## 📄 许可证

[MIT](LICENSE) © 2026 Megumin1024
