# 梗采集器 · astrbot_plugin_meme_harvester

从 B 站视频字幕中自动抽取网络梗与黑话，沉淀成本地词条库，供 AstrBot 在聊天时检索、注入提示词，
并可一键同步到 [梗百科（astrbot_plugin_memewiki）](https://github.com/PurLango/astrbot_plugin_memewiki)。

> 只收录「词条 + 释义 + 出处（BV 号 / 时间戳）」，不保存全文字幕，支持随时删除。

## 功能

- **单条解析**：粘贴 B 站视频链接 / BV 号，自动抓取 CC 字幕或 AI 字幕，交给 LLM 结构化抽取梗。
- **合集批量解析**：B 站合集（season）、系列（series）链接自动分页、串行入库，带限速与风控熔断。
- **字幕优先级**：中文 CC 字幕 → 中文 AI 字幕 → AI 字幕搜索接口 → 弹幕接口，逐级兜底。
- **只增不改**：同名词条不覆盖，释义冲突进「审核队列」人工裁决；重复别名与标签自动并集。
- **反幻觉**：每条梗必须能在字幕里逐字定位到引用句，定位失败的候选直接丢弃。
- **群黑话**：按 `group:<群号>` / `private:<QQ号>` 作用域维护自定义黑话，优先于公共梗库命中。
- **提示词注入**：`on_llm_request` 钩子按消息内容做 n-gram 触发，把相关词条注入 system prompt。
- **WebUI 七页**：概览、单条解析、合集解析、词条库、审核队列、群黑话、设置。
- **扫码登录**：插件内直接渲染 B 站登录二维码，也支持手动粘贴 Cookie；Cookie 加密落库、日志脱敏。
- **梗百科兼容**：进程内调用其管理接口写入（自动重建索引），不可用时降级导出 JSONL。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AstrBot | ≥ 4.13（推荐 4.24+，`StarTools.get_data_dir` / 页面桥接更完整） |
| Python | 3.10+（AstrBot 自带依赖即可，**无额外第三方依赖**） |
| LLM | AstrBot 中已配置的对话模型 Provider（需支持 JSON 输出，不支持时自动降级容错解析） |
| B 站账号 | 建议使用小号；AI 字幕必须登录态才能获取 |

## 安装

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/<your-name>/astrbot_plugin_meme_harvester.git
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
- **词条库**：搜索、编辑释义/用法/出处、切换发布状态、删除。
- **审核队列**：处理「释义冲突」与「低置信度」候选，采纳或忽略。
- **群黑话**：按作用域新增/删除，聊天时优先命中。
- **设置**：模型、抽取、限速、注入、同步等全部参数。

## 工作流程

```
链接 → 解析目标（BV/av/短链/合集/系列）
     → 取视频信息（/x/web-interface/view）
     → 取字幕轨（player/wbi/v2 → player/v2 → dm/view，按 CC > AI 排序）
     → 下载字幕（CDN auth_key 分钟级过期，取到即下）
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
| `review_required` | `true` | 新词条先入草稿/审核 |
| `memewiki_enabled` / `memewiki_min_confidence` | `true` / `0.6` | 同步开关与最低置信度 |
| `inject_enabled` / `inject_max_chars` | `true` / `600` | 聊天注入开关与预算 |
| `auto_parse_on_link` | `false` | 群里直接发链接是否自动解析（默认关，防刷） |

## 数据与隐私

- 数据目录：`<AstrBot>/data/plugin_data/astrbot_plugin_meme_harvester/`
  - `memes.db`：词条、视频、字幕分段、任务、审核、黑话、账号、日志（SQLite WAL）。
  - `secret.key`：Cookie 加密密钥（仅本机，勿提交到仓库）。
- Cookie 使用 Fernet 加密后入库，接口只回显掩码，日志不打印明文。
- 字幕原文默认 30 天后清空，只保留抽出的词条与引用片段。
- 删除词条 / 视频记录即同时删除其引用；卸载插件后删除数据目录即可彻底清除。
- 请遵守 B 站用户协议与所在地区的法律法规，控制请求频率，不解析付费/充电专属内容。

## 已知限制

- **抖音不支持**：抖音无字幕接口，硬字幕需 OCR/ASR，成本与稳定性不成正比，故不纳入本插件。
- **AI 字幕需要登录**：匿名接口返回空字幕轨；登录态失效会提示重新扫码。
- **字幕覆盖率**：科技、解说、访谈类视频覆盖率高；纯音乐、整活、无解说视频可能没有字幕。
- **风控**：触发 412 后全局熔断 30 分钟，任务会失败并记录 `RISK_CONTROL`，恢复后重试即可。
- **长视频截断**：超过 `chunk_chars × max_chunks` 的字幕只处理前 N 块，结果里会标注。
- **合集速度**：受 1 QPS 限速约束，约 100–200 条/小时。

## 开发与测试

```bash
# 纯逻辑测试（无需 AstrBot）
python -m unittest discover -s tests -t .

# 完整测试（需要 AstrBot 运行环境，用于 api / 插件装配测试）
export PYTHONPATH="<AstrBot>/backend/app:$PYTHONPATH"
python -m unittest discover -s tests -t .
```

当前测试：**137 个用例**（存储、字幕协议、采集兜底、抽取校验、任务队列、梗百科适配、登录态、WebAPI、插件装配）。

目录结构：

```
main.py                 # 插件入口：命令、注入钩子、懒初始化
harvester/
  bili/                 # B 站采集（URL/WBI/Cookie/客户端/字幕/合集）
  storage.py            # SQLite（WAL + FTS5 trigram，降级 LIKE）
  extractor.py          # LLM 抽取 + 反幻觉校验
  pipeline.py           # 单条 / 合集流水线
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
