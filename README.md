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
| ⚙️ **设置与诊断** | 深色 / 浅色主题、网络通道、代理池和输出目录配置；提供运行环境诊断、最近错误详情和可复用的任务历史 |

**所有工具免登录使用**，内置统一的抗干扰网络层（见[下文](#-网络层设计)）。

## 📸 截图

**💬 评论抓取**

![评论抓取](docs/screenshots/comments.png)

**📈 监控仪表盘 · 精确数据**

![监控仪表盘](docs/screenshots/dashboard.png)

**🌙 深色「数据终端」主题**

![深色主题](docs/screenshots/comments-dark.png)

**⚙️ 设置与运行诊断**

![设置与运行诊断](docs/screenshots/settings.png)

**🧾 任务历史**

![任务历史](docs/screenshots/settings-history.png)

**🧾 任务历史详情（深色主题）**

![任务历史详情](docs/screenshots/settings-history-detail-dark.png)

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

**设置与诊断**：在设置页切换主题、配置网络和默认输出目录；运行诊断会检查 Python、依赖、配置目录、输出目录及打包资源。最近错误与任务历史支持查看、复制、清理和复用参数。

## ⚙️ 设置

- **主题**：深色 / 浅色，下拉即时切换
- **传输通道**：`auto`（推荐）/ `h2-ja3` / `urllib`
- **代理池**：逗号分隔，如 `direct,socks5://127.0.0.1:7890`
- **默认输出目录**：默认为 exe 旁的 `导出` 目录
- **任务历史**：任务历史和最近错误仅保存在本机配置目录，不会写入或上传到项目仓库；项目目录中的运行数据、日志和缓存由 `.gitignore` 排除

## 🧱 网络层设计

所有工具共享同一套网络层（`core/`），请求统一调度：

1. **浏览器指纹身份**：每次会话生成内部自洽的随机 Chrome 指纹（UA / 屏幕 / 硬件 / WebGL / 时区），成套请求头防特征矛盾
2. **凭证预热与 buvid 激活**：spi 领取 buvid3/buvid4 → 签发 bili_ticket（24h 缓存）→ gaia ExClimbWuzhi 主动激活；cookie 按身份持久化，跨重启复用
3. **TLS 指纹双通道**：curl_cffi（真实 Chrome JA3/JA4 + HTTP/2）优先，失败自动降级标准库通道；风控（412/403/-352）与限流（429/-799）分类处理、退避重试
4. **代理池**：失败阈值冷却 + 自动轮换，每个代理出口绑定独立指纹身份
5. **风控人工恢复**：触发 -352 挑战时自动弹出极验验证页，人工通过后任务从断点继续

评论 gRPC 通道在上述基础上注入预热 cookie 与客户端 UA；另提供实验性 Chrome-TLS gRPC 通道（评论页可勾选，失败自动回退）。

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
    └── monitor/       # 实时监控
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
