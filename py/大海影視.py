# -*- coding: utf-8 -*-
"""
大海影视 (www.vsyy520.net) - TVBox 爬虫源 (maccms / stui 模板)
================================================================
接口：homeContent / categoryContent(含二级分类筛选) / detailContent(懒加载)
      / playerContent(按需解析 m3u8 直链) / searchContent

站点特性（已实测确认）：
1. 模板为 maccms + stui，PC/移动 UA 均可访问（默认伪装移动端更稳）
2. 导航分类：/dahaitp/{1..5}.html  ->  电影/电视剧/综艺/动漫/短剧
3. 分类列表：/dahaisw/{slug}-----------.html（第1页）
   翻页：    /dahaisw/{slug}--------{n}---.html
4. 筛选 URL 槽位（实测确认，顺序固定）：
   /dahaisw/{类型}-{地区}---{剧情}-----{字母}--{年份}.html
   排序：    /dahaisw/{类型}--{by}---------.html   (by = time/hits/score)
5. 详情页：/dahaidt/{id}.html
   播放源为多个 <div class="stui-pannel-box b playlist">，源名在 <h3 class="title">
   集数链接：/dahaipy/{id}-{sid}-{nid}.html
6. 播放页内 var player_xxxx = {...} 的 url 字段即真实 m3u8 直链
7. 搜索：/dahaisc/-------------.html?wd={kw}&page={n}
   另有 maccms 标准 suggest 接口 /index.php/ajax/suggest?mid=1&wd={kw}（最快）

性能优化（重点）：
- 详情页懒加载：不预解析任何集数 m3u8，秒开
- playerContent 按需解析 + 15 分钟缓存 + 后台预取第 1 集
- 多级缓存：首页10分钟 / 分类5分钟 / 详情5分钟(失败30秒) / 搜索3分钟 / 播放15分钟
- 连接池复用(HTTPAdapter) + 短超时(8s/5s/4s) + 快速重试(0.2s) + 429 限流等待
- 搜索三级策略：suggest 接口 -> 站内搜索页 -> 分类并行爬取兜底
- 图片 HTTPS 归一化 + data-original/data-src/src 三级回退
"""

import re
import json
import time
import threading
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
except ImportError:
    ThreadPoolExecutor = None
    as_completed = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

try:
    import sys
    sys.path.append('..')
    from base.spider import Spider as _BaseSpider
except ImportError:
    _BaseSpider = None


# ============================================================
# 常量
# ============================================================
HOST = "https://www.vsyy520.net"

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

# 超时（秒）
TIMEOUT_PAGE = 8
TIMEOUT_API = 5
TIMEOUT_PLAY = 4

# 缓存 TTL（秒）
TTL_HOME = 600
TTL_CAT = 300
TTL_DETAIL_OK = 300
TTL_DETAIL_EMPTY = 30
TTL_SEARCH = 180
TTL_PLAY = 900

# 地区
AREAS = [
    "大陆", "香港", "台湾", "美国", "法国", "英国", "日本", "韩国",
    "德国", "泰国", "印度", "意大利", "西班牙", "加拿大", "其他",
]

# 剧情（站点「按剧情」筛选项）
GENRES = [
    "喜剧", "爱情", "恐怖", "动作", "科幻", "剧情", "战争", "警匪",
    "犯罪", "动画", "奇幻", "武侠", "冒险", "枪战", "悬疑", "惊悚",
    "经典", "青春", "文艺", "微电影", "历史", "军事", "同性", "家庭",
    "儿童", "灾难", "西部", "纪录", "短片", "歌舞", "运动", "网络电影",
]

# 语言
LANGS = ["国语", "英语", "粤语", "闽南语", "韩语", "日语", "法语", "德语", "其它"]

# 字母
LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ["0-9"]

# 年份
YEARS = [str(y) for y in range(2026, 2005, -1)]

# 排序
SORTS = [
    {"n": "最新", "v": "time"},
    {"n": "最热", "v": "hits"},
    {"n": "评分", "v": "score"},
]

# 分类表（一级分类 -> 二级分类 slug）
CATS = [
    {"id": "1", "name": "电影", "subs": [
        ("6", "动作片"), ("7", "喜剧片"), ("8", "爱情片"), ("9", "科幻片"),
        ("10", "恐怖片"), ("11", "剧情片"), ("12", "战争片"), ("13", "纪录片"),
        ("14", "悬疑片"), ("15", "犯罪片"), ("16", "奇幻片"), ("31", "动画片"),
    ]},
    {"id": "2", "name": "电视剧", "subs": [
        ("17", "国产剧"), ("18", "港剧"), ("19", "台湾剧"), ("20", "韩剧"),
        ("21", "日剧"), ("22", "美剧"), ("23", "泰剧"), ("24", "海外剧"),
    ]},
    {"id": "3", "name": "综艺", "subs": [
        ("25", "大陆综艺"), ("26", "日韩综艺"), ("27", "港台综艺"), ("28", "欧美综艺"),
    ]},
    {"id": "4", "name": "动漫", "subs": [
        ("29", "国产动漫"), ("30", "日本动漫"), ("32", "欧美动漫"), ("33", "海外动漫"),
    ]},
    {"id": "5", "name": "短剧", "subs": [
        ("34", "女频恋爱"), ("35", "反转爽剧"), ("36", "脑洞悬疑"),
        ("37", "古装仙侠"), ("38", "年代穿越"), ("39", "现代都市"),
    ]},
]


def _build_filters(cat):
    """构建筛选器：类型(二级分类) / 地区 / 剧情 / 年份 / 语言 / 字母 / 排序"""
    subs = [{"n": name, "v": slug} for slug, name in cat["subs"]]
    filters = [{
        "key": "class", "name": "类型",
        "value": [{"n": "全部", "v": ""}] + subs,
    }]
    filters.append({
        "key": "area", "name": "地区",
        "value": [{"n": "全部", "v": ""}] + [{"n": a, "v": a} for a in AREAS],
    })
    filters.append({
        "key": "genre", "name": "剧情",
        "value": [{"n": "全部", "v": ""}] + [{"n": g, "v": g} for g in GENRES],
    })
    filters.append({
        "key": "year", "name": "年份",
        "value": [{"n": "全部", "v": ""}] + [{"n": y, "v": y} for y in YEARS],
    })
    filters.append({
        "key": "lang", "name": "语言",
        "value": [{"n": "全部", "v": ""}] + [{"n": l, "v": l} for l in LANGS],
    })
    filters.append({
        "key": "letter", "name": "字母",
        "value": [{"n": "全部", "v": ""}] + [{"n": c, "v": c} for c in LETTERS],
    })
    filters.append({
        "key": "by", "name": "排序",
        "value": SORTS,
    })
    return filters


# 全部分类 + 筛选器
ALL_CLASSES = [{"type_id": c["id"], "type_name": c["name"], "filter": 1} for c in CATS]
ALL_FILTERS = {c["id"]: _build_filters(c) for c in CATS}

# 一级分类 id -> 默认二级 slug（tid 直接传一级 id 时兜底）
DEFAULT_SLUG = {c["id"]: c["subs"][0][0] for c in CATS}


# ============================================================
# Spider 主类
# ============================================================
_Base = _BaseSpider if _BaseSpider is not None else object


class Spider(_Base):
    siteUrl = HOST
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': HOST + '/',
    }

    # ===== 初始化 =====
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.headers['Connection'] = 'keep-alive'
        self.session.verify = False
        adapter = HTTPAdapter(
            pool_connections=20, pool_maxsize=40,
            max_retries=0, pool_block=False,
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        # 缓存容器 + 锁
        self._lock = threading.Lock()
        self._home_cache = []
        self._home_cache_time = 0
        self._cat_cache = {}
        self._detail_cache = {}
        self._search_cache = {}
        self._play_cache = {}
        self._prefetching = set()

    def init(self, extend=""):
        self.extend = extend or ""

    # ===== 网络工具 =====
    def _get(self, url, referer='', timeout=TIMEOUT_PAGE):
        headers = {'Connection': 'keep-alive'}
        if referer:
            headers['Referer'] = referer
        for attempt in range(2):
            try:
                r = self.session.get(url, timeout=timeout, headers=headers)
                if r.status_code == 429:
                    time.sleep(2.0)
                    continue
                r.raise_for_status()
                r.encoding = r.apparent_encoding or 'utf-8'
                return r
            except Exception:
                if attempt == 0:
                    time.sleep(0.2)
                else:
                    return None
        return None

    def _get_text(self, url, referer='', timeout=TIMEOUT_PAGE):
        r = self._get(url, referer, timeout)
        return r.text if r is not None else ""

    # ===== 缓存 =====
    @staticmethod
    def _cache_get(cache, key, ttl=None):
        item = cache.get(key)
        if item and time.time() - item[0] < (ttl if ttl is not None else item[2]):
            return item[1]
        return None

    @staticmethod
    def _cache_set(cache, key, value, ttl=TTL_CAT):
        if len(cache) > 512:
            cache.clear()
        cache[key] = (time.time(), value, ttl)

    # ===== HTML 解析 =====
    @staticmethod
    def _soup(html):
        if not html or BeautifulSoup is None:
            return None
        try:
            return BeautifulSoup(html, 'lxml')
        except Exception:
            try:
                return BeautifulSoup(html, 'html.parser')
            except Exception:
                return None

    @staticmethod
    def _abs(u):
        u = (u or '').strip()
        if not u:
            return ''
        if u.startswith('//'):
            return 'https:' + u
        if u.startswith('/'):
            return HOST + u
        if not u.startswith('http'):
            return HOST + '/' + u
        return u

    @staticmethod
    def _extract_id(href):
        m = re.search(r'/(\d{3,10})\.(?:html|shtml)$', (href or '').strip())
        return m.group(1) if m else None

    @staticmethod
    def _clean_name(raw):
        if not raw:
            return raw
        return re.sub(r'\s*[（(]\s*\d{4}\s*[）)]\s*$', '', raw.strip())

    @staticmethod
    def _pick_pic(el):
        if el is None:
            return ''
        for attr in ('data-original', 'data-src', 'src'):
            val = str(el.get(attr) or '').strip()
            if val and 'load.gif' not in val:
                return Spider._abs(val)
        style = str(el.get('style') or '')
        m = re.search(r'background-image:\s*url\(([^)]+)\)', style)
        if m:
            return Spider._abs(m.group(1).strip().strip('"').strip("'"))
        return ''

    @staticmethod
    def _pick_remarks(title, box=None):
        """备注：优先 .pic-text，其次标题正则兜底"""
        if box is not None:
            st = box.select_one('.pic-text') or box.select_one('.pic-tag')
            if st:
                txt = st.get_text(strip=True)
                if txt:
                    return txt
        m = re.search(
            r'(更新至[^\s]{0,12}|更新到[^\s]{0,12}|连载至[^\s]{0,12}|全\d+集|全集'
            r'|已完结|正片|HD中字|HD国语|TC中字|抢先版|预告片)', title or ''
        )
        return m.group(1) if m else ''

    def _build_card(self, a, box=None):
        """从 <a> 节点构建一条 vod 卡片，失败返回 None"""
        href = str(a.get('href') or '')
        vid = self._extract_id(href)
        if not vid:
            return None
        title = str(a.get('title') or '').strip() or a.get_text(strip=True)
        if not title:
            return None
        if box is None:
            box = a.find_parent('li')
        return {
            'vod_id': vid,
            'vod_name': self._clean_name(title),
            'vod_pic': self._pick_pic(a),
            'vod_remarks': self._pick_remarks(title, box),
        }

    def _parse_cards(self, html, limit=36):
        """解析列表页卡片（stui-vodlist__thumb）"""
        if not html:
            return []
        soup = self._soup(html)
        if soup is None:
            return []
        items = {}
        for a in soup.select('a.stui-vodlist__thumb'):
            card = self._build_card(a)
            if card and card['vod_id'] not in items:
                items[card['vod_id']] = card
        return list(items.values())[:limit]

    # ============================================================
    # 首页
    # ============================================================
    def _fetch_home(self):
        html = self._get_text(HOST)
        return self._parse_cards(html, limit=60)

    def homeContent(self, filter=False):
        vod_list = []
        now = int(time.time())
        with self._lock:
            if self._home_cache and now - self._home_cache_time < TTL_HOME:
                vod_list = self._home_cache[:60]
        if not vod_list:
            try:
                vod_list = self._fetch_home()
                if vod_list:
                    with self._lock:
                        self._home_cache = vod_list
                        self._home_cache_time = int(time.time())
            except Exception:
                pass
        return {
            "class": ALL_CLASSES,
            "filters": ALL_FILTERS,
            "list": vod_list,
        }

    def homeVideoContent(self):
        now = int(time.time())
        with self._lock:
            if self._home_cache and now - self._home_cache_time < TTL_HOME:
                return {"list": self._home_cache[:60]}
        try:
            vod_list = self._fetch_home()
            if vod_list:
                with self._lock:
                    self._home_cache = vod_list
                    self._home_cache_time = int(time.time())
            return {"list": vod_list[:60]}
        except Exception:
            return {"list": []}

    # ============================================================
    # 分类列表（含二级分类筛选）
    # ============================================================
    def _empty_category(self, page=1):
        return {"list": [], "page": page, "pagecount": 1, "limit": 36, "total": 0}

    @staticmethod
    def _cat_url(slug, page=1, ext=None):
        """
        拼接分类 URL（实测槽位顺序固定）：
        /dahaisw/{类型}-{地区}---{剧情}-----{字母}--{年份}.html
        翻页：/dahaisw/{类型}--------{n}---.html
        """
        ext = ext or {}
        area = (ext.get('area') or '').strip()
        genre = (ext.get('genre') or '').strip()
        letter = (ext.get('letter') or '').strip()
        year = (ext.get('year') or '').strip()
        by = (ext.get('by') or '').strip()

        # 排序 URL 与筛选 URL 互斥（站点如此设计）
        if by and by != 'time':
            return f"{HOST}/dahaisw/{slug}--{by}---------.html"

        # 无任何筛选 -> 走翻页槽位
        if not (area or genre or letter or year):
            page_str = str(page) if page > 1 else ''
            return f"{HOST}/dahaisw/{slug}--------{page_str}---.html"

        # 有筛选 -> 走筛选槽位
        a = quote(area) if area else ''
        g = quote(genre) if genre else ''
        l = letter if letter else ''
        y = year if year else ''
        return f"{HOST}/dahaisw/{slug}-{a}---{g}-----{l}--{y}.html"

    def categoryContent(self, tid, pg, filter, extend):
        page = 1
        try:
            page = max(1, int(pg or 1))
            ext = {}
            if extend:
                if isinstance(extend, dict):
                    ext = extend
                elif isinstance(extend, str):
                    try:
                        ext = json.loads(extend)
                    except Exception:
                        ext = {}

            tid = str(tid or '').strip()
            slug = (ext.get('class') or '').strip() or DEFAULT_SLUG.get(tid, tid)

            ckey = "%s|%d|%s" % (
                slug, page, json.dumps(ext, ensure_ascii=False, sort_keys=True)
            )
            cached = self._cache_get(self._cat_cache, ckey, TTL_CAT)
            if cached is not None:
                return cached

            url = self._cat_url(slug, page, ext)
            html = self._get_text(url)
            if not html:
                return self._empty_category(page)

            # 总页数：优先「尾页」链接，其次分页数字
            pagecount = 1
            m = re.search(r'/(\d+)---\.html"[^>]*>\s*尾页', html)
            if m:
                pagecount = int(m.group(1))
            else:
                m = re.search(r'<span class="num">\d+/(\d+)</span>', html)
                if m:
                    pagecount = int(m.group(1))
                else:
                    nums = [int(x) for x in re.findall(r'--------(\d+)---\.html', html)]
                    if nums:
                        pagecount = max(nums)

            vod_list = self._parse_cards(html, limit=36)

            result = {
                "list": vod_list,
                "page": page,
                "pagecount": pagecount,
                "limit": 36,
                "total": pagecount * 36,
            }
            self._cache_set(self._cat_cache, ckey, result, TTL_CAT)
            return result
        except Exception:
            return self._empty_category(page)

    # ============================================================
    # 详情页（懒加载：不预解析任何 m3u8）
    # ============================================================
    def detailContent(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        vid = str(ids[0]).split(',')[0].strip()
        if not vid:
            return {"list": []}

        cached = self._cache_get(self._detail_cache, vid, None)
        if cached is not None:
            return cached

        result = self._fetch_detail(vid)
        ttl = TTL_DETAIL_OK if result.get("list") else TTL_DETAIL_EMPTY
        self._cache_set(self._detail_cache, vid, result, ttl)

        # 后台预取第 1 集直链，用户点播放时秒开
        if result.get("list"):
            self._prefetch_play(result["list"][0])
        return result

    @staticmethod
    def _split_meta_segments(p):
        """
        站点把「类型 / 地区 / 年份」塞在同一个 <p class="data"> 里，
        用 <span class="split-line"> 分隔，必须按 span 切分，否则会串味。
        """
        segs, cur = [], []
        for node in p.children:
            cls = getattr(node, 'get', lambda *a: None)('class') or []
            if 'split-line' in cls:
                segs.append(''.join(cur))
                cur = []
            else:
                cur.append(node.get_text() if hasattr(node, 'get_text') else str(node))
        segs.append(''.join(cur))
        return segs

    def _fetch_detail(self, vid):
        html = self._get_text(f"{HOST}/dahaidt/{vid}.html")
        if not html:
            return {"list": []}
        soup = self._soup(html)
        if soup is None:
            return {"list": []}

        # --- 名称 ---
        name = ''
        h1 = soup.select_one('.stui-content__detail h1') or soup.select_one('h1')
        if h1:
            name = self._clean_name(h1.get_text(strip=True))
        if not name and soup.title:
            name = self._clean_name(soup.title.get_text(strip=True))

        # --- 封面 ---
        pic = ''
        img = (soup.select_one('.stui-content__thumb img')
               or soup.select_one('.stui-vodlist__thumb img'))
        if img:
            pic = self._pick_pic(img)

        # --- 元信息 ---
        director = actor = area = type_name = year = ''
        for p in soup.select('.stui-content__detail p.data'):
            label_el = p.find('span', class_='text-muted')
            if label_el is None:
                continue
            label = (label_el.get_text(strip=True) or '').rstrip(':：')
            full = p.get_text(' ', strip=True)
            val = full[len(label_el.get_text(strip=True)):].strip().lstrip(':： ').strip()
            if not val:
                continue
            if label == '主演' and not actor:
                actor = val
            elif label == '导演' and not director:
                director = val

        # 类型 / 地区 / 年份：按 split-line 切分同一段落
        for p in soup.select('.stui-content__detail p.data'):
            if not p.find('span', class_='split-line'):
                continue
            for seg in self._split_meta_segments(p):
                seg = re.sub(r'\s+', ' ', seg).strip()
                if not seg:
                    continue
                m = re.match(r'^(类型|地区|年份|语言|片长|别名|又名)[:：]\s*(.*)$', seg)
                if not m:
                    continue
                k, v = m.group(1), m.group(2).strip()
                if k == '类型' and not type_name:
                    type_name = v
                elif k == '地区' and not area:
                    area = v
                elif k == '年份' and not year:
                    year = v
            break

        if not year:
            year = self._pick_year(soup.get_text(' ', strip=True))

        # --- 简介 ---
        content = ''
        desc = soup.select_one('.detail-content') or soup.select_one('.detail-sketch')
        if desc:
            content = desc.get_text(strip=True)
        if not content:
            meta = soup.find('meta', attrs={'name': 'description'})
            if meta:
                content = str(meta.get('content') or '')[:300]

        # --- 备注 ---
        remarks = ''
        st = soup.select_one('.stui-content__detail .pic-text')
        if st:
            remarks = st.get_text(strip=True)
        if not remarks:
            remarks = self._pick_remarks(name)

        # --- 播放源与集数（每个 playlist 面板 = 一条线路）---
        play_groups = []
        for box in soup.select('div.stui-pannel-box.b.playlist'):
            h3 = box.select_one('.stui-pannel__head h3.title')
            src_name = h3.get_text(strip=True) if h3 else '线路'
            eps = []
            for a in box.select('ul.stui-content__playlist a'):
                href = str(a.get('href') or '')
                # 过滤外链广告（//app2.zstv47.com 之类）
                if not href.startswith('/') or 'dahaipy' not in href:
                    continue
                ep_name = a.get_text(strip=True) or '播放'
                ep_url = self._abs(href)
                if ep_url:
                    eps.append((ep_name, ep_url))
            if eps:
                play_groups.append((src_name, eps))

        # 兜底：直接抓全站 dahaipy 链接
        if not play_groups:
            eps = []
            for a in soup.select('a[href*="dahaipy"]'):
                href = str(a.get('href') or '')
                if not href.startswith('/'):
                    continue
                ep_name = a.get_text(strip=True) or '播放'
                ep_url = self._abs(href)
                if ep_url:
                    eps.append((ep_name, ep_url))
            if eps:
                play_groups.append(('默认线路', eps))

        play_from, play_url = '', ''
        for src_name, eps in play_groups:
            ep_parts = [f"{n}${u}" for n, u in eps]
            play_from = (play_from + '$$$' + src_name) if play_from else src_name
            play_url = (play_url + '$$$' + '#'.join(ep_parts)) if play_url else '#'.join(ep_parts)

        detail = {
            "vod_id": vid,
            "vod_name": name or f"视频{vid}",
            "vod_pic": pic or HOST,
            "type_name": type_name,
            "vod_remarks": remarks or '',
            "vod_year": year,
            "vod_area": area,
            "vod_lang": '',
            "vod_director": director,
            "vod_actor": actor,
            "vod_content": content,
            "vod_play_from": play_from or '默认',
            "vod_play_url": play_url or '',
        }
        return {"list": [detail]}

    # ============================================================
    # 播放解析（按需 + 缓存 + 预取）
    # ============================================================
    def _resolve_play(self, play_url):
        """解析播放页 -> 真实 m3u8 直链"""
        cached = self._cache_get(self._play_cache, play_url, TTL_PLAY)
        if cached:
            return cached
        real = ''
        try:
            text = self._get_text(play_url, referer=HOST + '/', timeout=TIMEOUT_PLAY)
            if text:
                # 主路径：var player_xxxx = {...} 的 url 字段
                m = re.search(r'var\s+player_\w+\s*=\s*(\{.*?\})\s*[;<]', text, re.S)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        u = (data.get('url') or '').replace('\\/', '/').strip()
                        if u:
                            real = self._abs(u)
                    except Exception:
                        pass
                # 兜底1：直接找 m3u8
                if not real:
                    m2 = re.search(r'"url"\s*:\s*"([^"]+\.m3u8[^"]*)"', text)
                    if m2:
                        real = self._abs(m2.group(1).replace('\\/', '/'))
                # 兜底2：任意 http 视频地址
                if not real:
                    m3 = re.search(r'"url"\s*:\s*"(https?://[^"]+)"', text)
                    if m3:
                        u = m3.group(1).replace('\\/', '/').strip()
                        if '.m3u8' in u or '.mp4' in u:
                            real = self._abs(u)
        except Exception:
            real = ''
        if real:
            self._cache_set(self._play_cache, play_url, real, TTL_PLAY)
        return real

    @staticmethod
    def _first_play_url(vod):
        for seg in (vod.get("vod_play_url") or "").split("$$$"):
            for item in seg.split("#"):
                parts = item.split("$", 1)
                if len(parts) == 2 and parts[1]:
                    return parts[1]
        return None

    def _spawn_prefetch(self, target):
        """后台线程预取一条播放直链（带去重）"""
        if not target:
            return
        with self._lock:
            if self._cache_get(self._play_cache, target, TTL_PLAY):
                return
            if target in self._prefetching:
                return
            self._prefetching.add(target)

        def _job():
            try:
                self._resolve_play(target)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._prefetching.discard(target)

        threading.Thread(target=_job, daemon=True).start()

    def _prefetch_play(self, vod):
        self._spawn_prefetch(self._first_play_url(vod))

    def _prefetch_next(self, play_url, vod_id):
        """预取下一集，实现连播秒开"""
        cached = self._cache_get(self._detail_cache, vod_id, TTL_DETAIL_OK)
        if not cached or not cached.get("list"):
            return
        vod = cached["list"][0]
        for seg in (vod.get("vod_play_url") or "").split("$$$"):
            items = seg.split("#")
            for i, item in enumerate(items):
                parts = item.split("$", 1)
                if len(parts) == 2 and self._abs(parts[1]) == play_url:
                    if i + 1 < len(items):
                        nxt = items[i + 1].split("$", 1)
                        if len(nxt) == 2 and nxt[1]:
                            self._spawn_prefetch(self._abs(nxt[1]))
                    return

    @staticmethod
    def _play_payload(playurl):
        is_m3u8 = '.m3u8' in playurl.lower()
        return {
            "parse": 0,
            "playUrl": "",
            "url": playurl,
            "header": {
                "User-Agent": UA,
                "Referer": HOST + "/",
                "Origin": HOST,
            },
            "format": "application/x-mpegURL" if is_m3u8 else "",
            "contentType": "application/x-mpegURL" if is_m3u8 else "",
        }

    def playerContent(self, flag, id, vipFlags):
        if not id:
            return {"parse": 0, "playUrl": "", "url": ""}
        play_url = self._abs(str(id))

        cached = self._cache_get(self._play_cache, play_url, TTL_PLAY)
        if cached:
            self._prefetch_next(play_url, play_url)
            return self._play_payload(cached)

        m3u8 = self._resolve_play(play_url)
        if m3u8:
            self._prefetch_next(play_url, play_url)
            return self._play_payload(m3u8)

        # 兜底：并行尝试同片其他线路
        alts = self._alt_play_urls(play_url)
        if alts and ThreadPoolExecutor is not None:
            try:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    futs = [ex.submit(self._resolve_play, u) for u in alts]
                    for f in as_completed(futs, timeout=TIMEOUT_PLAY):
                        u = f.result(timeout=TIMEOUT_PLAY)
                        if u:
                            self._cache_set(self._play_cache, play_url, u, TTL_PLAY)
                            return self._play_payload(u)
            except Exception:
                pass
        else:
            for u in alts:
                m = self._resolve_play(u)
                if m:
                    self._cache_set(self._play_cache, play_url, m, TTL_PLAY)
                    return self._play_payload(m)

        # 全部失败 -> 交给 TVBox 嗅探
        return {
            "parse": 1,
            "playUrl": "",
            "url": play_url,
            "header": {"User-Agent": UA, "Referer": HOST + "/"},
        }

    def _alt_play_urls(self, play_url, limit=6):
        """取同片其他线路的播放页地址"""
        m = re.search(r'/dahaipy/(\d+)-', play_url)
        if not m:
            return []
        vid = m.group(1)
        cached = self._cache_get(self._detail_cache, vid, TTL_DETAIL_OK)
        if not cached or not cached.get("list"):
            return []
        vod = cached["list"][0]
        out, seen = [], {play_url}
        for seg in (vod.get("vod_play_url") or "").split("$$$"):
            for item in seg.split("#"):
                parts = item.split("$", 1)
                if len(parts) == 2 and parts[1]:
                    u = parts[1]
                    if u not in seen:
                        seen.add(u)
                        out.append(u)
                        if len(out) >= limit:
                            return out
        return out

    # ============================================================
    # 工具：元信息提取
    # ============================================================
    @staticmethod
    def _pick_year(text):
        m = re.search(r'[（(]\s*(\d{4})\s*[）)]', text or '')
        if m:
            return m.group(1)
        m = re.search(r'\b(19\d{2}|20\d{2})\b', text or '')
        return m.group(1) if m else ''

    # ============================================================
    # 搜索（三级策略：suggest 接口 -> 站内搜索页 -> 分类并行爬取）
    # ============================================================
    def _parse_search_cards(self, html, limit=36):
        """解析搜索结果页（stui-vodlist__media 结构）"""
        if not html:
            return []
        soup = self._soup(html)
        if soup is None:
            return []
        items = {}
        selectors = [
            'a.stui-vodlist__thumb',
            '.stui-vodlist__media a[href*="/dahaidt/"]',
            'a[href*="/dahaidt/"]',
        ]
        for selector in selectors:
            for a in soup.select(selector):
                card = self._build_card(a)
                if card and card['vod_id'] not in items:
                    items[card['vod_id']] = card
            if items:
                break
        return list(items.values())[:limit]

    @staticmethod
    def _filter_by_keyword(cards, raw, limit=24):
        """分词模糊匹配 + 评分排序（比纯包含匹配命中率高）"""
        if not raw:
            return cards[:limit]
        raw = raw.lower().replace(' ', '').strip()
        if not raw:
            return cards[:limit]

        if len(raw) <= 2:
            tokens = [raw]
        else:
            tokens = [raw[i:i + 2] for i in range(0, len(raw) - 1)]
        tokens += list(raw)

        def score(name):
            name = (name or '').lower().replace(' ', '')
            if not name:
                return 0
            if raw in name:
                return 100
            return sum(1 for t in tokens if t in name)

        matched = [(score(c.get('vod_name') or ''), c) for c in cards]
        matched = [c for s, c in matched if s > 0]
        matched.sort(key=lambda c: score(c.get('vod_name') or ''), reverse=True)
        return matched[:limit]

    def _search_suggest(self, kw, page):
        """策略0：maccms 标准 suggest 接口（最快，实测可用）"""
        urls = [
            f"{HOST}/index.php/ajax/suggest?mid=1&wd={kw}&page={page}",
            f"{HOST}/ajax/suggest?mid=1&wd={kw}&page={page}",
        ]
        for u in urls:
            text = self._get_text(u, referer=HOST + '/', timeout=TIMEOUT_API)
            if not text:
                continue
            text = text.strip()
            if not text.startswith('{'):
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            out = []
            for it in (data.get('list') or []):
                if not isinstance(it, dict):
                    continue
                vid = str(it.get('id') or '')
                name = str(it.get('name') or '').strip()
                if vid and name:
                    out.append({
                        'vod_id': vid,
                        'vod_name': name,
                        'vod_pic': self._abs((it.get('pic') or '').replace('\\/', '/')),
                        'vod_remarks': '',
                    })
            if out:
                return {"list": out}
        return None

    def _search_site(self, kw, raw, page):
        """策略1：站内搜索页（实测 /dahaisc/-------------.html?wd=）"""
        urls = [
            f"{HOST}/dahaisc/-------------.html?wd={kw}&page={page}",
            f"{HOST}/index.php/vod/search.html?wd={kw}&page={page}",
        ]
        for url in urls:
            try:
                html = self._get_text(url, referer=HOST + '/', timeout=TIMEOUT_API)
                if not html:
                    continue
                if '搜索功能关闭' in html or '搜索功能暂停' in html:
                    continue
                cards = self._parse_search_cards(html, limit=36)
                if not cards:
                    continue
                matched = self._filter_by_keyword(cards, raw)
                if matched:
                    return {"list": matched}
            except Exception:
                continue
        return None

    def _search_by_scrape(self, raw, page):
        """策略2：分类并行爬取兜底（分词评分过滤）"""
        if page > 1:
            return {"list": []}

        pages = (1, 2, 3) if len(raw) >= 3 else (1, 2)

        def _fetch_cat(args):
            cat_id, p = args
            page_str = str(p) if p > 1 else ''
            url = f"{HOST}/dahaisw/{cat_id}--------{page_str}---.html"
            html = self._get_text(url, timeout=TIMEOUT_API)
            return self._parse_cards(html, limit=36)

        all_cards = []
        if ThreadPoolExecutor is not None:
            tasks = [(c['id'], p) for c in CATS for p in pages]
            try:
                with ThreadPoolExecutor(max_workers=5) as ex:
                    futs = [ex.submit(_fetch_cat, t) for t in tasks]
                    for f in as_completed(futs, timeout=TIMEOUT_API * 5):
                        try:
                            all_cards.extend(f.result(timeout=TIMEOUT_API))
                        except Exception:
                            continue
            except Exception:
                pass
        else:
            for c in CATS:
                for p in pages:
                    try:
                        page_str = str(p) if p > 1 else ''
                        url = f"{HOST}/dahaisw/{c['id']}--------{page_str}---.html"
                        html = self._get_text(url, timeout=TIMEOUT_API)
                        all_cards.extend(self._parse_cards(html, limit=36))
                    except Exception:
                        continue

        matched = self._filter_by_keyword(all_cards, raw, limit=24)
        return {"list": matched} if matched else None

    def searchContent(self, keyword, quick=False, pg=1):
        """三级搜索：suggest 接口 -> 站内搜索页 -> 分类爬取兜底"""
        raw = (keyword or '').strip().lower()
        kw = quote((keyword or '').strip())
        if not kw:
            return {"list": [], "msg": "请输入搜索关键词"}

        page = int(pg or 1)
        ckey = "%s|%s" % (page, raw)
        cached = self._cache_get(self._search_cache, ckey, TTL_SEARCH)
        if cached is not None:
            return cached

        # 策略0：suggest 接口（最快）
        result = self._search_suggest(kw, page)

        # 策略1：站内搜索页
        if not result:
            result = self._search_site(kw, raw, page)

        # 策略2：分类爬取兜底
        if not result:
            result = self._search_by_scrape(raw, page)

        if result and result.get("list"):
            self._cache_set(self._search_cache, ckey, result, TTL_SEARCH)
            return result

        result = {"list": [], "msg": "未找到相关内容，请尝试其他关键词或通过分类浏览"}
        self._cache_set(self._search_cache, ckey, result, TTL_SEARCH)
        return result

    # ============================================================
    # 本地代理 & 清理
    # ============================================================
    def localProxy(self, param):
        return [200, "video/MP2T", b"", ""]

    def destroy(self):
        try:
            self.session.close()
        except Exception:
            pass

    def close(self):
        self.destroy()


# ============================================================
# 本地测试
# ============================================================
if __name__ == '__main__':
    s = Spider()
    action = sys.argv[1] if len(sys.argv) > 1 else 'home'
    if action == 'home':
        print(json.dumps(s.homeContent(), ensure_ascii=False)[:800])
    elif action == 'category':
        tid = sys.argv[2] if len(sys.argv) > 2 else '1'
        pg = sys.argv[3] if len(sys.argv) > 3 else '1'
        cl = sys.argv[4] if len(sys.argv) > 4 else ''
        print(json.dumps(
            s.categoryContent(tid, pg, False, {'class': cl} if cl else {}),
            ensure_ascii=False
        )[:800])
    elif action == 'detail':
        vid = sys.argv[2] if len(sys.argv) > 2 else '261275'
        r = s.detailContent(vid)
        d = r['list'][0] if r.get('list') else {}
        print(json.dumps(d, ensure_ascii=False)[:1200])
    elif action == 'play':
        pid = sys.argv[2] if len(sys.argv) > 2 else ''
        print(json.dumps(s.playerContent('', pid, []), ensure_ascii=False)[:500])
    elif action == 'search':
        kw = sys.argv[2] if len(sys.argv) > 2 else '古寨'
        print(json.dumps(s.searchContent(kw), ensure_ascii=False)[:800])