"""常量、错误码与提示词。"""

from __future__ import annotations

from enum import Enum

PLUGIN_NAME = "astrbot_plugin_meme_harvester"
DISPLAY_NAME = "梗采集器"
DB_FILENAME = "harvester.db"
SCHEMA_VERSION = 1

#: 梗百科插件名（用于进程内适配）
MEMEWIKI_PLUGIN_NAME = "astrbot_plugin_memewiki"
#: 梗百科自定义黑话的字段限制（来自其 WebUI 校验逻辑）
MEMEWIKI_MIN_TERM = 2
MEMEWIKI_MAX_TERM = 16
MEMEWIKI_MAX_MEANING = 300

BILI_REFERER = "https://www.bilibili.com"
BILI_MAIN_HOST = "https://api.bilibili.com"
BILI_PASSPORT_HOST = "https://passport.bilibili.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 规则快筛关键词：命中任一词的片段进入 LLM 精抽
CANDIDATE_KEYWORDS = (
    "梗",
    "黑话",
    "网络用语",
    "这什么梗",
    "什么梗",
    "被称为",
    "叫做",
    "意思是",
    "指的是",
    "名场面",
    "名梗",
    "谐音",
    "缩写",
    "弹幕",
    "出圈",
    "破防",
    "整活",
    "玩梗",
    "梗百科",
)

#: 视频级错误码（前端与日志共用）
class ErrorCode(str, Enum):
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    LOGIN_EXPIRED = "LOGIN_EXPIRED"
    RISK_CONTROL = "RISK_CONTROL"
    NO_SUBTITLE = "NO_SUBTITLE"
    CHARGE_BLOCKED = "CHARGE_BLOCKED"
    SUBTITLE_EXPIRED = "SUBTITLE_EXPIRED"
    VIDEO_NOT_FOUND = "VIDEO_NOT_FOUND"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    PROVIDER_JSON_UNSUPPORTED = "PROVIDER_JSON_UNSUPPORTED"
    LLM_FAILED = "LLM_FAILED"
    LLM_JSON_INVALID = "LLM_JSON_INVALID"
    MEMEWIKI_UNAVAILABLE = "MEMEWIKI_UNAVAILABLE"
    MEMEWIKI_SCHEMA_MISMATCH = "MEMEWIKI_SCHEMA_MISMATCH"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    BAD_URL = "BAD_URL"
    COLLECTION_EMPTY = "COLLECTION_EMPTY"
    INTERNAL = "INTERNAL"


ERROR_MESSAGES = {
    ErrorCode.LOGIN_REQUIRED: "需要登录 B 站：AI 字幕只对登录态开放，请在「设置」页扫码或粘贴 Cookie。",
    ErrorCode.LOGIN_EXPIRED: "B 站登录已失效（-101/-352），请重新扫码或更新 Cookie。",
    ErrorCode.RISK_CONTROL: "触发 B 站风控（-412），已暂停采集 30 分钟。",
    ErrorCode.NO_SUBTITLE: "这条视频没有可用字幕（CC / AI 字幕都取不到）。",
    ErrorCode.CHARGE_BLOCKED: "充电专属视频未解锁，已跳过。",
    ErrorCode.SUBTITLE_EXPIRED: "字幕下载链接已过期（auth_key 分钟级），正在重试。",
    ErrorCode.VIDEO_NOT_FOUND: "视频不存在或已删除。",
    ErrorCode.PROVIDER_NOT_CONFIGURED: "尚未选择对话模型 Provider，请到插件「设置」页选择。",
    ErrorCode.PROVIDER_JSON_UNSUPPORTED: "当前 Provider 不支持 JSON 模式，已自动降级。",
    ErrorCode.LLM_FAILED: "模型调用失败，请检查 Provider 配置或稍后重试。",
    ErrorCode.LLM_JSON_INVALID: "模型输出不是合法 JSON，已转审核队列。",
    ErrorCode.MEMEWIKI_UNAVAILABLE: "未找到梗百科插件，词条只保存在本插件词库。",
    ErrorCode.MEMEWIKI_SCHEMA_MISMATCH: "梗百科版本不匹配，已降级为导出文件。",
    ErrorCode.SSRF_BLOCKED: "链接不在允许的域名内，已拒绝。",
    ErrorCode.BAD_URL: "无法识别的视频链接。",
    ErrorCode.COLLECTION_EMPTY: "该合集没有可解析的视频。",
    ErrorCode.INTERNAL: "内部错误，请查看 AstrBot 日志。",
}

EXTRACT_SYSTEM_PROMPT = """你是一个中文网络梗词条编辑。给你一段 B 站视频字幕（带时间轴），请从中抽取真正的「网络梗 / 黑话 / 流行语」，输出 JSON。

要求：
1. 只输出 JSON，不要 Markdown 代码块，不要解释。
2. 顶层是对象：{"memes": [...]}。
3. 每个元素字段：
   - term: 梗的名称（2-16 字，尽量用字幕里出现的写法）
   - aliases: 别名数组（没有就给 []）
   - definition: 一句话释义，40-120 字，说清「是什么、怎么来的」
   - usage: 使用场景或例句，不超过 80 字，没有就给 ""
   - quote: 字幕中支撑该词条的原文片段（必须逐字摘录，不得改写）
   - start_ms: quote 在字幕中的起始毫秒（整数）
   - confidence: 0-1 的置信度（越确定越高）
   - tags: 2-4 个标签数组
4. 不要编造字幕里没有依据的词条；quote 必须能在输入字幕中找到。
5. 人名、地名、普通名词、单纯的情绪词不算梗。宁缺毋滥。
6. 最多输出 8 条，按置信度从高到低排序。"""

EXTRACT_USER_TEMPLATE = """视频标题：{title}
视频作者：{owner}

字幕片段（每行格式：起始毫秒|文本）：
{lines}

请抽取词条并输出 JSON。"""
