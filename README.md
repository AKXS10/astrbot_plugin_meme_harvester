# 梗采集器 · astrbot_plugin_meme_harvester

从视频字幕与网页正文中自动抽取网络梗与黑话，沉淀成本地词条库，供 AstrBot 在聊天时检索、注入提示词，
并可一键同步到 [梗百科（astrbot_plugin_memewiki）](https://github.com/PurLango/astrbot_plugin_memewiki)。

支持 B 站（字幕链路完整）以及 yt-dlp 能解析的其它视频平台（有字幕用字幕，没字幕可本地 Whisper 转写），
也支持萌娘百科 / MediaWiki / 普通网页的正文抓取。只收录「词条 + 释义 + 出处」，不保存全文字幕。

## 功能

- **单条解析**：粘贴 B 站视频链接 / BV 号，自动抓取 CC 字幕或 AI 字幕；没有字幕且已安装 `faster-whisper` 时下载音频本地转写，交给 LLM 结构化抽取梗。
- **多平台视频**：YouTube、抖音、TikTok、X、Vimeo、优酷、腾讯视频等交给 yt-dlp；优先平台字幕，没有字幕且已安装
  `faster-whisper` 时下载音频在本地转写（临时文件在任务结束后清理，可配置保留）。
- **网页抓取**：萌娘百科 / 维基百科走 MediaWiki API，其它站点提取正文；正文转成无时间轴分段后走同一套抽取流程。
- **合集批量解析**：B 站合集（season）、系列（series）链接自动分页、串行入库，带限速与风控熔断；支持新式
  `space.bilibili.com/<mid>/lists/<id>?type=season` 链接。
- **字幕优先级**：中文 CC 字幕 → 中文 AI 字幕 → AI 字幕搜索接口，逐级兜底（全部来自签名播放接口）；CC 轨只有片头/广告等极少内容时自动改选信息更全的 AI 轨；字幕时间轴与视频时长明显不符时整条拒绝，避免串片。
- **字幕兜底**：B 站 CC/AI 字幕都取不到、或字幕时间轴与视频明显不符时，自动下载音频走本地转写，避免「没有字幕就失败」。
- **来源分类**：词条库按来源归类——B 站显示 BV 号，其它站点显示「作者｜标题」，可按来源筛选。
- **任务管理**：任务列表可清除任意记录；同一链接再次提交时先清除旧进度再重新排队，不会堆叠重复任务。
- **字幕存档**：解析出的字幕文本保存在插件 `temp/` 目录，默认保留 3 天，可配置由后台定时清理。
- **只增不改**：同名词条不覆盖，释义冲突进「审核队列」人工裁决；重复别名与标签自动并集。
- **反幻觉**：每条梗必须能在来源文本里逐字定位到引用句，定位失败的候选直接丢弃。
- **群黑话**：按 `group:<群号>` / `private:<QQ号>` 作用域维护自定义黑话，优先于公共梗库命中。
- **提示词注入**：`on_llm_request` 钩子按消息内容做 n-gram 触发，把相关词条注入 system prompt。
- **WebUI 七页**：概览、单条解析、合集解析、词条库、审核队列、群黑话、设置。
- **扫码登录**：插件内直接渲染 B 站登录二维码，也支持手动粘贴 Cookie；Cookie 加密落库、日志脱敏。
- **梗百科兼容**：进程内调用其管理接口写入（自动重建索引），不可用时降级导出 JSONL。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | ≥ 4.13（推荐 4.24+，`StarTools.get_data_dir` / 页面桥接更完整） |
| Python | 3.10+（B 站与网页解析零额外依赖） |
| 可选依赖 | `yt-dlp`（其它平台视频）；`faster-whisper`（无字幕时本地转写，CPU 可跑 int8） |
| LLM | AstrBot 中已配置的对话模型 Provider（需支持 JSON 输出，不支持时自动降级容错解析） |
| B 站账号 | 建议使用小号；AI 字幕必须登录态才能获取 |

```bash
# 可选：开启多平台 / 本地转写
pip install yt-dlp
pip install faster-whisper "av<16"
```

> `av>=16` 与 faster-whisper 1.2.x 不兼容（`metadata_errors` 参数被移除），务必锁 `av<16`。
> 如果不方便改动 AstrBot 自带的 Python，可以装到独立目录并填进设置项 `asr_deps_path`：
>
> ```bash
> pip install --target <插件数据目录>/asr_deps faster-whisper "av<16"
> ```
>
> 插件会在导入 `faster_whisper` 前把该目录加入 `sys.path`。

**为什么不把 faster-whisper 打包进插件里？**

- 体积：faster-whisper + `av` + ctranslate2 + tokenizers 的 wheel 合计 **几百 MB**，且分平台（Windows / Linux / macOS × Python 版本各有独立 wheel），打进插件 zip 会让安装包和 GitHub 仓库直接爆炸。
- 许可证：CTranslate2 是 MPL-2.0，分发二进制需要附带合规声明，混进插件仓库增加维护负担。
- 安全与审核：插件市场 / GitHub 上传大体积二进制扩展容易被扫描器误报，也拖慢 AstrBot 装载。
- 按需安装：faster-whisper 只在「视频无字幕且开启本地转写」时才需要；大多数 B 站视频有 CC/AI 字幕，多数用户根本用不到。
- 模型权重另算：即便打进依赖，Whisper 模型权重（`tiny` 75MB ~ `small` 461MB）仍会在首次转写时联网下载到本机缓存，无法随插件分发省掉这一步。

因此本插件把它作为**可选依赖**：上面一行 `pip install` 即可启用；不安装时 B 站字幕、网页抓取、梗百科同步全部功能不受影响，仅无字幕视频的本地转写入口提示 `ASR_UNAVAILABLE`。

未安装可选依赖时，B 站与网页解析不受影响；相关任务会以 `MEDIA_UNSUPPORTED` / `ASR_UNAVAILABLE` 明确提示。

## 安装

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/AKXS10/astrbot_plugin_meme_harvester.git
# 重启 AstrBot，或在 WebUI「插件管理」中重载
```

重启后在「插件管理」启用「梗采集器」，进入插件页即可看到 WebUI。

## 首次配置

1. **选择模型**：插件页 → 设置 → 「对话模型 Provider」。留空表示跟随 AstrBot 当前默认模型。
2. **登录 B 站**：概览页 → 「扫码登录」，用 B 站手机客户端扫码确认。
   登录态约能维持数月；过期后插件会提示重新扫码。也可在「手动粘贴 Cookie」中填入浏览器里的完整 Cookie。
3. **（可选）连接梗百科**：安装并启用 `astrbot_plugin_memewiki` 后，概览页会显示「已连接」。
4. **（可选）调整抽取参数**：分块字数、最大分块数、最低置信度等均在设置页可改，保存到插件数据库。

## 使用

### 聊天命令

| 命令 | 说明 |
| --- | --- |
| `/梗解析 <链接或 BV 号>` | 解析单条视频并抽取梗 |
| `/梗查 <词>` | 查询词条释义与出处 |
| `/梗库` | 查看本地词条 / 视频 / 待审核统计 |
| `/梗同步` | 把待同步词条写入梗百科 |

### WebUI

- **单条解析 / 合集解析**：提交后进入后台队列，页面轮询任务进度；可关闭页面，任务继续执行。
- **词条库**：搜索、编辑标题/释义/用法/出处、切换发布状态、删除；修改标题会自动处理重名冲突，并联动清理梗百科里旧标题的同步条目。
- **审核队列**：处理「释义冲突」与「低置信度」候选，采纳或忽略。
- **群黑话**：按作用域新增/删除，聊天时优先命中。
- **设置**：模型、抽取、限速、注入、同步等全部参数。

## 工作流程

```
链接 → classify_url 分派
     ├─ B 站：解析目标（BV/av/短链/合集/系列）
     │    → 取视频信息（/x/web-interface/view）
     │    → 取字幕轨（只用签名端点 player/wbi/v2，按 CC > AI 排序，并校验时间轴与视频时长）
     │    → 下载字幕（CDN auth_key 分钟级过期，取到即下）
     │    └（无字幕 / 时间轴异常）下载音频 → faster-whisper 本地转写 → 临时目录清理
     ├─ 其它平台：yt-dlp 探测字幕 →（有）下载并解析 VTT/SRT
     │    └（无）下载音频 → faster-whisper 本地转写 → 临时目录清理
     └─ 网页：MediaWiki API / HTML 正文提取 → 切成无时间轴分段
     → 清洗分段 / 按字数与时间窗分块
     → 关键词快筛 → LLM 结构化抽取（JSON）
     → 引用校验 / 别名归一 / 置信度过滤
     → 只增不改入库（冲突进审核）
     → 梗百科同步（进程内写入 / JSONL 导出）
```

## 与梗百科（memewiki）的兼容

- 采用**进程内调用**：通过 `context.get_registered_star("astrbot_plugin_memewiki")` 取到插件实例，
  写入其 `user_terms` 后调用 `_save_user_terms()` 持久化，并逐条清理查询缓存。
- **只增不改**：已存在的词条跳过，不会覆盖你在梗百科里手改过的释义。
- 名称超过 16 字或释义为空的候选标记为 `skipped`，不写入。
- 梗百科未安装 / 版本不兼容时自动降级为「导出 JSONL」，可在其管理页手动导入。
- 群黑话只存在于本插件，不会写入梗百科。

## 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `provider_id` | `""` | 指定 Provider，空 = 跟随默认模型 |
| `json_mode` | `true` | 请求 `response_format=json_object`，不支持时自动重试 |
| `temperature` / `max_tokens` | `0.2` / `4096` | 抽取采样参数 |
| `llm_timeout` | `60` | 单次 LLM 请求超时（秒） |
| `chunk_chars` / `max_chunks` | `6000` / `8` | 字幕分块大小与单视频上限，超出标记截断 |
| `request_interval` | `1.0` | B 站请求最小间隔（秒），建议 ≥1 |
| `collection_page_size` / `max_collection_items` | `30` / `500` | 合集翻页与单次上限 |
| `queue_concurrency` | `1` | 后台任务并发（1–4） |
| `raw_retention_days` | `30` | 原始字幕保留天数，到期清空正文 |
| `temp_retention_days` | `3` | 字幕临时文件保留天数（1–90），后台每 6 小时清理一次 |
| `review_required` | `true` | 新词条先入草稿/审核 |
| `memewiki_enabled` / `memewiki_min_confidence` | `true` / `0.6` | 同步开关与最低置信度 |
| `inject_enabled` / `inject_max_chars` | `true` / `600` | 聊天注入开关与预算 |
| `auto_parse_on_link` | `false` | 群里直接发链接是否自动解析（默认关，防刷） |
| `media_enabled` | `true` | 是否解析 yt-dlp 支持的其它平台（需自行安装 yt-dlp） |
| `transcribe_enabled` | `true` | 无字幕时是否本地 Whisper 转写（需 faster-whisper） |
| `whisper_model` / `whisper_device` / `whisper_compute_type` | `small` / `cpu` / `int8` | 转写模型与推理设备；CPU 建议 tiny/base/small |
| `transcribe_language` | `""` | 转写语言，空 = 自动识别 |
| `asr_deps_path` | `""` | faster-whisper 的独立安装目录（`pip install --target`），留空用当前 Python |
| `media_max_duration` | `5400` | 单条视频时长上限（秒），超出直接拒绝 |
| `transcribe_timeout` / `download_timeout` | `1800` / `600` | 转写与音视频下载超时（秒） |
| `ytdlp_cookies_file` / `ytdlp_proxy` | `""` | yt-dlp 的 Cookie 文件（Netscape 格式）与代理 |
| `keep_temp_files` | `false` | 任务结束后是否保留临时音频（排查用） |
| `web_enabled` | `true` | 是否解析网页链接 |
| `web_timeout` / `web_max_bytes` / `web_min_chars` | `20` / `2000000` / `200` | 网页抓取超时、响应上限、正文最少字数 |
| `web_cookies` | `""` | 网页抓取 Cookie（可选，部分站点 403 时填写） |
| `max_source_chars` | `60000` | 单个文本源进入抽取前的最大字数 |

## 数据与隐私

- 数据目录：`<AstrBot>/data/plugin_data/astrbot_plugin_meme_harvester/`
  - `memes.db`：词条、视频、字幕分段、任务、审核、黑话、账号、日志（SQLite WAL）。
  - `secret.key`：Cookie 加密密钥（仅本机，勿提交到仓库）。
- Cookie 使用 Fernet 加密后入库，接口只回显掩码，日志不打印明文。
- 字幕原文默认 30 天后清空，只保留抽出的词条与引用片段。
- 删除词条 / 视频记录即同时删除其引用；卸载插件后删除数据目录即可彻底清除。
- 请遵守 B 站用户协议与所在地区的法律法规，控制请求频率，不解析付费/充电专属内容。

## 已知限制

- **抖音 / 其它平台的可用性取决于 yt-dlp**：抖音提取器受签名与风控影响经常失效，失败时会返回可诊断错误码；
  平台字幕可用时优先用字幕，不依赖 yt-dlp 的抖音提取器也不做硬字幕 OCR。
- **本地转写成本**：faster-whisper 在 CPU 上接近 1× 实时，长视频耗时明显；建议 `small` 或更小模型，并限制 `media_max_duration`。
- **AI 字幕需要登录**：匿名接口返回空字幕轨；登录态失效会提示重新扫码。播放接口偶发返回空轨道，插件会自动重试一次。
- **字幕轨只从签名端点读取**：B 站未签名的 `player/v2`、`v2/dm/view` 对同一 bvid 会返回**其他视频**的字幕（实测同一请求多次拿到不同视频的字幕），
  因此插件只信任 `/x/player/wbi/v2`，并额外校验「字幕时间轴不得超过视频时长 1.3 倍 + 30s」，不符则报 `SUBTITLE_MISMATCH` 拒绝入库。宁可缺，不可串。
- **字幕覆盖率**：科技、解说、访谈类视频覆盖率高；纯音乐、整活、无解说视频可能没有字幕，此时会明确提示 `NO_SUBTITLE`。
- **风控**：触发 412 后全局熔断 30 分钟，任务会失败并记录 `RISK_CONTROL`，恢复后重试即可。
- **长文本截断**：超过 `chunk_chars × max_chunks` 的文本只处理前 N 块，结果里会标注。
- **网页抓取**：只取静态 HTML（不执行 JS），SPA 站点可能正文为空；内置 SSRF 防护，拒绝私网与回环地址。
- **合集速度**：受 1 QPS 限速约束，约 100–200 条/小时。

## 开发与测试

```bash
# 纯逻辑测试（无需 AstrBot）
python -m unittest discover -s tests -t .

# 完整测试（需要 AstrBot 运行环境，用于 api / 插件装配测试）
export PYTHONPATH="<AstrBot>/backend/app:$PYTHONPATH"
python -m unittest discover -s tests -t .
```

当前测试：**186 个用例**（存储、字幕协议、采集兜底、抽取校验、任务队列、梗百科适配、登录态、WebAPI、插件装配、
VTT/SRT 解析、URL 分类、SSRF 防护、网页正文提取、MediaWiki API、yt-dlp 探测与转写回退、多平台流水线、回归用例）。

目录结构：

```
main.py                 # 插件入口：命令、注入钩子、懒初始化
harvester/
  bili/                 # B 站采集（URL/WBI/Cookie/客户端/字幕/合集）
  sources/              # 多来源：URL 分类、VTT/SRT、网页抓取、yt-dlp、Whisper
  storage.py            # SQLite（WAL + FTS5 trigram，降级 LIKE）
  extractor.py          # LLM 抽取 + 反幻觉校验
  pipeline.py           # 单条 / 合集 / 多平台 / 网页流水线
  llm.py                # AstrBot Provider 适配与 JSON 降级
  memewiki.py           # 梗百科进程内适配器
  search.py             # 检索与提示词注入
  tasks.py              # 后台任务队列
  api.py  auth.py       # WebAPI / 登录态
views/meme_harvester/   # WebUI（原生 JS，无构建步骤）
tests/                  # unittest
```

## License

MIT
