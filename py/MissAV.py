# coding=utf-8
# //@name:MissAV 中文字幕
# //@id:missav_subtitle
# //@version:7
#
# 四壳契约（TVBox / 影视仓 / OK影视 / PickTV）参照「麻豆AI传媒 madouai.xyz」写法：
#  - 独立 class Spider，不继承 base.spider，宿主缺模块也能无参数实例化
#  - 13 标准接口齐全且全部可调用（getDependence/localProxy 返回字符串，不为 None）
#  - homeContent: class[type_id/type_name] + filters 为 dict（空数组会让新壳分类退化）
#  - 列表五键 page/pagecount/limit/total/list，pagecount 取站点真实总页数
#  - 详情多线路 $$$、多集 #、集名与地址 $；playerContent header 为 dict、parse=0/jx=0
#  - init 预热网络通道，避免壳首次 homeContent 时握手未就绪导致分类空白
#
# 修复要点（2026-08-31 实测）
# 1. Cloudflare 拦截：站点已开启 TLS/请求头指纹校验。必须同时满足
#    (a) 自定义 SSLContext 并调用 set_ecdh_curve("X25519")（改变 supported_groups 指纹）
#    (b) 请求头带 Upgrade-Insecure-Requests 与 Sec-Fetch-Mode
#    缺任何一项一律返回 403「Just a moment...」。curl_cffi 作为可选二级通道
#    （实测 safari17_2_ios / chrome131 可用，chrome120 已失效）。
# 2. Accept-Encoding 不得声明 br：宿主常无 brotli，声明后拿到的是未解压字节，
#    表现为「页面能通但解析不出内容」。统一 gzip, deflate。
# 3. 播放地址在页面内被 eval(p,a,c,k,e,d) 打包，内含 source/source842/source1280，
#    需本地解包读取；不再凭 UUID 盲拼 1080p（实测多数片源只有 720p 目录）。
# 4. surrit.com CDN 同样需要 X25519 上下文 + Referer 才放行。

import ast
import base64
import importlib
import json
import re
import ssl
import threading
import time
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.poolmanager import PoolManager
    from urllib3.util import ssl_ as urllib3_ssl
    HAS_URLLIB3 = True
except Exception:
    PoolManager = None
    urllib3_ssl = None
    HAS_URLLIB3 = False

try:
    curl_requests = importlib.import_module("curl_cffi.requests")
    HAS_CURL_CFFI = True
except Exception:
    curl_requests = None
    HAS_CURL_CFFI = False

try:
    from lxml import html as lxml_html
    HAS_LXML = True
except Exception:
    lxml_html = None
    HAS_LXML = False


SCHEMA_DECLARATION = r'''
PLUGIN_CONFIG_SCHEMA = {
  "source": "declared",
  "description": "MissAV 直连插件。内置 Cloudflare 指纹兼容传输层（X25519 + 浏览器导航头），可选 curl_cffi 指纹通道与外部网关。字幕优先 FongMi 原生 subs。",
  "allowAdditional": false,
  "fields": [
    {"key": "host", "label": "站点地址", "type": "string", "required": false, "defaultValue": "https://missav.ws"},
    {"key": "mirrors", "label": "备用域名", "type": "string", "required": false, "defaultValue": "missav.ai,missav123.com,missav888.com,njavtv.com,thisav2.com", "description": "逗号分隔，主域不可用时依次尝试。"},
    {"key": "cookie", "label": "站点 Cookie", "type": "secret", "required": false},
    {"key": "timeout", "label": "请求超时秒数", "type": "number", "required": false, "defaultValue": 15},
    {"key": "gateway_timeout", "label": "FlareSolverr 网关超时秒数", "type": "number", "required": false, "defaultValue": 90},
    {"key": "playlist_timeout", "label": "清晰度清单超时秒数", "type": "number", "required": false, "defaultValue": 5},
    {"key": "cache_ttl", "label": "列表页缓存秒数", "type": "number", "required": false, "defaultValue": 60},
    {"key": "warmup", "label": "启动预热", "type": "boolean", "required": false, "defaultValue": true},
    {"key": "cover_via_proxy", "label": "封面走本地代理", "type": "boolean", "required": false, "defaultValue": false, "description": "封面默认直连 fourhoi（实测图片路径无 CF 盾，普通客户端 200）。仅当你的壳直连 403 时才开 true 走本地代理。"},
    {"key": "subtitle_fetch", "label": "字幕查询方式", "type": "string", "required": false, "defaultValue": "async", "description": "async=后台预取不阻塞起播；blocking=等到字幕再起播。"},
    {"key": "subtitle_wait", "label": "字幕最长等待秒数", "type": "number", "required": false, "defaultValue": 2},
    {"key": "play_quality", "label": "播放清晰度策略", "type": "string", "required": false, "defaultValue": "best", "description": "auto=返回 HLS 主清单；best=页面内最高档直链；1080p/720p/480p=优先不超过目标高度。"},
    {"key": "subtitle_enabled", "label": "启用中文字幕", "type": "boolean", "required": false, "defaultValue": true},
    {"key": "subtitle_mode", "label": "字幕接入方式", "type": "string", "required": false, "defaultValue": "native"},
    {"key": "subtitle_worker_base_url", "label": "字幕 Worker 地址", "type": "string", "required": false},
    {"key": "subtitle_sources", "label": "字幕来源顺序", "type": "string", "required": false, "defaultValue": "xunlei,subtitlecat"},
    {"key": "subtitle_cache_ttl", "label": "字幕缓存秒数", "type": "number", "required": false, "defaultValue": 21600},
    {"key": "proxy_gateway", "label": "外部代理网关地址", "type": "string", "required": false, "defaultValue": ""},
    {"key": "gateway_url", "label": "兼容网关地址", "type": "string", "required": false},
    {"key": "search_api_enabled", "label": "启用公开搜索接口", "type": "boolean", "required": false, "defaultValue": true},
    {"key": "impersonate", "label": "浏览器指纹", "type": "string", "required": false, "defaultValue": "safari17_2_ios", "description": "curl_cffi 指纹；实测可用 safari17_2_ios / safari17_0 / chrome131 / chrome124。"},
    {"key": "max_retries", "label": "直连重试次数", "type": "number", "required": false, "defaultValue": 2}
  ]
}
PLUGIN_SCHEMA_END = 1
FILTER_CONFIG_SCHEMA = {
  "source": "declared",
  "description": "通用番号中文字幕过滤器。推荐拦截 detail,player。",
  "allowAdditional": false,
  "fields": [
    {"key": "enabled", "label": "启用过滤器", "type": "boolean", "required": false, "defaultValue": true},
    {"key": "subtitle_mode", "label": "字幕接入方式", "type": "string", "required": false, "defaultValue": "native"},
    {"key": "subtitle_worker_base_url", "label": "字幕 Worker 地址", "type": "string", "required": false},
    {"key": "subtitle_sources", "label": "字幕来源顺序", "type": "string", "required": false, "defaultValue": "xunlei,subtitlecat"},
    {"key": "timeout", "label": "字幕请求超时秒数", "type": "number", "required": false, "defaultValue": 10},
    {"key": "subtitle_cache_ttl", "label": "字幕缓存秒数", "type": "number", "required": false, "defaultValue": 21600},
    {"key": "mark_detail", "label": "详情标记识别到的番号", "type": "boolean", "required": false, "defaultValue": false},
    {"key": "overwrite_subs", "label": "覆盖站点已有字幕", "type": "boolean", "required": false, "defaultValue": false}
  ]
}
FILTER_SCHEMA_END = 1
'''

PLUGIN_CONFIG_SCHEMA = {
    "source": "declared",
    "description": "MissAV 直连插件。内置 Cloudflare 指纹兼容传输层（X25519 + 浏览器导航头），可选 curl_cffi 指纹通道与外部网关。字幕优先 FongMi 原生 subs。",
    "allowAdditional": False,
    "fields": [
        {"key": "host", "label": "站点地址", "type": "string", "required": False, "defaultValue": "https://missav.ws"},
        {"key": "mirrors", "label": "备用域名", "type": "string", "required": False,
         "defaultValue": "missav.ai,missav123.com,missav888.com,njavtv.com,thisav2.com",
         "description": "逗号分隔，主域不可用时依次尝试。"},
        {"key": "cookie", "label": "站点 Cookie", "type": "secret", "required": False},
        {"key": "timeout", "label": "请求超时秒数", "type": "number", "required": False, "defaultValue": 15},
        {"key": "gateway_timeout", "label": "FlareSolverr 网关超时秒数", "type": "number", "required": False, "defaultValue": 90},
        {"key": "playlist_timeout", "label": "清晰度清单超时秒数", "type": "number", "required": False, "defaultValue": 5},
        {"key": "cache_ttl", "label": "列表页缓存秒数", "type": "number", "required": False, "defaultValue": 60,
         "description": "同一页在此时间内重复请求直接走内存缓存，抵消壳的重复调用；0=关闭。"},
        {"key": "warmup", "label": "启动预热", "type": "boolean", "required": False, "defaultValue": True,
         "description": "init 时提前完成 TLS 握手并缓存首屏，减少壳打开源时的等待。"},
        {"key": "cover_via_proxy", "label": "封面走本地代理", "type": "boolean", "required": False, "defaultValue": False,
         "description": "Peek Pro 对 fourhoi 图片会 403，必须走本地代理（http://127.0.0.1:9978/proxy?do=local）。默认开启；仅当你的壳能直连 fourhoi 时才关。"},
        {"key": "subtitle_fetch", "label": "字幕查询方式", "type": "string", "required": False, "defaultValue": "async",
         "description": "async=后台预取，最多等 subtitle_wait 秒就起播；blocking=等到字幕结果再起播。"},
        {"key": "subtitle_wait", "label": "字幕最长等待秒数", "type": "number", "required": False, "defaultValue": 2,
         "description": "仅 async 模式有效。0=完全不等（首播可能无字幕，重进即有）。"},
        {"key": "play_quality", "label": "播放清晰度策略", "type": "string", "required": False, "defaultValue": "best",
         "description": "auto=返回 HLS 主清单；best=页面内最高档直链；1080p/720p/480p=优先不超过目标高度。"},
        {"key": "subtitle_enabled", "label": "启用中文字幕", "type": "boolean", "required": False, "defaultValue": True},
        {"key": "subtitle_mode", "label": "字幕接入方式", "type": "string", "required": False, "defaultValue": "native"},
        {"key": "subtitle_worker_base_url", "label": "字幕 Worker 地址", "type": "string", "required": False},
        {"key": "subtitle_sources", "label": "字幕来源顺序", "type": "string", "required": False, "defaultValue": "xunlei,subtitlecat"},
        {"key": "subtitle_cache_ttl", "label": "字幕缓存秒数", "type": "number", "required": False, "defaultValue": 21600},
        {"key": "proxy_gateway", "label": "外部代理网关地址", "type": "string", "required": False, "defaultValue": ""},
        {"key": "gateway_url", "label": "兼容网关地址", "type": "string", "required": False},
        {"key": "search_api_enabled", "label": "启用公开搜索接口", "type": "boolean", "required": False, "defaultValue": True},
        {"key": "impersonate", "label": "浏览器指纹", "type": "string", "required": False, "defaultValue": "safari17_2_ios",
         "description": "curl_cffi 指纹；实测可用 safari17_2_ios / safari17_0 / chrome131 / chrome124。"},
        {"key": "max_retries", "label": "直连重试次数", "type": "number", "required": False, "defaultValue": 2},
    ],
}

FILTER_CONFIG_SCHEMA = {
    "source": "declared",
    "description": "通用番号中文字幕过滤器。推荐拦截 detail,player。",
    "allowAdditional": False,
    "fields": [
        {"key": "enabled", "label": "启用过滤器", "type": "boolean", "required": False, "defaultValue": True},
        {"key": "subtitle_mode", "label": "字幕接入方式", "type": "string", "required": False, "defaultValue": "native"},
        {"key": "subtitle_worker_base_url", "label": "字幕 Worker 地址", "type": "string", "required": False},
        {"key": "subtitle_sources", "label": "字幕来源顺序", "type": "string", "required": False, "defaultValue": "xunlei,subtitlecat"},
        {"key": "timeout", "label": "字幕请求超时秒数", "type": "number", "required": False, "defaultValue": 10},
        {"key": "subtitle_cache_ttl", "label": "字幕缓存秒数", "type": "number", "required": False, "defaultValue": 21600},
        {"key": "mark_detail", "label": "详情标记识别到的番号", "type": "boolean", "required": False, "defaultValue": False},
        {"key": "overwrite_subs", "label": "覆盖站点已有字幕", "type": "boolean", "required": False, "defaultValue": False},
    ],
}


DEFAULT_HOST = "https://missav.ws"
DEFAULT_MIRRORS = ("missav.ai", "missav123.com", "missav888.com", "njavtv.com", "thisav2.com")
XUNLEI_SUBTITLE_API = "https://api-shoulei-ssl.xunlei.com/oracle/subtitle"
SUBTITLECAT_SITE = "https://subtitlecat.com"
COVER_HOST = "https://fourhoi.com"
PAGE_SIZE = 12  # 站点列表每页固定 12 条（实测）
# 列表用缩略图（cover-t 平均 27KB），详情用高清图（cover-n 平均 129KB）。
# 一页 12 张：缩略图合计约 320KB，高清图约 1.5MB —— 列表用高清是首屏卡顿主因。
COVER_LIST = "cover-t.jpg"
COVER_DETAIL = "cover-n.jpg"
DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)
PLAYER_UA = (
    "Mozilla/5.0 (Linux; Android 11; TV) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SEARCH_API_HOST = "https://client-rapi-missav.recombee.com"
SEARCH_API_DATABASE = "missav-default"
SEARCH_API_TOKEN = "Ikkg568nlM51RHvldlPvc2GzZPE9R4XGzaH9Qj4zK9npbbbTly1gj9K4mgRn0QlV"
DEFAULT_IMPERSONATE = "safari17_2_ios"
# chrome120 在 2026-08 已被 Cloudflare 拒绝，保留别名但不作为默认。
IMPERSONATE_ALIASES = {
    "safari17_2_ios": "safari17_2_ios",
    "safari17_0": "safari17_0",
    "safari_ios": "safari_ios",
    "safari": "safari",
    "chrome131": "chrome131",
    "chrome124": "chrome124",
    "chrome120": "chrome120",
    "chrome": "chrome",
}
IMPERSONATE_FALLBACK = ("safari17_2_ios", "chrome131", "chrome124", "chrome")
IMPERSONATE_UA = {
    "safari17_2_ios": DEFAULT_UA,
    "safari17_0": DEFAULT_UA,
    "chrome131": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "chrome124": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "chrome120": PLAYER_UA,
}
# 关键：这两个头缺失即被 Cloudflare 判定为非浏览器导航请求（实测 403）
NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    # 不声明 br：宿主可能缺 brotli 解码器，会拿到未解压内容
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
}
ATVP_DETAIL_PREFIX = "atvp_detail:"
PLAY_PREFIX = "missav-play:"
STATUS_PREFIX = "missav-status:"
CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "just a moment",
    "attention required",
    "turnstile",
    "enable javascript and cookies to continue",
)
CHALLENGE_PLATFORM_MARKERS = (
    "/cdn-cgi/challenge-platform",
    "_cf_chl_opt",
    "challenge-platform",
)
CODE_PATTERNS = (
    re.compile(r"(?<![A-Z0-9])FC2(?:[-_ ]?PPV)?[-_ ]?(\d{5,9})(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z]{2,10})[-_ ]+(\d{2,7})(?![A-Z0-9])", re.I),
    re.compile(r"(?<![A-Z0-9])([A-Z]{2,10})(\d{3,7})(?![A-Z0-9])", re.I),
)
IGNORED_CODE_PREFIXES = frozenset(
    ("AAC", "AVC", "BD", "DVD", "FHD", "FPS", "H264", "H265", "HDR", "HEVC", "UHD", "WEB")
)
SLUG_SUFFIX_RE = re.compile(r"-(?:chinese-subtitle|english-subtitle|uncensored-leak)$", re.I)
# 列表卡片封面：站点所有列表统一走 fourhoi.com/<slug>/cover-t.jpg，
# 用它锚定卡片可彻底排除语言切换旗标等噪声链接。
CARD_COVER_RE = re.compile(r"https?://[^\s\"']*?/([a-z0-9][a-z0-9._-]*?)/cover-t\.jpg", re.I)
PACKED_RE = re.compile(
    r"}\('(?P<p>(?:\\.|[^'\\])*)',(?P<a>\d+),(?P<c>\d+),'(?P<k>(?:\\.|[^'\\])*)'\.split\('\|'\)"
)
SOURCE_ASSIGN_RE = re.compile(r"(source(?:\d+)?)\s*=\s*'(https?://[^']+\.m3u8[^']*)'", re.I)
_B36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _bounded_int(value, default, minimum, maximum):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def _classify_response(response):
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    lower = text.lower()
    headers = getattr(response, "headers", {}) or {}
    if any(marker in lower for marker in CHALLENGE_MARKERS):
        return "cloudflare-managed-challenge"
    mitigated = str(headers.get("CF-Mitigated") or headers.get("cf-mitigated") or "").lower()
    if "challenge" in mitigated:
        return "cloudflare-managed-challenge"
    if status >= 400 and any(marker in lower for marker in CHALLENGE_PLATFORM_MARKERS):
        return "cloudflare-managed-challenge"
    if status == 429:
        return "rate-limited"
    if 500 <= status <= 599:
        return "upstream-error"
    if status >= 400:
        return "http-error"
    if not text.strip():
        return "empty-response"
    return "ok"


def _parse_config(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)):
        merged = {}
        for item in value:
            merged.update(_parse_config(item))
        return merged
    text = str(value or "").strip()
    if not text:
        return {}
    for loader in (json.loads, ast.literal_eval):
        try:
            data = loader(text)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def _normalize_origin(value):
    text = str(value or DEFAULT_HOST).strip().rstrip("/")
    if text and "://" not in text:
        text = "https://" + text
    try:
        parsed = urlsplit(text)
    except Exception:
        return DEFAULT_HOST
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return DEFAULT_HOST
    return parsed.scheme + "://" + parsed.netloc


def _is_flaresolverr_gateway(value):
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and parsed.path.rstrip("/") == "/v1"


def _normalize_play_quality(value):
    text = str(value or "best").strip().lower().replace(" ", "")
    aliases = {
        "auto": "auto", "adaptive": "auto", "abr": "auto",
        "best": "best", "highest": "best", "max": "best",
        "1080": "1080", "1080p": "1080",
        "720": "720", "720p": "720",
        "480": "480", "480p": "480",
        "360": "360", "360p": "360",
    }
    return aliases.get(text, "best")


def _normalize_impersonate(value):
    text = str(value or DEFAULT_IMPERSONATE).strip().lower().replace("-", "_")
    return IMPERSONATE_ALIASES.get(text, DEFAULT_IMPERSONATE)


def _normalize_code(prefix, number):
    upper = str(prefix or "").upper().replace("_", "-").strip("- ")
    digits = str(number or "").strip()
    if not upper or not digits:
        return ""
    if upper.startswith("FC2"):
        return "FC2-PPV-" + digits
    if upper in IGNORED_CODE_PREFIXES:
        return ""
    return upper + "-" + digits


def extract_video_code(*values):
    text = " ".join(_clean_text(value).upper() for value in values if value)
    if not text:
        return ""
    for index, pattern in enumerate(CODE_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        if index == 0:
            return "FC2-PPV-" + match.group(1)
        code = _normalize_code(match.group(1), match.group(2))
        if code:
            return code
    return ""


def _code_matches(value, code):
    compact_value = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    compact_code = re.sub(r"[^A-Z0-9]", "", str(code or "").upper())
    return bool(compact_code and compact_code in compact_value)


def _format_duration(seconds):
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


def _subtitle_mime(url):
    path = urlsplit(str(url or "")).path.lower()
    if path.endswith(".vtt"):
        return "text/vtt"
    if path.endswith((".ass", ".ssa")):
        return "text/x-ssa"
    return "application/x-subrip"


def _js_unescape(text):
    return (
        str(text or "")
        .replace("\\\\", "\x00")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\n", "\n")
        .replace("\x00", "\\")
    )


def _base_convert(number, radix):
    out = ""
    while True:
        number, remainder = divmod(number, radix)
        out = (_B36_DIGITS[remainder] if remainder < 36 else chr(remainder + 29)) + out
        if number == 0:
            return out


def unpack_eval_blocks(text):
    """本地解包 eval(function(p,a,c,k,e,d){...}) 打包脚本，不依赖 JS 引擎。"""
    results = []
    for match in PACKED_RE.finditer(str(text or "")):
        try:
            payload = _js_unescape(match.group("p"))
            radix = int(match.group("a"))
            count = int(match.group("c"))
            words = _js_unescape(match.group("k")).split("|")
            table = {}
            for index in range(count):
                key = _base_convert(index, radix)
                value = words[index] if index < len(words) else ""
                table[key] = value if value else key
            results.append(re.sub(r"\b\w+\b", lambda m: table.get(m.group(0), m.group(0)), payload))
        except Exception:
            continue
    return results


def extract_play_sources(html_text):
    """返回 {变量名: m3u8 地址}，来源为页面解包后的 source/source842/source1280。"""
    sources = {}
    for block in unpack_eval_blocks(html_text):
        for name, url in SOURCE_ASSIGN_RE.findall(block):
            sources[name.lower()] = url
    if not sources:
        for name, url in SOURCE_ASSIGN_RE.findall(str(html_text or "")):
            sources[name.lower()] = url
    return sources


class CloudflareTLSAdapter(HTTPAdapter):
    """
    改写 TLS ClientHello 的 supported_groups（X25519 优先）+ ALPN，
    使 requests 的 JA3 不再落在 Cloudflare 的 python-requests 黑名单里。
    实测：仅此一项改变即可让 403 挑战页变成 200 正常页。
    """

    def __init__(self, ciphers=None, **kwargs):
        self._ciphers = ciphers
        super(CloudflareTLSAdapter, self).__init__(**kwargs)

    def _build_context(self):
        if HAS_URLLIB3 and urllib3_ssl is not None:
            context = urllib3_ssl.create_urllib3_context(ciphers=self._ciphers)
        else:
            context = ssl.create_default_context()
            if self._ciphers:
                try:
                    context.set_ciphers(self._ciphers)
                except Exception:
                    pass
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except Exception:
            pass
        try:
            context.set_alpn_protocols(["h2", "http/1.1"])
        except Exception:
            pass
        try:
            context.options |= ssl.OP_NO_COMPRESSION
        except Exception:
            pass
        # 决定性因素：显式指定曲线，改变 supported_groups 扩展
        for curve in ("X25519", "prime256v1"):
            try:
                context.set_ecdh_curve(curve)
                break
            except Exception:
                continue
        return context

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        context = self._build_context()
        if HAS_URLLIB3 and PoolManager is not None:
            kwargs["ssl_context"] = context
            self.poolmanager = PoolManager(
                num_pools=connections, maxsize=maxsize, block=block, **kwargs
            )
        else:
            super(CloudflareTLSAdapter, self).init_poolmanager(
                connections, maxsize, block=block, **kwargs
            )

    def proxy_manager_for(self, proxy, **kwargs):
        try:
            kwargs["ssl_context"] = self._build_context()
        except Exception:
            pass
        return super(CloudflareTLSAdapter, self).proxy_manager_for(proxy, **kwargs)


def build_tls_session(user_agent=None, cookie=""):
    """构造带 CF 指纹兼容层的 requests.Session。"""
    session = requests.Session()
    try:
        session.headers.clear()
    except Exception:
        pass
    headers = dict(NAV_HEADERS)
    headers["User-Agent"] = user_agent or DEFAULT_UA
    if cookie:
        headers["Cookie"] = cookie
    session.headers.update(headers)
    try:
        session.mount("https://", CloudflareTLSAdapter())
    except Exception:
        pass
    return session


class SubtitleResolver:
    def _init_subtitle_resolver(self):
        self._subtitle_session = build_tls_session()
        self._subtitle_enabled = True
        self._subtitle_mode = "native"
        self._subtitle_worker = ""
        self._subtitle_sources = ("xunlei", "subtitlecat")
        self._subtitle_timeout = 10
        self._subtitle_cache_ttl = 21600
        self._subtitle_cache = {}
        self._subtitle_inflight = set()
        self._subtitle_blocking = False
        self._subtitle_wait = 2.0
        self._subtitle_lock = threading.RLock()

    def _configure_subtitles(self, config):
        self._subtitle_enabled = _bool(config.get("subtitle_enabled", config.get("enabled")), True)
        mode = str(config.get("subtitle_mode") or "native").strip().lower()
        self._subtitle_mode = mode if mode in ("native", "hls") else "native"
        self._subtitle_worker = str(config.get("subtitle_worker_base_url") or "").strip().rstrip("/")
        requested = [item.strip().lower() for item in str(config.get("subtitle_sources") or "xunlei,subtitlecat").split(",")]
        self._subtitle_sources = tuple(item for item in requested if item in ("xunlei", "subtitlecat")) or ("xunlei",)
        self._subtitle_timeout = _bounded_int(config.get("timeout"), 10, 3, 30)
        self._subtitle_cache_ttl = _bounded_int(config.get("subtitle_cache_ttl"), 21600, 60, 604800)
        # 字幕查询是否阻塞起播：blocking=等到结果；async=后台预取 + 短暂等待
        mode = str(config.get("subtitle_fetch") or "async").strip().lower()
        self._subtitle_blocking = mode in ("blocking", "block", "sync", "wait")
        # async 模式下 playerContent 最多等这么久（秒），到点仍无结果就先起播
        self._subtitle_wait = max(float(_bounded_int(config.get("subtitle_wait"), 2, 0, 10)), 0.0)
        with self._subtitle_lock:
            self._subtitle_cache = {}
            self._subtitle_inflight = set()

    def _subtitle_rows(self, payload):
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("data", "results", "items", "subtitles"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = self._subtitle_rows(value)
                if nested:
                    return nested
        return []

    def _find_xunlei_subtitle(self, code):
        try:
            response = self._subtitle_session.get(
                XUNLEI_SUBTITLE_API,
                params={"name": code},
                timeout=self._subtitle_timeout,
            )
            if int(getattr(response, "status_code", 0) or 0) >= 400:
                return ""
            payload = response.json()
        except Exception:
            return ""
        fallback = ""
        for row in self._subtitle_rows(payload):
            if not isinstance(row, dict):
                continue
            url = row.get("url") or row.get("subtitle_url") or row.get("download_url")
            if not url:
                continue
            name = " ".join(str(row.get(key) or "") for key in ("name", "extra_name"))
            haystack = name + " " + str(url)
            if not _code_matches(haystack, code):
                continue
            text = str(url)
            if re.search(r"\.(?:srt|vtt|ass|ssa)(?:[?#]|$)", text, re.I):
                if re.search(r"(?:^|[^a-z])(?:zh|chs|cht|cn|chi)(?:[^a-z]|$)|简|繁|中文", name + text, re.I):
                    return text.strip()
                fallback = fallback or text.strip()
        return fallback

    def _find_subtitlecat_subtitle(self, code):
        try:
            search = self._subtitle_session.get(
                SUBTITLECAT_SITE + "/index.php",
                params={"search": code},
                timeout=self._subtitle_timeout,
            )
            if int(getattr(search, "status_code", 0) or 0) >= 400:
                return ""
            detail_url = ""
            for href in re.findall(r'href="(subs/[^"]+\.html)"', search.text, re.I):
                if _code_matches(href, code):
                    detail_url = urljoin(SUBTITLECAT_SITE + "/", href)
                    break
            if not detail_url:
                for href in re.findall(r'href="([^"]+\.html)"', search.text, re.I):
                    if _code_matches(href, code):
                        detail_url = urljoin(SUBTITLECAT_SITE + "/", href)
                        break
            if not detail_url:
                return ""
            detail = self._subtitle_session.get(detail_url, timeout=self._subtitle_timeout)
            if int(getattr(detail, "status_code", 0) or 0) >= 400:
                return ""
            body = detail.text
        except Exception:
            return ""
        candidates = []
        for href in re.findall(r'href="([^"]+)"', body):
            if not re.search(r"\.srt(?:\?|$)|download\.php", href, re.I):
                continue
            score = 0
            if re.search(r"zh-CN|zh_CN|simplified|简体|chs", href, re.I):
                score = 3
            elif re.search(r"zh-TW|zh_TW|繁|cht", href, re.I):
                score = 2
            elif re.search(r"(?:^|[^a-z])(?:zh|cn|chinese|中文)", href, re.I):
                score = 1
            candidates.append((score, urljoin(detail_url, href)))
        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0], reverse=True)
        if candidates[0][0] <= 0:
            return ""
        return candidates[0][1]

    def _resolve_subtitle(self, code, wait=None):
        """
        wait=None  按默认超时同步查询（阻塞直到有结果）
        wait=0     只读缓存；未命中则后台预取，立即返回空
        wait=N>0   最多等待 N 秒后台结果，超时返回空（不拖慢起播）
        """
        normalized = extract_video_code(code)
        if not self._subtitle_enabled or not normalized:
            return ""
        cached = self._cached_subtitle(normalized)
        if cached is not None:
            return cached
        if wait is None:
            subtitle_url = self._lookup_subtitle(normalized)
            with self._subtitle_lock:
                self._subtitle_cache[normalized] = (time.time(), subtitle_url)
            return subtitle_url
        self._spawn_subtitle_lookup(normalized)
        deadline = time.time() + max(float(wait or 0), 0.0)
        while time.time() < deadline:
            time.sleep(0.1)
            cached = self._cached_subtitle(normalized)
            if cached is not None:
                return cached
        return ""

    def _cached_subtitle(self, normalized):
        with self._subtitle_lock:
            hit = self._subtitle_cache.get(normalized)
        if hit and time.time() - hit[0] < self._subtitle_cache_ttl:
            return hit[1]
        return None

    def _lookup_subtitle(self, normalized):
        """
        两个字幕源并发查询，谁先命中用谁（实测 xunlei 0.4~3.7s、subtitlecat 0.8~1.6s，
        串行时最差要等两者之和；并发后基本等于较快的那个）。
        """
        finders = []
        for source in self._subtitle_sources:
            if source == "xunlei":
                finders.append(("xunlei", self._find_xunlei_subtitle))
            elif source == "subtitlecat":
                finders.append(("subtitlecat", self._find_subtitlecat_subtitle))
        if not finders:
            return ""
        if len(finders) == 1:
            try:
                return finders[0][1](normalized) or ""
            except Exception:
                return ""

        results = {}
        lock = threading.Lock()

        def run(name, fn):
            try:
                value = fn(normalized) or ""
            except Exception:
                value = ""
            with lock:
                results[name] = value

        threads = []
        for name, fn in finders:
            thread = threading.Thread(target=run, args=(name, fn),
                                      name="missav-sub-%s" % name, daemon=True)
            thread.start()
            threads.append(thread)
        deadline = time.time() + self._subtitle_timeout + 2
        for thread in threads:
            thread.join(max(deadline - time.time(), 0.1))
        # 按配置的来源优先级取第一个命中的
        with lock:
            for name, _ in finders:
                if results.get(name):
                    return results[name]
        return ""

    def _spawn_subtitle_lookup(self, normalized):
        """后台预取字幕，结果进缓存；下一次请求同一番号即可直接命中。"""
        with self._subtitle_lock:
            if normalized in self._subtitle_inflight:
                return
            self._subtitle_inflight.add(normalized)

        def worker():
            try:
                url = self._lookup_subtitle(normalized)
            except Exception:
                url = ""
            with self._subtitle_lock:
                self._subtitle_cache[normalized] = (time.time(), url)
                self._subtitle_inflight.discard(normalized)

        try:
            thread = threading.Thread(target=worker, name="missav-sub-" + normalized, daemon=True)
            thread.start()
        except Exception:
            with self._subtitle_lock:
                self._subtitle_inflight.discard(normalized)

    def _subtitle_track(self, subtitle_url):
        source_url = str(subtitle_url or "").strip()
        if not source_url:
            return None
        if self._subtitle_worker:
            proxy_url = self._subtitle_worker + "/subtitle.vtt?" + urlencode({"subtitle": source_url})
            return {"name": "中文字幕", "url": proxy_url, "lang": "zh-CN", "format": "text/vtt", "flag": 1}
        return {
            "name": "中文字幕",
            "url": source_url,
            "lang": "zh-CN",
            "format": _subtitle_mime(source_url),
            "flag": 1,
        }

    def _attach_native_subtitle(self, result, subtitle_url, overwrite=False):
        if not isinstance(result, dict):
            return result
        track = self._subtitle_track(subtitle_url)
        if not track:
            return result
        output = dict(result)
        existing = output.get("subs")
        if isinstance(existing, list) and existing and not overwrite:
            urls = {str(item.get("url") or "") for item in existing if isinstance(item, dict)}
            if track["url"] not in urls:
                output["subs"] = list(existing) + [track]
            return output
        output["subs"] = [track]
        return output

    def _worker_master_url(self, video_url, subtitle_url):
        if not self._subtitle_worker:
            return ""
        return self._subtitle_worker + "/master.m3u8?" + urlencode(
            {"video": str(video_url or ""), "subtitle": str(subtitle_url or "")}
        )

    def _attach_hls_subtitle(self, result, subtitle_url):
        if not isinstance(result, dict) or not self._subtitle_worker:
            return result
        output = dict(result)
        value = output.get("url")

        def wrap(item):
            text = str(item or "").strip()
            if not re.search(r"\.m3u8(?:[?#]|$)", text, re.I):
                return item
            if text.startswith(self._subtitle_worker + "/master.m3u8"):
                return item
            return self._worker_master_url(text, subtitle_url)

        if isinstance(value, list):
            converted = list(value)
            for index in range(1, len(converted), 2):
                converted[index] = wrap(converted[index])
            output["url"] = converted
        elif isinstance(value, str):
            output["url"] = wrap(value)
        return output

    def _attach_subtitle(self, result, code, overwrite=False, wait=None):
        subtitle_url = self._resolve_subtitle(code, wait=wait)
        if not subtitle_url:
            return result
        if self._subtitle_mode == "hls":
            return self._attach_hls_subtitle(result, subtitle_url)
        return self._attach_native_subtitle(result, subtitle_url, overwrite=overwrite)


class Spider(SubtitleResolver):
    """
    四壳契约（TVBox / 影视仓 / OK影视 / PickTV）参照麻豆AI传媒写法：
    - 独立 class Spider，不继承 base.spider，宿主缺模块也能实例化
    - 13 个标准接口全部可调用，返回类型固定
    - categoryContent / searchContent 采用位置参数签名，兼容各壳调用方式
    """
    name = "MissAV 中文字幕"
    backend_parse = False
    category_mode = False

    CATEGORIES = (
        ("today-hot", "今日热门", "today_views"),
        ("weekly-hot", "本周热门", "weekly_views"),
        ("monthly-hot", "本月热门", "monthly_views"),
        ("new", "最近更新", "published_at"),
        ("release", "新作上市", "released_at"),
        ("chinese-subtitle", "中文字幕", "released_at"),
        ("uncensored-leak", "无码流出", "released_at"),
        ("fc2", "FC2", "released_at"),
        ("siro", "SIRO", "released_at"),
        ("luxu", "LUXU", "released_at"),
        ("gana", "GANA", "released_at"),
        ("heyzo", "HEYZO", "released_at"),
        ("tokyohot", "东京热", "released_at"),
        ("actresses", "女优一览", "published_at"),
        ("genres", "类型", "published_at"),
        ("makers", "发行商", "published_at"),
    )
    SORTS = (
        ("released_at", "发行日期"),
        ("published_at", "最近更新"),
        ("today_views", "今日浏览"),
        ("weekly_views", "本周浏览"),
        ("monthly_views", "本月浏览"),
        ("views", "总浏览"),
        ("saved", "收藏数"),
    )

    def __init__(self):
        self._init_subtitle_resolver()
        self.host = DEFAULT_HOST
        self.mirrors = list(DEFAULT_MIRRORS)
        self.timeout = 15
        self.gateway_timeout = 90
        self.cookie = ""
        self.playlist_timeout = 5
        self.play_quality = "best"
        self.proxy_gateway = ""
        self.flare_session = None
        self.impersonate = DEFAULT_IMPERSONATE
        self.search_api_enabled = True
        self.max_retries = 2
        # 单次取页的总时长预算（秒）：主域慢 + 多个镜像逐个超时的最坏情况不能拖死接口
        self.total_budget = 8.0
        self._request_lock = threading.Lock()
        self._curl_session = None
        self._warmed = False
        self._page_cache = {}
        self._cache_lock = threading.RLock()
        self.cache_ttl = 60
        self.warmup_enabled = True
        self._preferred_origin = ""
        self.cover_via_proxy = False
        self._cover_cache = {}
        self._cover_inflight = set()
        self._cover_lock = threading.RLock()
        self._session_mode = "tls"
        self.session = self._build_session()

    # ---------- 宿主标准接口 ----------

    # ---------- 宿主标准接口（13 项，四壳契约） ----------

    def getDependence(self):
        return ""

    def getName(self):
        return self.name

    def init(self, extend=""):
        config = _parse_config(extend)
        self.host = _normalize_origin(config.get("host"))
        raw_mirrors = str(config.get("mirrors") or ",".join(DEFAULT_MIRRORS))
        mirrors = []
        for item in re.split(r"[,\s;|]+", raw_mirrors):
            origin = _normalize_origin(item) if item.strip() else ""
            if origin and origin != self.host and origin not in mirrors:
                mirrors.append(origin)
        self.mirrors = mirrors
        self.timeout = _bounded_int(config.get("timeout"), 15, 5, 40)
        self.gateway_timeout = _bounded_int(config.get("gateway_timeout"), 90, 10, 180)
        self.cookie = str(config.get("cookie") or "").strip()
        self.playlist_timeout = _bounded_int(config.get("playlist_timeout"), 5, 2, 15)
        self.play_quality = _normalize_play_quality(config.get("play_quality"))
        self.proxy_gateway = str(config.get("proxy_gateway") or config.get("gateway_url") or "").strip()
        if _is_flaresolverr_gateway(self.proxy_gateway):
            self.proxy_gateway = self.proxy_gateway.rstrip("/")
        self.flare_session = None
        self.impersonate = _normalize_impersonate(config.get("impersonate"))
        self.search_api_enabled = _bool(config.get("search_api_enabled"), True)
        self.max_retries = _bounded_int(config.get("max_retries"), 2, 0, 6)
        self.total_budget = max(float(_bounded_int(config.get("total_budget"), 8, 3, 40)), 3.0)
        self._configure_subtitles(config)
        self._curl_session = None
        self._session_mode = "tls"
        self._warmed = False
        self.cache_ttl = _bounded_int(config.get("cache_ttl"), 60, 0, 900)
        self.warmup_enabled = _bool(config.get("warmup"), True)
        self._preferred_origin = ""
        self.cover_via_proxy = _bool(config.get("cover_via_proxy"), False)
        with self._cover_lock:
            self._cover_cache = {}
        with self._cache_lock:
            self._page_cache = {}
        self.session = self._build_session()
        # 预热：提前建立 CF 通道，避免壳首次 homeContent 时握手未就绪导致分类空白
        try:
            self._warmup()
        except Exception:
            pass
        return ""

    def _warmup(self):
        """
        预热：提前完成 TLS 握手并把首屏那一页缓存下来。
        壳的调用顺序通常是 init → homeContent(today-hot)，
        缓存后 homeContent 不会重复发请求（此前冷启动会白抓一次）。
        """
        if self._warmed or self.proxy_gateway or not self.warmup_enabled:
            return
        self._warmed = True
        try:
            url = self.host + "/cn/today-hot?" + urlencode({"sort": "today_views", "page": 1})
            html_text, final_url = self._fetch_url(url, referer=self.host + "/",
                                                   timeout=min(self.timeout, 12), retries=0)
            self._cache_put(url, (html_text, final_url))
        except Exception:
            pass

    # ---------- 页面缓存（短 TTL，抵消壳的重复调用） ----------

    def _cache_put(self, key, value):
        with self._cache_lock:
            self._page_cache[key] = (time.time(), value)
            if len(self._page_cache) > 24:
                oldest = sorted(self._page_cache.items(), key=lambda kv: kv[1][0])[:8]
                for stale_key, _ in oldest:
                    self._page_cache.pop(stale_key, None)

    def _cache_get(self, key):
        with self._cache_lock:
            hit = self._page_cache.get(key)
        if not hit:
            return None
        stamp, value = hit
        if time.time() - stamp > self.cache_ttl:
            with self._cache_lock:
                self._page_cache.pop(key, None)
            return None
        return value

    def isVideoFormat(self, url):
        text = str(url or "").lower()
        return bool(text) and bool(re.search(r"\.(?:m3u8|mp4|mkv|flv|avi|ts)(?:[?#]|$)", text))

    def manualVideoCheck(self):
        return False

    def action(self, action):
        return ""

    def destroy(self):
        for session in (getattr(self, "session", None), getattr(self, "_curl_session", None),
                        getattr(self, "_subtitle_session", None)):
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass
        return ""

    # ---------- 封面本地代理 ----------
    #
    # fourhoi.com 对壳的图片加载器（okhttp TLS 指纹）返回 403，而浏览器/指纹适配能过。
    # 直连封面在影视仓/T4 这类壳里必裂。这里提供 local:// 封面代理：
    #   vod_pic = local://cover/<urlsafe-slug>
    # 壳拿到 local:// 时会调用本源的 localProxy(param=key) 取图片字节。
    # 图片由本 Spider 用带 X25519 指纹的 session 拉取（能过 fourhoi 的 CF），再回给壳。
    # 支持该协议时封面立即可显示，不支持的壳可用配置 cover_via_proxy=off 切回直连 http。
    #
    # 注意：localProxy 是同步调用，每次列表翻页会拉若干张图；用内存缓存避免重复请求。

    def _cover_url(self, slug, detail=False):
        """返回封面 URL。cover_via_proxy 开启时走壳的本地代理（壳调 localProxy），否则直连 fourhoi。

        TVBox/Peek Pro 标准本地代理写法：http://127.0.0.1:9978/proxy?do=local&key=<param>
        壳的本地代理服务器拦截这个地址后回调本源的 localProxy(param=key) 取图片字节。
        这比 local:// 协议兼容性广，Peek Pro/TVBox/影视仓 均支持。
        """
        kind = COVER_DETAIL if detail else COVER_LIST
        if self.cover_via_proxy:
            key = base64.urlsafe_b64encode(slug.encode("utf-8")).decode("ascii").rstrip("=")
            param = "cover/" + key + "/" + kind
            return "http://127.0.0.1:9978/proxy?do=local&key=" + quote(param, safe="")
        return COVER_HOST + "/" + slug + "/" + kind

    def _resolve_cover_local(self, key):
        """local://cover/<b64slug>/<kind> -> 用指纹 session 拉图。"""
        try:
            parts = key.split("/")
            if len(parts) < 3 or parts[0] != "cover":
                return None
            kind = parts[-1]
            if kind not in (COVER_LIST, COVER_DETAIL):
                kind = COVER_LIST
            slug = base64.urlsafe_b64decode(parts[1].encode("ascii") + b"=" * (-len(parts[1]) % 4)).decode("utf-8")
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", slug, re.I):
                return None
        except Exception:
            return None
        return self._fetch_cover(COVER_HOST + "/" + slug + "/" + kind)

    def _cover_cache_get(self, url):
        with self._cover_lock:
            hit = self._cover_cache.get(url)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        return None

    def _fetch_cover(self, url):
        """拉取封面字节，带进程内并发去重 + 一次失败重试。成功结果缓存 600s。"""
        cached = self._cover_cache_get(url)
        if cached:
            return cached
        with self._cover_lock:
            if url in self._cover_inflight:
                pass  # 已有线程在拉，取到后它会写缓存；这里仍在锁外等它
            else:
                self._cover_inflight.add(url)
        # 若别的线程正在拉，等它结果
        for _ in range(40):
            cached = self._cover_cache_get(url)
            if cached:
                break
            time.sleep(0.05)
        if cached:
            return cached
        # 真正拉取（带 1 次幂等重试）
        data, ctype = self._http_cover(url)
        if not data:
            data, ctype = self._http_cover(url)
        if data:
            try:
                with self._cover_lock:
                    self._cover_cache[url] = (time.time(), (data, ctype))
                    if len(self._cover_cache) > 80:
                        stale = sorted(self._cover_cache.items(), key=lambda kv: kv[1][0])[:12]
                        for k, _ in stale:
                            self._cover_cache.pop(k, None)
            except Exception:
                pass
        with self._cover_lock:
            self._cover_inflight.discard(url)
        if data:
            return data, ctype
        return None, None

    def _http_cover(self, url):
        data, ctype = None, "image/jpeg"
        try:
            resp = self.session.get(url,
                                    headers={"User-Agent": PLAYER_UA, "Referer": self.host + "/"},
                                    timeout=min(self.timeout, 10))
            if resp.status_code == 200:
                data = resp.content
                ctype = str(resp.headers.get("content-type") or "image/jpeg")
        except Exception:
            data = None
        return data, ctype

    def prefetch_cover(self, slug):
        """后台预取某影片封面，供列表/详情复用；不阻塞调用方。"""
        try:
            url = COVER_HOST + "/" + slug + "/" + COVER_LIST
            if self._cover_cache_get(url):
                return
            threading.Thread(target=self._fetch_cover, args=(url,), daemon=True).start()
        except Exception:
            pass

    def _prefetch_page_covers(self, items, max_workers=6):
        """列表返回后后台并发拉取本页全部封面，localProxy 命中缓存 → 壳逐张调用秒回。"""
        urls = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            pic = str(item.get("vod_pic") or "")
            slug = None
            # 兼容 local://cover/<b64>/<kind> 与 http://127.0.0.1:9978/proxy?do=local&key=cover/<b64>/<kind>
            if pic.startswith("local://"):
                try:
                    parts = pic[len("local://"):].split("/")
                    slug = base64.urlsafe_b64decode(parts[1].encode("ascii") + b"=" * (-len(parts[1]) % 4)).decode("utf-8")
                except Exception:
                    slug = None
            elif "do=local" in pic and "key=" in pic:
                try:
                    key = pic.split("key=", 1)[1].split("&", 1)[0]
                    key = unquote(key)
                    if key.startswith("cover/"):
                        parts = key.split("/")
                        slug = base64.urlsafe_b64decode(parts[1].encode("ascii") + b"=" * (-len(parts[1]) % 4)).decode("utf-8")
                except Exception:
                    slug = None
            if slug and re.fullmatch(r"[a-z0-9][a-z0-9._-]+", slug, re.I):
                urls.append(COVER_HOST + "/" + slug + "/" + COVER_LIST)
        if not urls:
            return
        unique = list(dict.fromkeys(urls))
        todo = [u for u in unique if not self._cover_cache_get(u)]
        if not todo:
            return

        def worker(u):
            try:
                self._fetch_cover(u)
            except Exception:
                pass

        # 有界并发拉取全部
        from concurrent.futures import ThreadPoolExecutor as _TPE
        try:
            with _TPE(max_workers=max_workers) as executor:
                executor.map(worker, todo)
        except Exception:
            for u in todo:
                worker(u)

    def localProxy(self, param):
        """
        封面本地代理：返回封面图片字节。兼容壳的三种调用形态：
          - http://127.0.0.1:9978/proxy?do=local&key=<param>  → param 是完整 URL
          - do=local&key=<param>（TVBoxOSC/影视仓/T4 query string 形态）
          - local://cover/<b64slug>/<kind> 或裸 cover/<b64slug>/<kind>
        解出 cover/<b64slug>/<kind> 后走指纹 session 拉图；
        成功返回 dict，失败返回 [404,'text/plain','']（勿返回 None/空串）。
        """
        value = str(param or "")
        if "?" in value:
            try:
                qs = urlsplit(value).query
                value = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv).get("key", value)
            except Exception:
                pass
        elif "key=" in value:
            try:
                value = dict(kv.split("=", 1) for kv in value.split("&") if "=" in kv).get("key", value)
            except Exception:
                pass
        if value.startswith("local://"):
            value = value[len("local://"):]
        try:
            value = unquote(value)
        except Exception:
            pass
        if not value.startswith("cover/"):
            return [404, "text/plain", ""]
        resolved = self._resolve_cover_local(value)
        if not resolved:
            return [404, "text/plain", ""]
        data, ctype = resolved
        if not data:
            return [404, "text/plain", ""]
        return {"code": 200, "content": data, "headers": {"Content-Type": ctype}}

    # ---------- 传输层 ----------

    def _user_agent(self):
        return IMPERSONATE_UA.get(self.impersonate, DEFAULT_UA)

    def _build_session(self):
        return build_tls_session(self._user_agent(), self.cookie)

    def _build_curl_session(self, impersonate=None):
        if not HAS_CURL_CFFI:
            return None
        target = impersonate or self.impersonate
        try:
            session = curl_requests.Session(impersonate=target)
        except Exception:
            try:
                session = curl_requests.Session()
            except Exception:
                return None
        headers = dict(NAV_HEADERS)
        headers["User-Agent"] = IMPERSONATE_UA.get(target, DEFAULT_UA)
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            session.headers.update(headers)
        except Exception:
            pass
        return session

    def _request_headers(self, referer):
        headers = {"User-Agent": self._user_agent()}
        headers.update(NAV_HEADERS)
        headers["User-Agent"] = self._user_agent()
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _fetch_url(self, url, referer=None, timeout=None, retries=None):
        """
        统一取页入口。顺序：
        1. 外部网关（若配置）
        2. requests + CF 指纹兼容 TLS 适配层
        3. curl_cffi 指纹通道（多指纹轮换，若可用）
        4. 备用域名重放（成功的域名会被记住，后续请求优先走它，
           避免主域已挂时每个请求都先白等一次超时）
        """
        if timeout is None:
            timeout = self.timeout
        if retries is None:
            retries = self.max_retries if not self.proxy_gateway else 0

        if self.proxy_gateway:
            return self._fetch_via_gateway(url, referer, timeout)

        # 总预算：避免主域慢 + 5 个镜像逐个超时叠加成十几秒
        deadline = time.time() + max(self.total_budget, timeout)
        errors = []
        for candidate in self._url_candidates(url):
            if time.time() >= deadline and errors:
                errors.append("超出总时长预算")
                break
            try:
                result = self._fetch_direct(candidate, referer, timeout, retries, deadline=deadline)
                self._remember_origin(candidate)
                return result
            except Exception as exc:
                errors.append("%s -> %s" % (urlsplit(candidate).netloc, _clean_text(exc)[:80]))
            if time.time() < deadline:
                curl_text = self._fetch_via_curl(candidate, referer, timeout, deadline=deadline)
                if curl_text is not None:
                    self._remember_origin(candidate)
                    return curl_text
        raise ValueError("；".join(errors) if errors else "请求失败")

    def _remember_origin(self, url):
        try:
            parsed = urlsplit(url)
        except Exception:
            return
        if parsed.scheme and parsed.netloc:
            self._preferred_origin = parsed.scheme + "://" + parsed.netloc

    def _url_candidates(self, url):
        try:
            parsed = urlsplit(url)
        except Exception:
            return [url]
        origin = parsed.scheme + "://" + parsed.netloc
        known = [self.host] + list(self.mirrors)
        if origin not in known:
            # 非站点域（如 surrit CDN）不做镜像替换
            return [url]
        order = []
        if self._preferred_origin and self._preferred_origin in known:
            order.append(self._preferred_origin)
        for item in known:
            if item not in order:
                order.append(item)
        candidates = []
        for item in order:
            replaced = url.replace(origin, item, 1)
            if replaced not in candidates:
                candidates.append(replaced)
        return candidates

    def _fetch_direct(self, url, referer, timeout, retries, deadline=None):
        headers = self._request_headers(referer)
        last_exc = None
        for attempt in range(max(retries, 0) + 1):
            if deadline is not None and time.time() >= deadline:
                break
            slot = timeout
            if deadline is not None:
                slot = max(min(timeout, deadline - time.time()), 2)
            try:
                response = self.session.get(url, headers=headers, timeout=slot, allow_redirects=True)
                verdict = _classify_response(response)
                if verdict == "cloudflare-managed-challenge":
                    if attempt < retries and self._can_wait(deadline, 1.5):
                        time.sleep(self._backoff_seconds(response, attempt))
                        continue
                    raise ValueError("Cloudflare 挑战页")
                if verdict == "rate-limited":
                    if attempt < retries and self._can_wait(deadline, 1.5):
                        time.sleep(self._backoff_seconds(response, attempt))
                        continue
                    raise ValueError("请求被限流 (429)")
                if verdict in ("http-error", "upstream-error"):
                    raise ValueError("HTTP %s" % getattr(response, "status_code", "?"))
                if verdict == "empty-response":
                    raise ValueError("空响应")
                return response.text, str(getattr(response, "url", url))
            except Exception as exc:
                last_exc = exc
                if attempt < retries and self._can_wait(deadline, 1.0):
                    time.sleep(min(0.6 * (attempt + 1), 2.0))
                    continue
                break
        raise last_exc or RuntimeError("请求失败")

    @staticmethod
    def _can_wait(deadline, need):
        return deadline is None or (deadline - time.time()) > need

    def _fetch_via_curl(self, url, referer, timeout, deadline=None):
        """curl_cffi 指纹通道；不可用或全部失败时返回 None，由上层继续回退。"""
        if not HAS_CURL_CFFI:
            return None
        order = [self.impersonate] + [fp for fp in IMPERSONATE_FALLBACK if fp != self.impersonate]
        for fingerprint in order:
            if deadline is not None and time.time() >= deadline:
                return None
            session = self._curl_session if (self._curl_session is not None and fingerprint == self.impersonate) else self._build_curl_session(fingerprint)
            if session is None:
                continue
            try:
                headers = {}
                if referer:
                    headers["Referer"] = referer
                slot = timeout
                if deadline is not None:
                    slot = max(min(timeout, deadline - time.time()), 2)
                response = session.get(url, headers=headers, timeout=slot)
                if _classify_response(response) != "ok":
                    continue
                self._curl_session = session
                self.impersonate = fingerprint
                return response.text, str(getattr(response, "url", url))
            except Exception:
                continue
        return None

    def _fetch_via_gateway(self, url, referer, timeout):
        gateway_url = self.proxy_gateway
        if _is_flaresolverr_gateway(gateway_url):
            solver_timeout = max(int(timeout), self.gateway_timeout)
            if self.flare_session is None:
                self.flare_session = self._create_flare_session(gateway_url, solver_timeout)
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": solver_timeout * 1000,
                "session": self.flare_session,
                "headers": {
                    "Referer": referer or self.host + "/",
                    "Accept": NAV_HEADERS["Accept"],
                    "Accept-Language": NAV_HEADERS["Accept-Language"],
                },
            }
            try:
                response = requests.post(gateway_url, json=payload, timeout=solver_timeout + 10)
                response.raise_for_status()
                data = response.json()
                if data.get("status") != "ok":
                    raise ValueError("FlareSolverr 错误: %s" % data.get("message", ""))
                solution = data.get("solution")
                if not isinstance(solution, dict):
                    raise ValueError("FlareSolverr 未返回有效 solution")
                for cookie in solution.get("cookies") or []:
                    try:
                        self.session.cookies.set(cookie["name"], cookie["value"])
                    except Exception:
                        pass
                if solution.get("userAgent"):
                    self.session.headers.update({"User-Agent": solution["userAgent"]})
                content = solution.get("response")
                if content is None:
                    content = solution.get("body") or ""
                if not str(content).strip():
                    raise ValueError("FlareSolverr 返回空响应")
                return str(content), str(solution.get("url") or solution.get("finalUrl") or url)
            except Exception as exc:
                raise ValueError("FlareSolverr 调用失败: %s" % _clean_text(exc))

        target = gateway_url + ("&" if "?" in gateway_url else "?") + "url=" + quote(url, safe="")
        headers = {"User-Agent": self._user_agent()}
        if referer:
            headers["Referer"] = referer
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            response = requests.get(target, headers=headers, timeout=timeout + 5)
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "")
            if "application/json" in content_type:
                data = response.json()
                encoded = data.get("body_base64")
                if encoded:
                    content = base64.b64decode(str(encoded)).decode("utf-8", errors="replace")
                else:
                    content = data.get("content") or data.get("body") or data.get("text") or response.text
                return content, str(response.url)
            return response.text, str(response.url)
        except Exception as exc:
            raise ValueError("网关请求失败: %s" % _clean_text(exc))

    def _create_flare_session(self, gateway_url, solver_timeout):
        try:
            response = requests.post(gateway_url, json={"cmd": "sessions.create"}, timeout=solver_timeout + 10)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "ok":
                raise ValueError("FlareSolverr 会话创建错误: %s" % data.get("message", ""))
            session_id = data.get("session")
            if not session_id:
                raise ValueError("FlareSolverr 未返回 session")
            return str(session_id)
        except Exception as exc:
            raise ValueError("FlareSolverr 会话创建失败: %s" % _clean_text(exc))

    @staticmethod
    def _backoff_seconds(response, attempt):
        try:
            retry_after = int(str((getattr(response, "headers", {}) or {}).get("Retry-After") or "0"))
            if retry_after > 0:
                return min(retry_after, 30)
        except Exception:
            pass
        return min(2 ** (attempt + 1), 15)

    def _request_html(self, url, referer):
        return self._fetch_url(url, referer)

    # ---------- 列表 / 分类 ----------

    def homeContent(self, filter=False):
        result = {
            "class": [{"type_id": item[0], "type_name": item[1]} for item in self.CATEGORIES],
            "filters": self._filters(),
            "list": [],
        }
        try:
            result["list"] = self.categoryContent("today-hot", 1, False, {}).get("list", [])
        except Exception:
            result["list"] = []
        return result

    def _filters(self):
        """四壳契约：filters 必须是 dict（空数组会让部分新壳分类渲染退化）。"""
        options = [{"n": label, "v": value} for value, label in self.SORTS]
        base_filters = {
            item[0]: [{"key": "sort", "name": "排序", "init": item[2], "value": options}]
            for item in self.CATEGORIES
        }
        actress_filters = [
            {"key": "height", "name": "身高", "init": "", "value": [
                {"n": "一级筛选", "v": ""},
                {"n": "131 - 135cm", "v": "131-135"}, {"n": "136 - 140cm", "v": "136-140"},
                {"n": "141 - 145cm", "v": "141-145"}, {"n": "146 - 150cm", "v": "146-150"},
                {"n": "151 - 155cm", "v": "151-155"}, {"n": "156 - 160cm", "v": "156-160"},
                {"n": "161 - 165cm", "v": "161-165"}, {"n": "166 - 170cm", "v": "166-170"},
                {"n": "171 - 175cm", "v": "171-175"}, {"n": "176 - 180cm", "v": "176-180"},
                {"n": "181 - 185cm", "v": "181-185"}, {"n": "186 - 190cm", "v": "186-190"},
            ]},
            {"key": "cup", "name": "罩杯", "init": "", "value": [
                {"n": "一级筛选", "v": ""},
                {"n": "A 罩杯", "v": "A"}, {"n": "B 罩杯", "v": "B"},
                {"n": "C 罩杯", "v": "C"}, {"n": "D 罩杯", "v": "D"},
                {"n": "E 罩杯", "v": "E"}, {"n": "F 罩杯", "v": "F"},
                {"n": "G 罩杯", "v": "G"}, {"n": "H 罩杯", "v": "H"},
                {"n": "I 罩杯", "v": "I"}, {"n": "J 罩杯", "v": "J"},
                {"n": "K 罩杯", "v": "K"}, {"n": "L 罩杯", "v": "L"},
                {"n": "M 罩杯", "v": "M"}, {"n": "N 罩杯", "v": "N"},
                {"n": "O 罩杯", "v": "O"}, {"n": "P 罩杯", "v": "P"},
                {"n": "Q 罩杯", "v": "Q"},
            ]},
            {"key": "age", "name": "年龄", "init": "", "value": [
                {"n": "一级筛选", "v": ""},
                {"n": "< 20", "v": "0-20"}, {"n": "20 - 30", "v": "20-30"},
                {"n": "30 - 40", "v": "30-40"}, {"n": "40 - 50", "v": "40-50"},
                {"n": "50 - 60", "v": "50-60"}, {"n": "> 60", "v": "60-99"},
            ]},
            {"key": "debut", "name": "出道年份", "init": "", "value": [
                {"n": "一级筛选", "v": ""},
                {"n": "2025 以前", "v": "2025"}, {"n": "2024 以前", "v": "2024"},
                {"n": "2023 以前", "v": "2023"}, {"n": "2022 以前", "v": "2022"},
                {"n": "2021 以前", "v": "2021"}, {"n": "2020 以前", "v": "2020"},
                {"n": "2019 以前", "v": "2019"}, {"n": "2018 以前", "v": "2018"},
                {"n": "2017 以前", "v": "2017"}, {"n": "2016 以前", "v": "2016"},
                {"n": "2015 以前", "v": "2015"}, {"n": "2014 以前", "v": "2014"},
                {"n": "2013 以前", "v": "2013"}, {"n": "2012 以前", "v": "2012"},
                {"n": "2011 以前", "v": "2011"}, {"n": "2010 以前", "v": "2010"},
                {"n": "2009 以前", "v": "2009"}, {"n": "2008 以前", "v": "2008"},
                {"n": "2007 以前", "v": "2007"}, {"n": "2006 以前", "v": "2006"},
                {"n": "2005 以前", "v": "2005"}, {"n": "2004 以前", "v": "2004"},
                {"n": "2003 以前", "v": "2003"}, {"n": "2002 以前", "v": "2002"},
                {"n": "2001 以前", "v": "2001"}, {"n": "2000 以前", "v": "2000"},
            ]},
        ]
        base_filters["actresses"] = base_filters.get("actresses", []) + actress_filters
        return base_filters

    def homeVideoContent(self):
        return self.categoryContent("today-hot", 1, False, {})

    def categoryContent(self, tid, pg, filter=False, extend=None):
        page = _bounded_int(pg, 1, 1, 100000)
        options = _parse_config(extend)
        slug = str(tid or "").strip().strip("/")
        # 兼容旧版 tid（today/weekly/monthly...）与带 dmXXX 前缀的历史值
        legacy = {
            "today": "today-hot", "weekly": "weekly-hot", "monthly": "monthly-hot",
            "chinese": "chinese-subtitle", "uncensored": "uncensored-leak",
        }
        slug = legacy.get(slug, slug)
        slug = re.sub(r"^dm\d+/", "", slug)
        slug = re.sub(r"^(?:cn|en)/", "", slug)
        # 目录类分类（女优/类型/发行商）走独立解析
        if slug in ("actresses", "genres", "makers"):
            return self._dir_page(slug, page, options)
        category = next((item for item in self.CATEGORIES if item[0] == slug), None)
        default_sort = category[2] if category else "released_at"
        sort_value = str(options.get("sort") or default_sort).strip()
        if sort_value not in {item[0] for item in self.SORTS}:
            sort_value = default_sort
        if not slug:
            return self._empty_page(page)
        url = self.host + "/cn/" + slug + "?" + urlencode({"sort": sort_value, "page": page})
        return self._list_page(url, page)

    def searchContent(self, key, quick=False, pg="1"):
        keyword = _clean_text(key)
        page = _bounded_int(pg, 1, 1, 100000)
        if not keyword:
            return self._empty_page(page)
        result = None
        try:
            url = self.host + "/cn/search/" + quote(keyword, safe="") + "?" + urlencode({"page": page})
            result = self._list_page(url, page, tolerate_empty=True)
        except Exception:
            result = None
        if not (result and result.get("list")):
            api_result = self._recombee_search(keyword, page)
            if api_result is not None and api_result.get("list"):
                result = api_result
        if not result:
            return self._empty_page(page)
        requested_code = extract_video_code(keyword)
        if requested_code:
            filtered = [
                item for item in result.get("list", [])
                if extract_video_code(item.get("vod_name"), item.get("vod_id")) == requested_code
            ]
            if filtered:
                result["list"] = filtered
                result["limit"] = len(filtered)
                result["total"] = len(filtered)
        return result

    def _recombee_search(self, keyword, page):
        """站点搜索页不可用时的回退（Recombee 公开搜索接口，无分页）。"""
        if not self.search_api_enabled or page != 1:
            return None
        try:
            import hashlib
            import hmac
        except Exception:
            return None
        timestamp = int(time.time())
        user_id = "anon_missav_%x" % timestamp
        unsigned = "/%s/search/users/%s/items/?frontend_timestamp=%d" % (
            SEARCH_API_DATABASE, quote(user_id, safe=""), timestamp
        )
        signature = hmac.new(SEARCH_API_TOKEN.encode("utf-8"), unsigned.encode("utf-8"), hashlib.sha1).hexdigest()
        url = SEARCH_API_HOST + unsigned + "&frontend_sign=" + signature
        body = {"searchQuery": keyword, "count": 40, "cascadeCreate": True, "returnProperties": True}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.host,
            "Referer": self.host + "/",
            "User-Agent": self._user_agent(),
        }
        payload = None
        for session in (self.session, requests):
            try:
                response = session.post(url, headers=headers, json=body, timeout=self.timeout)
                if _classify_response(response) != "ok":
                    continue
                payload = response.json()
                break
            except Exception:
                continue
        rows = payload.get("recomms") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            values = row.get("values") if isinstance(row.get("values"), dict) else {}
            slug = _clean_text(row.get("id"))
            if not slug:
                continue
            title = _clean_text(
                values.get("title_cn") or values.get("title_zh") or values.get("title") or slug
            )
            code = extract_video_code(slug, title)
            if code and code not in title.upper():
                title = code + " " + title
            duration = _bounded_int(values.get("duration"), 0, 0, 86400)
            remarks = _format_duration(duration) or code
            if _bool(values.get("has_chinese_subtitle"), False):
                remarks = (remarks + " · 中文字幕").strip(" ·")
            items.append({
                "vod_id": self.host + "/cn/" + quote(slug, safe="-"),
                "vod_name": title,
                "vod_pic": self._cover_url(slug, False),
                "vod_remarks": remarks,
                "vod_year": "",
                "vod_area": "",
                "vod_type": "",
                "vod_content": _clean_text(values.get("title_en") or values.get("title") or ""),
            })
        return {
            "page": page,
            "pagecount": page,
            "limit": len(items) or PAGE_SIZE,
            "total": len(items),
            "list": items,
        }

    def _list_page(self, url, page, tolerate_empty=False):
        try:
            cached = self._cache_get(url)
            if cached is not None:
                html_text, final_url = cached
            else:
                html_text, final_url = self._fetch_url(url, referer=self.host + "/")
                self._cache_put(url, (html_text, final_url))
            items = self._parse_list(html_text, final_url)
            if not items and tolerate_empty:
                return {"page": page, "pagecount": page, "limit": PAGE_SIZE, "total": 0, "list": []}
            pagecount = self._parse_pagecount(html_text, page)
            if items and pagecount <= page:
                pagecount = page + 1
            limit = len(items) or PAGE_SIZE
            # 后台预取本页全部封面：壳随后逐张调 localProxy 时命中缓存 → 秒回
            if self.cover_via_proxy:
                self._prefetch_page_covers(items)
            return {
                "page": page,
                "pagecount": pagecount,
                "limit": limit,
                "total": pagecount * limit,
                "list": items,
            }
        except Exception as exc:
            if tolerate_empty:
                raise
            message = _clean_text(exc) or "列表加载失败"
            return {
                "page": page,
                "pagecount": page,
                "limit": PAGE_SIZE,
                "total": 1,
                "list": [{
                    "vod_id": STATUS_PREFIX + message,
                    "vod_name": "访问受限：" + message,
                    "vod_pic": "",
                    "vod_remarks": "可在插件配置里填写代理网关或更换备用域名",
                    "vod_year": "",
                    "vod_area": "",
                    "vod_type": "",
                }],
            }

    def _parse_list(self, html_text, base_url):
        """
        先按卡片容器切块，再在块内解析，避免相邻卡片内容互相串位。
        锚点优先级：class="thumbnail group" 容器 → fourhoi 封面地址窗口。
        """
        text = str(html_text or "")
        items = []
        seen = set()
        for block in self._card_blocks(text):
            match = CARD_COVER_RE.search(block)
            if not match:
                continue
            slug = match.group(1)
            if not slug or slug.lower() in ("img", "flags", "logo"):
                continue
            href = self._card_href(block, slug, base_url) or (self.host + "/cn/" + slug)
            key = href.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            code = extract_video_code(SLUG_SUFFIX_RE.sub("", slug))
            title = self._card_title(block, slug) or code or slug
            if code and code not in title.upper():
                title = code + " " + title
            remarks = self._card_remarks(block) or code
            if re.search(r"-chinese-subtitle$", slug, re.I):
                remarks = (remarks + " · 中文字幕").strip(" ·")
            elif re.search(r"-uncensored-leak$", slug, re.I):
                remarks = (remarks + " · 无码流出").strip(" ·")
            items.append({
                "vod_id": href,
                "vod_name": _clean_text(title),
                "vod_pic": self._cover_url(slug, False),
                "vod_remarks": _clean_text(remarks),
                "vod_year": "",
                "vod_area": "",
                "vod_type": "",
            })
        if items:
            return items
        return self._parse_list_fallback(text, base_url)

    @staticmethod
    def _card_blocks(text):
        """按卡片容器切块；模板变更时退化为封面地址前后窗口。"""
        anchors = [m.start() for m in re.finditer(r'class="thumbnail group"', text)]
        if len(anchors) >= 2:
            blocks = []
            for index, start in enumerate(anchors):
                end = anchors[index + 1] if index + 1 < len(anchors) else len(text)
                blocks.append(text[start:end])
            return blocks
        blocks = []
        positions = [m.start() for m in CARD_COVER_RE.finditer(text)]
        for index, start in enumerate(positions):
            begin = max(0, start - 1500)
            end = positions[index + 1] if index + 1 < len(positions) else min(len(text), start + 2500)
            blocks.append(text[begin:min(end, start + 2500)])
        return blocks

    def _card_href(self, block, slug, base_url):
        pattern = re.compile(r'href="([^"]*/' + re.escape(slug) + r')"', re.I)
        match = pattern.search(block)
        if match:
            return urljoin(base_url, match.group(1))
        return ""

    @staticmethod
    def _card_title(block, slug):
        # 卡片标题固定在 "my-2 text-sm ... truncate" 容器里的 <a> 文本
        match = re.search(r'truncate"[^>]*>\s*<a[^>]*>\s*([^<]{4,200}?)\s*</a>', block, re.I | re.S)
        if match:
            cleaned = _clean_text(match.group(1))
            if cleaned:
                return cleaned
        for value in re.findall(r'alt="([^"]{6,})"', block):
            cleaned = _clean_text(value)
            if cleaned and cleaned.lower() != slug.lower() and not cleaned.startswith("http"):
                return cleaned
        for value in re.findall(r'>\s*([^<>{}\d][^<>{}]{8,150})\s*<', block):
            cleaned = _clean_text(value)
            if cleaned and not cleaned.startswith("{") and "function" not in cleaned:
                return cleaned
        return ""

    @staticmethod
    def _card_remarks(block):
        match = re.search(r">\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*<", block)
        return match.group(1) if match else ""

    def _parse_list_fallback(self, text, base_url):
        """无 fourhoi 封面时（模板变更）的兜底：按详情链接结构提取。"""
        if not HAS_LXML:
            return []
        try:
            document = lxml_html.fromstring(text)
        except Exception:
            return []
        items = []
        seen = set()
        for link in document.xpath("//a[@href]"):
            images = link.xpath(".//img[1]")
            if not images:
                continue
            image = images[0]
            source = str(image.get("data-src") or image.get("src") or "")
            if "/flags/" in source or source.startswith("data:"):
                continue
            href = urljoin(base_url, str(link.get("href") or "").strip())
            parsed = urlsplit(href)
            match = re.search(r"/(?:dm\d+/)?(?:[a-z]{2,3}/)?([a-z0-9][a-z0-9-]+)$", parsed.path, re.I)
            if not match:
                continue
            slug = match.group(1)
            code = extract_video_code(SLUG_SUFFIX_RE.sub("", slug))
            if not code:
                continue
            key = parsed.scheme + "://" + parsed.netloc + parsed.path
            if key in seen:
                continue
            seen.add(key)
            title = _clean_text(link.get("title") or image.get("alt") or link.text_content()) or code
            if code not in title.upper():
                title = code + " " + title
            items.append({
                "vod_id": key,
                "vod_name": title,
                "vod_pic": self._cover_url(slug, False),
                "vod_remarks": code,
                "vod_year": "",
                "vod_area": "",
                "vod_type": "",
            })
        return items

    @staticmethod
    def _parse_pagecount(html_text, current):
        """
        真实总页数在分页表单右侧 `id="price-currency"` 的 "/ N" 里（实测 2000），
        取不到时退化为分页链接中的最大 page 值。
        """
        text = str(html_text or "")
        match = re.search(r'id="price-currency"[^>]*>\s*/?\s*([\d,]+)', text)
        if match:
            total = _bounded_int(match.group(1).replace(",", ""), 0, 0, 100000)
            if total >= current:
                return total
        pages = [current]
        for item in re.finditer(r"[?&]page=(\d+)", text, re.I):
            pages.append(_bounded_int(item.group(1), current, 1, 100000))
        return max(pages)

    # ---------- 详情 / 播放 ----------

    def detailContent(self, ids):
        source_id = ids[0] if isinstance(ids, (list, tuple)) and ids else ids
        value = str(source_id or "").strip()
        if value.startswith(ATVP_DETAIL_PREFIX):
            value = value[len(ATVP_DETAIL_PREFIX):]
        if value.startswith(STATUS_PREFIX):
            return {"list": [self._status_detail(value[len(STATUS_PREFIX):])]}
        if not value or not re.search(r"[a-z0-9]", value, re.I):
            return {"list": [self._error_detail("", "", "MissAV", "", "缺少有效的详情标识")]}
        if not self._slug_of(value):
            return {"list": [self._error_detail(value, "", "MissAV", "", "详情地址无效")]}
        detail_url = urljoin(self.host + "/", value)
        try:
            html_text, final_url = self._fetch_url(detail_url, referer=self.host + "/")
        except Exception as exc:
            return {"list": [self._error_detail(detail_url, extract_video_code(detail_url), "MissAV", "", _clean_text(exc))]}
        try:
            slug = self._slug_of(final_url) or self._slug_of(detail_url)
            title = self._meta(html_text, "og:title") or self._html_title(html_text)
            title = re.sub(r"\s*[-|]\s*MissAV.*$", "", title, flags=re.I).strip()
            code = extract_video_code(title, SLUG_SUFFIX_RE.sub("", slug), final_url)
            if code and code not in title.upper():
                title = (code + " " + title).strip()
            picture = self._cover_url(slug, True) if slug else self._meta(html_text, "og:image")
            description = self._meta(html_text, "og:description") or self._meta(html_text, "description")
            duration = _bounded_int(self._meta(html_text, "og:video:duration"), 0, 0, 86400)
            sources = extract_play_sources(html_text)
            uuid = self._extract_uuid(html_text)
            if not sources and not uuid:
                return {"list": [self._error_detail(final_url, code, title, picture, "未解析到播放源（页面结构可能已变更）")]}
            # 多线路：按页面实际暴露的清晰度分线（$$$ 分隔），每线单集（集名$地址）
            froms, urls = self._build_play_lines(final_url, uuid, code, duration, sources)
            remarks = " · ".join(item for item in (code, _format_duration(duration)) if item)
            vod = {
                "vod_id": final_url,
                "vod_name": title or code or "MissAV",
                "vod_pic": picture,
                "vod_remarks": remarks,
                "vod_year": self._detail_field(html_text, "發行日期", "发行日期")[:4] or self._meta_year(html_text),
                "vod_area": "日本",
                "vod_lang": "日语",
                "vod_actor": self._detail_field(html_text, "女優", "女优", "演員", "演员"),
                "vod_director": self._detail_field(html_text, "導演", "导演"),
                "vod_type": self._detail_field(html_text, "類型", "类型", "標籤", "标签") or "有码",
                "vod_content": _clean_text(description),
                "vod_play_from": "$$$".join(froms),
                "vod_play_url": "$$$".join(urls),
            }
            # 详情阶段就后台预取字幕：用户从详情页点到播放通常有 1~数秒间隔，
            # 到 playerContent 时缓存已命中，既不拖慢起播又能挂上字幕。
            if code and self._subtitle_enabled and not self._subtitle_blocking:
                try:
                    self._resolve_subtitle(code, wait=0)
                except Exception:
                    pass
            return {"list": [vod]}
        except Exception as exc:
            return {"list": [self._error_detail(detail_url, extract_video_code(detail_url), "MissAV", "", _clean_text(exc))]}

    def _build_play_lines(self, detail_url, uuid, code, duration, sources):
        """
        依据页面解包出的 source/sourceNNN 生成多线路：
        每条线路固定一个清晰度，播放时不再二次探测，起播更快。
        """
        variants = []
        for name, url in (sources or {}).items():
            if name == "source":
                continue
            height = self._source_height(name, url)
            variants.append((height, url))
        unique = {}
        for height, url in variants:
            if url not in unique or height > unique[url]:
                unique[url] = height
        ordered = sorted(((h, u) for u, h in unique.items()), key=lambda item: item[0], reverse=True)

        lines = []
        seen_labels = set()
        for height, url in ordered:
            label = ("%dP" % height) if height else "直链"
            if label in seen_labels:
                continue
            seen_labels.add(label)
            lines.append((height, "MissAV " + label, url, str(height or "")))
        # 低于页面最低档的目标（如 480P）：从主清单补一条真实低档线
        master = (sources or {}).get("source") or (
            ("https://surrit.com/%s/playlist.m3u8" % uuid) if uuid else ""
        )
        target = _normalize_play_quality(self.play_quality)
        if target not in ("auto", "best") and master:
            lowest = min((h for h, _ in ordered if h), default=0)
            if lowest and lowest > int(target):
                low_url = self._refine_low_quality("", master, target)
                low_height = self._source_height("", low_url)
                label = "MissAV %dP" % low_height if low_height else "MissAV 低码率"
                if low_url and label not in seen_labels:
                    seen_labels.add(label)
                    lines.append((low_height, label, low_url, str(low_height or target)))
        # 自适应线路（主清单交播放器自选，也是无 sources 时的唯一线路）
        if master:
            lines.append((0, "MissAV 自适应", master, "auto"))

        # play_quality 决定默认线路顺序：壳默认选第一条线
        lines = self._order_lines(lines)

        froms, urls = [], []
        for _, label, url, quality in lines:
            play_id = PLAY_PREFIX + self._encode_payload({
                "detail": detail_url, "uuid": uuid, "code": code,
                "duration": duration, "src": sources, "q": quality, "u": url,
            })
            froms.append(label)
            urls.append("正片$" + play_id)
        if not froms:
            play_id = PLAY_PREFIX + self._encode_payload({
                "detail": detail_url, "uuid": uuid, "code": code,
                "duration": duration, "src": sources,
            })
            froms.append("MissAV")
            urls.append("正片$" + play_id)
        return froms, urls

    def _order_lines(self, lines):
        """按配置的 play_quality 把首选清晰度排到第一条线（壳默认播它）。"""
        selected = _normalize_play_quality(self.play_quality)

        def rank(item):
            height, _, _, quality = item
            if selected == "auto":
                return (0 if quality == "auto" else 1, -height)
            if selected == "best":
                return (1 if quality == "auto" else 0, -height)
            target = int(selected)
            if quality == "auto":
                return (2, 0)
            if height and height <= target:
                return (0, -height)
            return (1, height)

        return sorted(lines, key=rank)

    @staticmethod
    def _detail_field(html_text, *labels):
        """
        详情信息行结构：<div class="text-secondary"><span>女优:</span><a>名字</a>...</div>
        取块内所有锚文本，逗号拼接。类型/标签行可能很长，窗口给到 3000 字符。
        """
        text = str(html_text or "")
        for label in labels:
            pattern = re.compile(
                r">\s*" + re.escape(label) + r"\s*[:：]\s*</span>(.{0,3000}?)</div>",
                re.I | re.S,
            )
            match = pattern.search(text)
            if not match:
                continue
            body = match.group(1)
            values = re.findall(r">\s*([^<>]{1,60})\s*<", body)
            if not values:
                values = [re.sub(r"<[^>]+>", " ", body)]
            cleaned = []
            for item in values:
                value = _clean_text(item).strip(",，、 ")
                if value and value not in cleaned:
                    cleaned.append(value)
            if cleaned:
                return ",".join(cleaned)[:200]
        return ""

    def playerContent(self, flag, id, vipFlags=None):
        try:
            payload = self._decode_play_id(id)
            detail_url = str(payload.get("detail") or self.host + "/")
            # 线路已在详情阶段锁定清晰度（q/u），优先直接使用，避免二次探测
            line_url = str(payload.get("u") or "").strip()
            line_quality = str(payload.get("q") or "").strip()
            if line_url and line_quality and line_quality != "auto":
                # 线路已锁定固定清晰度，直接用
                video_url = line_url
            elif line_url and line_quality == "auto":
                # 自适应线路：始终返回 HLS 主清单，由播放器自行选档
                # （hls 字幕模式必须是具体档位，才回退到最高档）
                if self._subtitle_mode == "hls":
                    sources = payload.get("src") if isinstance(payload.get("src"), dict) else {}
                    video_url = self._pick_source(sources, "best") or line_url
                else:
                    video_url = line_url
            else:
                quality = self.play_quality
                if quality == "auto" and self._subtitle_mode == "hls":
                    quality = "best"
                sources = payload.get("src") if isinstance(payload.get("src"), dict) else {}
                video_url = self._pick_source(sources, quality)
                # 页面内档位不足（多数片源只暴露 720p/1080p）时，回读主清单取更低档
                video_url = self._refine_low_quality(video_url, sources.get("source") or "", quality)
                if not video_url:
                    video_url = self._resolve_video_url(str(payload.get("uuid") or ""), detail_url, quality)
            if not video_url:
                raise ValueError("未解析到可播放的 m3u8")
            result = {
                "parse": 0,
                "jx": 0,
                "playUrl": "",
                "url": video_url,
                "header": {
                    "User-Agent": PLAYER_UA,
                    # surrit CDN 必须带 Referer 才放行（实测无 Referer 一律 403）
                    "Referer": self.host + "/",
                    "Origin": self.host,
                },
                "format": "application/x-mpegURL",
                "contentType": "application/x-mpegURL",
            }
            return self._attach_subtitle(
                result,
                payload.get("code") or extract_video_code(detail_url),
                wait=None if self._subtitle_blocking else self._subtitle_wait,
            )
        except Exception as exc:
            return {
                "parse": 0,
                "jx": 0,
                "playUrl": "",
                "url": "",
                "header": {},
                "msg": _clean_text(exc) or "播放解析失败",
            }

    def _refine_low_quality(self, video_url, master_url, quality):
        """
        页面只暴露 720p/1080p 两档，选 480p 等低档时页面地址不够用，
        回读主清单取真实低档（主清单里有 360p/480p/720p 全档）。
        """
        target = _normalize_play_quality(quality)
        if target in ("auto", "best") or not master_url:
            return video_url
        height = self._source_height("", video_url)
        if video_url and height and height <= int(target):
            return video_url
        try:
            text, _ = self._fetch_url(master_url, referer=self.host + "/",
                                      timeout=self.playlist_timeout, retries=0)
            picked = self._select_playlist_variant(text, master_url, target)
            return picked or video_url
        except Exception:
            return video_url

    @staticmethod
    def _source_height(name, url):
        """从变量名/路径推断清晰度高度：source1280→1080 档位候选，路径 720p 更可信。"""
        path_match = re.search(r"/(\d{3,4})p/", str(url or ""))
        if path_match:
            return _bounded_int(path_match.group(1), 0, 0, 4320)
        width_match = re.search(r"source(\d{3,4})$", str(name or ""))
        if width_match:
            width = _bounded_int(width_match.group(1), 0, 0, 8192)
            return {1920: 1080, 1280: 720, 842: 480, 854: 480, 640: 360}.get(width, int(width * 9 / 16))
        return 0

    def _pick_source(self, sources, quality):
        if not sources:
            return ""
        selected = _normalize_play_quality(quality)
        master = sources.get("source") or ""
        variants = []
        for name, url in sources.items():
            if name == "source":
                continue
            height = self._source_height(name, url)
            variants.append((height, url))
        # 去重同地址
        unique = {}
        for height, url in variants:
            if url not in unique or height > unique[url]:
                unique[url] = height
        variants = sorted(((h, u) for u, h in unique.items()), key=lambda item: item[0])
        if selected == "auto":
            return master or (variants[-1][1] if variants else "")
        if not variants:
            return master
        if selected == "best":
            return variants[-1][1]
        target = int(selected)
        within = [item for item in variants if item[0] and item[0] <= target]
        if within:
            return within[-1][1]
        return variants[0][1]

    def _resolve_video_url(self, uuid, detail_url, quality=None):
        if not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", str(uuid or ""), re.I):
            return ""
        playlist_url = "https://surrit.com/%s/playlist.m3u8" % uuid
        selected = _normalize_play_quality(quality if quality is not None else self.play_quality)
        if selected == "auto":
            return playlist_url
        try:
            text, _ = self._fetch_url(playlist_url, referer=self.host + "/",
                                      timeout=self.playlist_timeout, retries=0)
            return self._select_playlist_variant(text, playlist_url, selected)
        except Exception:
            return playlist_url

    @staticmethod
    def _playlist_variants(text, playlist_url):
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        variants = []
        pending = None
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF:"):
                attributes = {}
                for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line, re.I):
                    attributes[match.group(1).upper()] = match.group(2).strip().strip('"')
                resolution = re.fullmatch(r"(\d+)x(\d+)", attributes.get("RESOLUTION", ""), re.I)
                try:
                    bandwidth = int(attributes.get("BANDWIDTH") or attributes.get("AVERAGE-BANDWIDTH") or 0)
                except (TypeError, ValueError):
                    bandwidth = 0
                pending = {
                    "width": int(resolution.group(1)) if resolution else 0,
                    "height": int(resolution.group(2)) if resolution else 0,
                    "bandwidth": max(bandwidth, 0),
                }
            elif pending is not None and not line.startswith("#"):
                pending["url"] = urljoin(playlist_url, line)
                variants.append(pending)
                pending = None
        return variants

    @staticmethod
    def _select_playlist_variant(text, playlist_url, quality):
        selected = _normalize_play_quality(quality)
        if selected == "auto":
            return playlist_url
        candidates = Spider._playlist_variants(text, playlist_url)
        if not candidates:
            return playlist_url
        score = lambda item: (item["height"], item["width"], item["bandwidth"])
        if selected == "best":
            return max(candidates, key=score)["url"]
        target = int(selected)
        within = [item for item in candidates if item["height"] and item["height"] <= target]
        if within:
            return max(within, key=score)["url"]
        known = [item for item in candidates if item["height"]]
        if known:
            return min(known, key=score)["url"]
        return min(candidates, key=lambda item: (item["bandwidth"] or 2 ** 62, item["url"]))["url"]

    @staticmethod
    def _pick_best_playlist(text, playlist_url):
        return Spider._select_playlist_variant(text, playlist_url, "best")

    # ---------- 解析辅助 ----------

    @staticmethod
    def _slug_of(url):
        try:
            path = urlsplit(str(url or "")).path.rstrip("/")
        except Exception:
            return ""
        match = re.search(r"/([a-z0-9][a-z0-9._-]*)$", path, re.I)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_uuid(html_text):
        text = str(html_text or "")
        patterns = (
            r"surrit\.com\\?/([a-f0-9-]{36})\\?/",
            r"nineyu\.com\\?/([a-f0-9-]{36})\\?/",
            r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _meta(html_text, key):
        text = str(html_text or "")
        for attribute in ("property", "name"):
            pattern = re.compile(
                r'<meta[^>]+' + attribute + r'=["\']' + re.escape(key) + r'["\'][^>]*content=["\']([^"\']*)["\']',
                re.I,
            )
            match = pattern.search(text)
            if match:
                return _clean_text(match.group(1))
            pattern = re.compile(
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*' + attribute + r'=["\']' + re.escape(key) + r'["\']',
                re.I,
            )
            match = pattern.search(text)
            if match:
                return _clean_text(match.group(1))
        return ""

    @staticmethod
    def _html_title(html_text):
        match = re.search(r"<title[^>]*>(.*?)</title>", str(html_text or ""), re.I | re.S)
        return _clean_text(match.group(1)) if match else ""

    @staticmethod
    def _meta_year(html_text):
        match = re.search(r"(20\d{2}|19\d{2})-\d{2}-\d{2}", str(html_text or ""))
        return match.group(1) if match else ""

    @staticmethod
    def _absolute_image(url, base_url):
        value = urljoin(base_url, str(url or "").strip())
        return re.sub(r"/cover-t\.jpg(?=([?#]|$))", "/cover-n.jpg", value, flags=re.I)

    @staticmethod
    def _encode_payload(payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_play_id(value):
        text = str(value or "").strip()
        if isinstance(value, (list, tuple)) and value:
            text = str(value[0] or "").strip()
        if not text.startswith(PLAY_PREFIX):
            raise ValueError("不支持的播放 ID")
        encoded = text[len(PLAY_PREFIX):]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("播放 ID 数据无效")
        return payload


    # ---------- 目录类页面（女优 / 类型 / 发行商） ----------

    def _dir_page(self, slug, page, options):
        """actresses / genres / makers 目录页解析"""
        params = {"page": page}
        if slug == "actresses":
            for key in ("height", "cup", "age", "debut", "sort"):
                value = str(options.get(key) or "").strip()
                if value:
                    params[key] = value
        # 过滤空值（与脚本2一致）
        params = {k: v for k, v in params.items() if v}
        url = self.host + "/cn/" + slug + "?" + urlencode(params)
        try:
            cached = self._cache_get(url)
            if cached is not None:
                html_text, final_url = cached
            else:
                html_text, final_url = self._fetch_url(url, referer=self.host + "/")
                self._cache_put(url, (html_text, final_url))
            if slug == "actresses":
                items = self._actca(html_text, final_url)
            else:
                items = self._gmsca(html_text, final_url)
            return {
                "page": page,
                "pagecount": 9999,
                "limit": 90,
                "total": 999999,
                "list": items,
            }
        except Exception as exc:
            message = _clean_text(exc) or "目录加载失败"
            return {
                "page": page,
                "pagecount": page,
                "limit": 90,
                "total": 1,
                "list": [{
                    "vod_id": STATUS_PREFIX + message,
                    "vod_name": "访问受限：" + message,
                    "vod_pic": "",
                    "vod_remarks": "可在插件配置里填写代理网关或更换备用域名",
                    "vod_year": "",
                    "vod_area": "",
                    "vod_type": "",
                }],
            }

    def _actca(self, html_text, base_url):
        """女优列表解析（pyquery优先 → lxml → 正则兜底）"""
        text = str(html_text or "")
        items = []
        seen = set()
        # ① pyquery（与脚本2完全一致的选择器）
        try:
            from pyquery import PyQuery as pq
            doc = pq(text)
            for li in doc('.max-w-full ul li').items():
                a_tag = li('a')
                href = a_tag.attr('href') or ''
                if not href:
                    continue
                vid = href.split('/', 3)[-1] if '/' in href else href
                vid = vid.strip('/')
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                pic = li('img').attr('src') or ''
                if pic.startswith('//'):
                    pic = 'https:' + pic
                if pic:
                    pic = pic.replace('\/', '/')
                name = li('img').attr('alt') or ''
                remarks = li('.text-nord10').eq(0).text() or ''
                year = li('.text-nord10').eq(-1).text() or ''
                items.append({
                    "vod_id": vid,
                    "vod_name": _clean_text(name) or vid,
                    "vod_pic": self._dir_pic(pic),
                    "vod_year": year if re.search(r"^\d{4}$", year) else "",
                    "vod_remarks": _clean_text(remarks),
                    "vod_tag": "folder",
                    "style": {"type": "oval"},
                })
            if items:
                return items
        except Exception:
            pass
        # ② lxml xpath（精确选择器）
        if HAS_LXML:
            try:
                document = lxml_html.fromstring(text)
                for li in document.xpath("//div[contains(@class,'max-w-full')]//ul/li"):
                    a_tag = li.xpath(".//a[@href]")
                    if not a_tag:
                        continue
                    href = urljoin(base_url, str(a_tag[0].get("href") or "").strip())
                    vid = href.split("/")[-1] if href else ""
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    img = li.xpath(".//img[@src or @data-src]")
                    pic = ""
                    name = ""
                    if img:
                        pic = str(img[0].get("data-src") or img[0].get("src") or "")
                        name = str(img[0].get("alt") or "")
                    if pic.startswith("//"):
                        pic = "https:" + pic
                    remarks = ""
                    year = ""
                    for span in li.xpath(".//*[contains(@class,'text-nord10')]"):
                        txt = _clean_text(span.text_content() or "")
                        if re.search(r"^\d{4}$", txt):
                            year = txt
                        elif not remarks:
                            remarks = txt
                        break
                    items.append({
                        "vod_id": vid,
                        "vod_name": _clean_text(name) or vid,
                        "vod_pic": self._dir_pic(pic),
                        "vod_year": year,
                        "vod_remarks": remarks,
                        "vod_tag": "folder",
                        "style": {"type": "oval"},
                    })
                if items:
                    return items
            except Exception:
                pass
        # ③ 正则兜底
        for match in re.finditer(
            r'<li[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>.*?<img[^>]*(?:src|data-src)="([^"]*)"[^>]*alt="([^"]*)".*?</li>',
            text, re.I | re.S
        ):
            href, pic, name = match.group(1), match.group(2), match.group(3)
            href = urljoin(base_url, href.strip())
            vid = href.split("/")[-1] if href else ""
            if not vid or vid in seen:
                continue
            seen.add(vid)
            if pic.startswith("//"):
                pic = "https:" + pic
            items.append({
                "vod_id": vid,
                "vod_name": _clean_text(name) or vid,
                "vod_pic": self._dir_pic(pic),
                "vod_year": "",
                "vod_remarks": "",
                "vod_tag": "folder",
                "style": {"type": "oval"},
            })
        return items

    def _gmsca(self, html_text, base_url):
        """genres / makers 目录解析（pyquery优先 → lxml → 正则兜底）"""
        text = str(html_text or "")
        items = []
        seen = set()
        # ① pyquery（与脚本2完全一致的选择器）
        try:
            from pyquery import PyQuery as pq
            doc = pq(text)
            for div in doc('.grid.grid-cols-2 div').items():
                a_tag = div('a').eq(0)
                href = a_tag.attr('href') or ''
                if not href:
                    continue
                vid = href.split('/', 3)[-1] if '/' in href else href
                vid = vid.strip('/')
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                name = div('.text-nord13').text() or a_tag.text() or ''
                remarks = div('.text-nord10').text() or ''
                items.append({
                    "vod_id": vid,
                    "vod_name": _clean_text(name) or vid,
                    "vod_pic": "",
                    "vod_remarks": _clean_text(remarks),
                    "vod_tag": "folder",
                    "style": {"type": "rect", "ratio": 2},
                })
            if items:
                return items
        except Exception:
            pass
        # ② lxml xpath（精确选择器）
        if HAS_LXML:
            try:
                document = lxml_html.fromstring(text)
                for div in document.xpath("//div[contains(@class,'grid') and contains(@class,'grid-cols-2')]//div"):
                    a_tag = div.xpath(".//a[@href]")
                    if not a_tag:
                        continue
                    href = urljoin(base_url, str(a_tag[0].get("href") or "").strip())
                    vid = href.split("/")[-1] if href else ""
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    name = ""
                    for elem in div.xpath(".//*[contains(@class,'text-nord13')]"):
                        name = _clean_text(elem.text_content() or "")
                        break
                    if not name:
                        name = _clean_text(a_tag[0].text_content() or "")
                    remarks = ""
                    for elem in div.xpath(".//*[contains(@class,'text-nord10')]"):
                        remarks = _clean_text(elem.text_content() or "")
                        break
                    items.append({
                        "vod_id": vid,
                        "vod_name": name or vid,
                        "vod_pic": "",
                        "vod_remarks": remarks,
                        "vod_tag": "folder",
                        "style": {"type": "rect", "ratio": 2},
                    })
                if items:
                    return items
            except Exception:
                pass
        # ③ 正则兜底
        for match in re.finditer(
            r'<div[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>.*?<[^>]*class="[^"]*text-nord13[^"]*"[^>]*>(.*?)</[^>]*>.*?<[^>]*class="[^"]*text-nord10[^"]*"[^>]*>(.*?)</[^>]*>.*?</div>',
            text, re.I | re.S
        ):
            href, name, remarks = match.group(1), match.group(2), match.group(3)
            href = urljoin(base_url, href.strip())
            vid = href.split("/")[-1] if href else ""
            if not vid or vid in seen:
                continue
            seen.add(vid)
            items.append({
                "vod_id": vid,
                "vod_name": _clean_text(re.sub(r'<[^>]+>', ' ', name)) or vid,
                "vod_pic": "",
                "vod_remarks": _clean_text(re.sub(r'<[^>]+>', ' ', remarks)),
                "vod_tag": "folder",
                "style": {"type": "rect", "ratio": 2},
            })
        return items

    def _dir_pic(self, pic):
        """目录类图片处理：fourhoi 走本地代理，其他加 Referer。"""
        if not pic:
            return ""
        if pic.startswith("//"):
            pic = "https:" + pic
        if pic:
            pic = pic.replace('\/', '/')
        if "fourhoi.com" in pic:
            m = re.search(r'/([a-z0-9][a-z0-9._-]*)/cover-[tn]\.jpg', pic, re.I)
            if m:
                return self._cover_url(m.group(1), False)
        return pic + "@Referer=" + self.host + "/"

    @staticmethod
    def _empty_page(page):
        return {"page": page, "pagecount": page, "limit": PAGE_SIZE, "total": 0, "list": []}

    @staticmethod
    def _status_detail(message):
        return {
            "vod_id": STATUS_PREFIX + str(message or ""),
            "vod_name": "MissAV 状态",
            "vod_pic": "",
            "vod_remarks": "不可播放",
            "vod_year": "",
            "vod_area": "",
            "vod_type": "",
            "vod_content": _clean_text(message),
            "vod_play_from": "",
            "vod_play_url": "",
        }

    @staticmethod
    def _error_detail(vod_id, code, title, picture, message):
        return {
            "vod_id": vod_id,
            "vod_name": title or code or "MissAV",
            "vod_pic": picture,
            "vod_remarks": code or "解析失败",
            "vod_year": "",
            "vod_area": "",
            "vod_type": "",
            "vod_content": _clean_text(message),
            "vod_play_from": "",
            "vod_play_url": "",
        }


class Filter(SubtitleResolver):
    def __init__(self):
        self._init_subtitle_resolver()
        self.enabled = True
        self.mark_detail = False
        self.overwrite_subs = False
        self._play_codes = {}
        self._play_lock = threading.RLock()

    def init(self, extend="", context=None):
        config = _parse_config(extend)
        self.enabled = _bool(config.get("enabled"), True)
        self.mark_detail = _bool(config.get("mark_detail"), False)
        self.overwrite_subs = _bool(config.get("overwrite_subs"), False)
        self._configure_subtitles(config)
        with self._play_lock:
            self._play_codes = {}

    def detail(self, result, context=None):
        if not self.enabled or not isinstance(result, dict):
            return result
        vods = result.get("list")
        if not isinstance(vods, list):
            return result
        output = dict(result)
        filtered = []
        for vod in vods:
            if not isinstance(vod, dict):
                filtered.append(vod)
                continue
            item = dict(vod)
            code = extract_video_code(
                item.get("vod_name"), item.get("vod_remarks"),
                item.get("vod_content"), item.get("vod_id"),
            )
            if code:
                self._remember_play_codes(item, code)
                if self.mark_detail:
                    remarks = _clean_text(item.get("vod_remarks"))
                    if code not in remarks.upper():
                        item["vod_remarks"] = (remarks + " · 字幕候选 " + code).strip(" ·")
            filtered.append(item)
        output["list"] = filtered
        return output

    def player(self, result, context=None):
        if not self.enabled or not isinstance(result, dict) or not isinstance(context, dict):
            return result
        if str(result.get("parse") if result.get("parse") is not None else 0) not in ("0", "False", "false"):
            return result
        if not result.get("url"):
            return result
        play_id = str(context.get("id") or "").strip()
        with self._play_lock:
            cached_code = self._play_codes.get(play_id, "")
        code = cached_code or extract_video_code(
            context.get("vod_name"), context.get("episode_name"),
            context.get("play_from"), play_id,
        )
        if not code:
            return result
        return self._attach_subtitle(result, code, overwrite=self.overwrite_subs)

    def _remember_play_codes(self, vod, code):
        values = []
        for group in str(vod.get("vod_play_url") or "").split("$$$"):
            for episode in str(group or "").split("#"):
                label, separator, target = episode.partition("$")
                value = target if separator else label
                if value:
                    values.append(str(value).strip())
        for group in vod.get("group") or []:
            if not isinstance(group, dict):
                continue
            for media in group.get("media") or []:
                if isinstance(media, dict) and media.get("url"):
                    values.append(str(media.get("url")).strip())
        with self._play_lock:
            for value in values:
                if value:
                    self._play_codes[value] = code
