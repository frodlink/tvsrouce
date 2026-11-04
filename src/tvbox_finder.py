# -*- coding: utf-8 -*-
"""
TVBox 地址体检工具
每次运行：体检内置的一批配置源，给出「当前最稳的 1 个配置地址」+「1 个直播地址」。
纯标准库，无需安装任何依赖。
"""
import sys, os, re, json, ssl, time, queue, threading, hashlib, urllib.request, urllib.parse, socket
from urllib.parse import urlsplit, urlunsplit, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "1.6"
LIVE_MAX_MS = 300   # 直播频道测速上限：超过 300ms 不采用
ALLOW_PUBLISH = True          # 分享版会把它关掉，避免别人的工具往我的服务器发配置
IS_SHARE = False              # 分享版会置 True，用于隐藏/禁用站长专用功能
UA = "Mozilla/5.0 (Linux; Android 9; TV Box) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(sys.executable))   # 打包成 exe 后，配置文件放 exe 旁边
else:
    HERE = os.path.dirname(os.path.abspath(__file__))

GH_RAW = "https://raw.githubusercontent.com/"
GH_PROXIES = ["https://gh-proxy.com/", "https://gh-proxy.org/", "https://ghproxy.net/"]
PROXY = None          # 运行时探到的可用加速前缀
PROXY_LOCK = threading.Lock()


def raw(path, repo="Lightconer/tvbox-ysc-config", branch="main"):
    return GH_RAW + repo + "/" + branch + "/" + quote(path)


SOURCES = [
    {"name": "小马",          "url": "https://szyyds.cn/tv/x.json"},
    {"name": "老刘备",        "url": "https://raw.liucn.cc/box/m.json"},
    {"name": "肥猫",          "url": "http://xn--z7x900a.net/tv"},
    {"name": "4K小盒子",      "url": "http://xhztv.top/4k.json"},
    {"name": "小盒子多仓",    "url": "http://xhztv.top/dc"},
    {"name": "拾光多仓",      "url": "http://xmbjm.fh4u.org/dc.txt"},
    {"name": "挺好多仓",      "url": "http://ztha.top/TVBox/GYCK.json"},
    {"name": "聚合(仓库)",    "url": raw("output/单仓聚合.json")},
    {"name": "小马(仓库)",    "url": raw("output/xiaoma.json")},
    {"name": "小盒子(仓库)",  "url": raw("output/xiaohezi.json")},
    {"name": "王二小(仓库)",  "url": raw("output/wangerxiao.json")},
    {"name": "老刘备(仓库)",  "url": raw("output/laoliubei.json")},
    {"name": "多仓(noimank)", "url": raw("tvboxmuti.json", "noimank/tvbox", "main")},
    {"name": "2hacc",         "url": raw("h/h.json", "2hacc/TVBox", "main")},
]

# ---------------------------------------------------------------- 网络层

def enc(u):
    p = urlsplit(u)
    netloc = p.netloc
    if any(ord(c) > 127 for c in netloc):
        netloc = netloc.encode("idna").decode("ascii")
    return urlunsplit((p.scheme, netloc, quote(p.path, safe="/%:"), quote(p.query, safe="=&%"), ""))


def _get(url, timeout=12, limit=3_000_000, deadline=18):
    """deadline 是单次请求的总时长上限（秒）。
    有些服务器会"一点点慢慢挤"数据，单次读超时永远不触发，必须靠总时长兜底。"""
    t0 = time.time()
    req = urllib.request.Request(enc(url), headers={"User-Agent": UA, "Accept": "*/*"})
    r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
    chunks, total = [], 0
    while total < limit:
        if time.time() - t0 > deadline:
            raise TimeoutError("服务器太慢：超过 %ds 还没下完" % deadline)
        chunk = r.read(min(65536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def fetch_url(url, timeout=12, limit=3_000_000, deadline=18):
    """直连；GitHub raw 自动走加速前缀。返回 ok/data/ms/used/err"""
    global PROXY
    t0 = time.time()
    attempts = []
    if GH_RAW in url:
        with PROXY_LOCK:
            first = PROXY
        order = ([first] if first else []) + [p for p in GH_PROXIES if p != first]
        attempts += [p + url for p in order]
    attempts.append(url)
    last = ""
    per_try = 10 if GH_RAW in url else deadline
    for a in attempts:
        try:
            d = _get(a, timeout=timeout, limit=limit, deadline=per_try)
            if not d:
                raise ValueError("空内容")
            if GH_RAW in url and GH_RAW not in a and a != url:
                with PROXY_LOCK:
                    if not PROXY:
                        PROXY = a[: a.index("http", 8)]
            return {"ok": True, "data": d, "ms": int((time.time() - t0) * 1000),
                    "used": (PROXY + url) if (GH_RAW in url and PROXY) else a}
        except Exception as e:
            last = repr(e)[:110]
    return {"ok": False, "err": last, "ms": int((time.time() - t0) * 1000), "used": url}

# ---------------------------------------------------------------- 解析层

def strip_comment_lines(t):
    return "\n".join(l for l in t.splitlines()
                     if not (l.lstrip().startswith("//") or l.lstrip().startswith("#")))


def lenient_load(text):
    """真实 TVBox 配置的宽松解析：BOM/注释/尾逗号/控制字符/尾部垃圾"""
    strict_ok = False
    try:
        json.loads(text)
        strict_ok = True
    except Exception:
        pass
    t2 = strip_comment_lines(text)
    t3 = re.sub(r",(\s*[}\]])", r"\1", t2)
    for label, s in (("原始", text), ("去注释", t2), ("去注释+去尾逗号", t3)):
        try:
            return json.loads(s, strict=False), label, strict_ok
        except Exception:
            pass
        try:
            v, _ = json.JSONDecoder(strict=False).raw_decode(s.lstrip())
            return v, label + "+忽略尾部", strict_ok
        except Exception:
            pass
    return None, "失败", strict_ok

# ---------------------------------------------------------------- 体检层

SPIDER_CACHE, LIVE_CACHE, CACHE_LOCK = {}, {}, threading.Lock()


def check_spider(base, spider):
    key = (base, str(spider))
    with CACHE_LOCK:
        if key in SPIDER_CACHE:
            return SPIDER_CACHE[key]
    parts = str(spider).split(";")
    u = parts[0].strip()
    md5 = ""
    for i, p in enumerate(parts):
        if p.lower() == "md5" and i + 1 < len(parts):
            md5 = parts[i + 1].strip()
    gh = GH_RAW in u
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        u = urllib.parse.urljoin(base, u)      # ./fty.jar、../jar/x.jar 都按配置地址解析
    r = fetch_url(u, timeout=10, limit=3_000_000, deadline=8)
    rec = {"url": u, "gh": gh, "reachable": False, "magic": "", "size": 0, "ms": r["ms"], "md5_ok": None, "err": r.get("err", "")}
    if r["ok"]:
        d = r["data"]
        rec.update(reachable=True, size=len(d), magic="PK" if d[:4] == b"PK\x03\x04" else repr(d[:4]))
        if md5:
            rec["md5_ok"] = (hashlib.md5(d).hexdigest().lower() == md5.lower())
    with CACHE_LOCK:
        SPIDER_CACHE[key] = rec
    return rec


def check_live(base, lv):
    if not isinstance(lv, dict):
        return None
    u = lv.get("url")
    if not u:
        return None
    u = str(u).strip()
    m = re.match(r"^https?://127\.0\.0\.1:\d+/proxy\?do=live&url=(.+)$", u)
    if m:
        u = urllib.parse.unquote(m.group(1))
    if not re.match(r"^https?://", u):
        return None
    if not re.match(r"^https?://", u):
        return None
    key = u
    with CACHE_LOCK:
        if key in LIVE_CACHE:
            return LIVE_CACHE[key]
    r = fetch_url(u, timeout=10, limit=400_000, deadline=12)
    rec = {"url": u, "name": str(lv.get("name") or ""), "kind": "", "count": 0, "ms": r["ms"], "ok": False}
    if r["ok"]:
        t = r["data"].decode("utf-8-sig", "replace")
        if t.lstrip().startswith("#EXTM3U"):
            rec.update(kind="M3U", count=len(re.findall(r"^#EXTINF", t, re.M)))
        else:
            rec.update(kind="TXT", count=len([x for x in t.splitlines() if "http" in x]))
        rec["ok"] = rec["count"] > 0
    with CACHE_LOCK:
        LIVE_CACHE[key] = rec
    return rec

# ---------------------------------------------------------------- 评分层

def score(rec):
    if not rec.get("parsed"):
        return -999
    s = 10.0
    if rec["kind"] == "单仓":
        sites = rec.get("sites") or 0
        s += 30 + min(sites, 80) * 0.25
        if sites > 250:
            s -= 15
        elif sites > 120:
            s -= 5
        s += 12 if rec.get("strict") else 2
        sp = rec.get("spider")
        if sp:
            if sp.get("reachable") and sp.get("magic") == "PK":
                s += 20
                if sp.get("md5_ok"):
                    s += 3
            else:
                s -= 25
            if sp.get("gh"):
                s -= 12
        if rec.get("live_ok"):
            s += 5
    elif rec["kind"] == "多仓":
        s += 8 + (6 if rec.get("strict") else 0)
    s -= rec.get("ms", 5000) / 300.0
    s -= len(rec.get("out_url", "") or "") / 120.0
    return round(s, 1)


def verdict(rec):
    if not rec.get("parsed"):
        return rec.get("err") or "不可用"
    if rec["kind"] == "多仓":
        return "多仓（盒子里还要再选一次）"
    notes = []
    if not rec.get("strict"):
        notes.append("JSON 不严格")
    sp = rec.get("spider")
    if sp and not (sp.get("reachable") and sp.get("magic") == "PK"):
        notes.append("spider 失效")
    if sp and sp.get("gh"):
        notes.append("spider 在 GitHub")
    if rec.get("sites", 0) > 250:
        notes.append("站点过多")
    return " · ".join(notes) if notes else "正常"


def evaluate(src):
    rec = {"name": src["name"], "src": src["url"], "parsed": False}
    r = fetch_url(src["url"])
    rec["ms"] = r["ms"]
    rec["used"] = r["used"]
    if not r["ok"]:
        rec["err"] = "抓取失败"
        return rec
    text = r["data"].decode("utf-8-sig", "replace")
    rec["size"] = len(r["data"])
    obj, rule, strict_ok = lenient_load(text)
    rec["rule"], rec["strict"] = rule, strict_ok
    if obj is None:
        rec["err"] = "不是配置"
        return rec
    rec["parsed"] = True
    rec["obj"] = obj
    if isinstance(obj, dict) and isinstance(obj.get("urls"), list):
        rec["kind"] = "多仓"
        rec["entries"] = len(obj["urls"])
    elif isinstance(obj, dict) and obj.get("sites"):
        rec["kind"] = "单仓"
        rec["sites"] = len(obj["sites"])
        rec["lives"] = len(obj.get("lives") or [])
        rec["lives_raw"] = obj.get("lives") or []
        rec["parses"] = len(obj.get("parses") or [])
        if obj.get("spider"):
            rec["spider"] = check_spider(src["url"], obj["spider"])
    else:
        rec["parsed"] = False
        rec["err"] = "不是 TVBox 配置"
        return rec
    rec["out_url"] = r["used"]
    rec["score"] = score(rec)
    rec["verdict"] = verdict(rec)
    return rec


def live_rank(c):
    """直播源排序：优先 M3U、优先真·电视直播列表；打压一起看/轮播这类娱乐源"""
    n = c.get("name") or ""
    s = 60 if c["kind"] == "M3U" else 0
    s += min(c["count"], 500) * 0.1
    if re.search(r"自动更新|综合|直播|IPTV|央视|卫视", n, re.I):
        s += 50
    if re.search(r"一起看|轮播|B站|斗鱼|虎牙|音乐|影视", n):
        s -= 80
    return s


def pick_live(records):
    """从所有配置里挑直播源"""
    cands, seen = [], set()
    for rec in records:
        for item in rec.get("live_list") or []:
            if not item or not item.get("ok"):
                continue
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            cands.append(item)
    if not cands:
        return None, []
    ranked = sorted(cands, key=live_rank, reverse=True)
    best = ranked[0]
    return best, ranked[1:4]


def load_sources():
    """优先读同目录的 sources.json，方便你随时增删源，不用改代码"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                data = [x for x in data if isinstance(x, dict) and x.get("url")]
                if data:
                    return data
        except Exception:
            pass
    return SOURCES


def asset(rel):
    p = os.path.join(HERE, rel)
    if os.path.exists(p):
        return p
    base = getattr(sys, "_MEIPASS", None)
    if base:
        q = os.path.join(base, rel)
        if os.path.exists(q):
            return q
    return ""


def load_cloud():
    """有 cloud.json 就代表已经接了 Cloudflare 固定地址（分享版永久关闭）"""
    if not ALLOW_PUBLISH:
        return None
    p = os.path.join(HERE, "cloud.json")
    if os.path.exists(p):
        try:
            c = json.load(open(p, encoding="utf-8"))
            if c.get("upload_url") and c.get("upload_key"):
                return c
        except Exception:
            pass
    return None


def live_target(base, lv):
    """把一条 lives 解析成真正要请求的 URL（兼容 127.0.0.1 内置代理）"""
    if not isinstance(lv, dict):
        return None
    u = lv.get("url")
    if not u:
        return None
    u = str(u).strip()
    m = re.match(r"^https?://127\.0\.0\.1:\d+/proxy\?do=live&url=(.+)$", u)
    if m:
        u = urllib.parse.unquote(m.group(1))
    if not re.match(r"^https?://", u):
        return None
    return u


def build_publish_config(rec, live_best, live_url=""):
    """整理要发布的配置：live_url 存在时把精选直播合并进 lives（一个地址搞定配置+直播）"""
    obj = json.loads(json.dumps(rec["obj"], ensure_ascii=False))
    if live_url:
        obj["lives"] = [{"name": "精选直播", "type": 0, "url": live_url}]
        return obj
    lives = obj.get("lives") or []
    tested = {it.get("url"): it for it in (rec.get("live_list") or [])}
    bad = {u for u, it in tested.items() if not it.get("ok")}
    kept = []
    for lv in lives:
        t = live_target(rec.get("src") or "", lv)
        if t and t in bad:
            continue
        kept.append(lv)
    if live_best:
        kept = [x for x in kept if live_target(rec.get("src") or "", x) != live_best["url"]]
        kept.insert(0, {"name": "推荐直播", "type": 0, "url": live_best["url"]})
    obj["lives"] = kept
    return obj


def number_site_names(cfg):
    """给站点名统一加序号：1 旺仔|xxx、2 旺仔|yyy …"""
    for i, site in enumerate(cfg.get("sites") or [], 1):
        n = re.sub(r"^\d+\s+", "", str(site.get("name") or "")).strip()
        site["name"] = ("%d %s" % (i, n)) if n else str(i)
    return cfg


def publish_config(cfg, cloud):
    cfg = number_site_names(cfg)
    body = json.dumps(cfg, ensure_ascii=False).encode("utf-8")
    url = cloud["upload_url"] + "?key=" + urllib.parse.quote(cloud["upload_key"])
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
            if j.get("ok"):
                return True, "已发布 %d 站 / %d 直播" % (len(cfg.get("sites") or []), len(cfg.get("lives") or []))
            return False, "云端拒绝：" + str(j)[:100]
    except Exception as e:
        return False, "发布失败：%s" % repr(e)[:90]


# ---------------------------------------------------------------- 直播源

LIVE_LISTS = [
    ("zbds", "https://live.zbds.top/tv/iptv4.m3u"),
    ("hacks央视", "https://live.hacks.tools/tv/ipv4/categories/%E5%A4%AE%E8%A7%86%E9%A2%91%E9%81%93.m3u"),
    ("hacks卫视", "https://live.hacks.tools/tv/ipv4/categories/%E5%8D%AB%E8%A7%86%E9%A2%91%E9%81%93.m3u"),
    ("小马大列表", "https://szyyds.cn/tv/live/x.txt"),
    ("aptv", "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u"),
    ("iptv-org-cn", "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u"),
]
# 可选：自建 Worker 之后把域名填进来（见 docs/自建服务器.md），用来代理抓 GitHub / jsDelivr。
# 留空则跳过这两个列表，其余 6 个列表照常工作，只是少一些低清卫视和体育台的候选。
PROXY_BASE = ""          # 例：PROXY_BASE = "https://tv.example.com"

if PROXY_BASE:
    LIVE_LISTS.append(("myIPTV", PROXY_BASE.rstrip("/") + "/proxy?url=" + urllib.parse.quote(
        "https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u", safe="")))
    LIVE_LISTS.append(("guovin-gd", PROXY_BASE.rstrip("/") + "/proxy?url=" + urllib.parse.quote(
        "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u", safe="")))


LIVE_BACKUPS = [
    ("zbds 全量（357 频道）", "https://live.zbds.top/tv/iptv4.m3u"),
    ("hacks 央视频道", "https://live.hacks.tools/tv/ipv4/categories/%E5%A4%AE%E8%A7%86%E9%A2%91%E9%81%93.m3u"),
    ("hacks 卫视频道", "https://live.hacks.tools/tv/ipv4/categories/%E5%8D%AB%E8%A7%86%E9%A2%91%E9%81%93.m3u"),
    ("小马大列表（6622 条）", "https://szyyds.cn/tv/live/x.txt"),
    ("aptv（GitHub · 120 条）", "https://gh-proxy.com/https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u"),
    ("iptv-org 中国（510 条）", "https://gh-proxy.com/https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u"),
]

LIVE_WS = [
    "北京卫视", "江苏卫视", "山东卫视", "湖北卫视", "河北卫视", "云南卫视", "广西卫视",
    "海南卫视", "重庆卫视", "吉林卫视", "安徽卫视", "东方卫视", "东南卫视",
    "甘肃卫视", "江西卫视", "辽宁卫视", "深圳卫视", "天津卫视", "新疆卫视",
    "延边卫视", "浙江卫视", "黑龙江卫视", "大湾区卫视", "河南卫视",
]


def clean_m3u_url(u):
    u = str(u).split("#")[0].split("$")[0].strip()
    while u.endswith("?") or u.endswith("&"):
        u = u[:-1]
    return u


def norm_live_name(n):
    return re.sub(r"[\s_\-]", "", str(n)).upper()


def parse_playlist_text(t):
    items = []
    if t.lstrip().startswith("#EXTM3U"):
        n = None
        for line in t.splitlines():
            if line.startswith("#EXTINF"):
                n = line.split(",", 1)[-1].strip()
            elif line.startswith("http") and n:
                items.append((n, line.strip()))
                n = None
        return items
    for line in t.splitlines():
        line = line.strip()
        if not line or "http" not in line:
            continue
        m = re.search(r"https?://\S+", line)
        if not m:
            continue
        items.append((line[:m.start()].strip(" ,，|").strip(), m.group(0)))
    return items


def live_probe(url, timeout=3):
    try:
        t0 = time.time()
        d = _get(url, timeout=timeout, limit=2048, deadline=timeout)
        ms = int((time.time() - t0) * 1000)
        if d[:7] == b"#EXTM3U" or d[:1] == b"\x47":
            return ms
    except Exception:
        pass
    return None




# ---------------------------------------------------------------- 直播分辨率检测
RES_WINDOW = 100          # 分辨率优选：最快线路 +100ms 窗口内优先高分辨率
RES_CACHE_DIR = os.path.join(HERE, "cache")


class _BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def u(self, n):
        v = 0
        for _ in range(n):
            if self.pos >= len(self.data) * 8:
                raise EOFError
            byte = self.data[self.pos >> 3]
            bit = (byte >> (7 - (self.pos & 7))) & 1
            v = (v << 1) | bit
            self.pos += 1
        return v

    def ue(self):
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise EOFError
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)


def _parse_sps(nal):
    br = _BitReader(nal)
    profile = br.u(8)
    br.u(8)
    br.u(8)
    br.ue()
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = br.ue()
        if chroma == 3:
            br.u(1)
        br.ue()
        br.ue()
        br.u(1)
        if br.u(1):
            n = 8 if chroma != 3 else 12
            for _ in range(n):
                if br.u(1):
                    br.ue()
                    br.ue()
                    br.ue()
                    br.ue()
    br.ue()
    poc = br.ue()
    if poc == 0:
        br.ue()
    elif poc == 1:
        br.u(1)
        br.ue()
        br.ue()
        for _ in range(br.ue()):
            br.ue()
    br.ue()
    br.u(1)
    w = (br.ue() + 1) * 16
    h = (br.ue() + 1) * 16
    if br.u(1) == 0:
        h = h * 2
    return w, h


def _find_sps(ts):
    pat = re.compile(b"\x00\x00\x00\x01\x67([\x00-\xff]{4,80})")
    for m in pat.finditer(ts):
        try:
            w, h = _parse_sps(m.group(1))
            if 100 <= w <= 4096 and 100 <= h <= 4096:
                return w, h
        except Exception:
            continue
    return None


# 用户电视核实为 1080p、但自动检测不出分辨率的频道（用于排序）
RES_OVERRIDE = {"江苏卫视": 1088, "北京卫视": 1088, "大湾区卫视": 1088, "黑龙江卫视": 1088,
                 "东方卫视": 1088, "东南卫视": 1088}

PREFERRED_MBPS = 2.5    # 优先码率（电视解码更稳）
HARD_MAX_MBPS = 3.0     # 超过此码率的线路尽量不用


def _segment_size(url, timeout=8):
    """用 Range 请求只取 2 字节，读 Content-Range 得到分片总大小。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-1"})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            cr = r.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)", cr)
            if m:
                return int(m.group(1))
            cl = r.headers.get("Content-Length")
            if cl:
                return int(cl)
    except Exception:
        pass
    return 0


def probe_bitrate(url):
    """只估算码率（Mbps），比 detect_stream 轻量；结果缓存 7 天。"""
    key = hashlib.md5(str(url).encode()).hexdigest() + ".br"
    path = os.path.join(RES_CACHE_DIR, key)
    try:
        d = json.load(open(path, encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) < 7 * 86400:
            return float(d.get("mbps", 0))
    except Exception:
        pass
    mbps = 0.0
    try:
        d = _get(url, timeout=6, limit=20000, deadline=8)
        txt = d.decode("utf-8", "replace")
        if "#EXT-X-STREAM-INF" in txt:
            bws = [int(x) for x in re.findall(r"BANDWIDTH=(\d+)", txt)]
            if bws:
                mbps = max(bws) / 1e6
            # 从第一个变体里判断编码，避免 master 一律 unknown
            lines = txt.splitlines()
            vurl = None
            for li, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF") and li + 1 < len(lines):
                    nxt = lines[li + 1].strip()
                    if nxt and not nxt.startswith("#"):
                        vurl = urllib.parse.urljoin(url, nxt)
                        break
            if vurl:
                try:
                    vd = _get(vurl, timeout=8, limit=20000, deadline=9).decode("utf-8", "replace")
                    vsegs = [x.strip() for x in vd.splitlines() if x.strip() and not x.startswith("#")]
                    if vsegs:
                        vseg = _get(urllib.parse.urljoin(vurl, vsegs[0]), timeout=8, limit=400000, deadline=10)
                        if vseg:
                            codec = _ts_codec(vseg)
                except Exception:
                    pass
        else:
            segs = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#")]
            if segs:
                dur = 6.0
                durs = re.findall(r"#EXTINF:([\d.]+)", txt)
                if durs:
                    dur = float(durs[0])
                size = _segment_size(urllib.parse.urljoin(url, segs[0]))
                if size:
                    mbps = size * 8 / dur / 1e6
    except Exception:
        pass
    try:
        os.makedirs(RES_CACHE_DIR, exist_ok=True)
        json.dump({"ts": time.time(), "mbps": mbps}, open(path, "w", encoding="utf-8"))
    except Exception:
        pass
    return mbps


STREAM_TYPE = {
    0x01: "mpeg2v", 0x02: "mpeg2v", 0x1B: "h264", 0x24: "hevc",
    0x1D: "avs", 0x42: "avs", 0xD2: "avs", 0x03: "audio", 0x04: "audio",
    0x0F: "audio", 0x11: "audio", 0x81: "audio", 0x87: "audio",
}


def _ts_codec(seg):
    """解析 TS 的 PAT/PMT，返回视频编码：h264 / hevc / avs / other / audio。"""
    try:
        pmt_pid = None
        for i in range(0, len(seg) - 188, 188):
            pkt = seg[i:i + 188]
            if pkt[0] != 0x47:
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pid != 0 or not (pkt[1] & 0x40):
                continue
            off = 4 + (pkt[4] if (pkt[3] & 0x20) else 0)
            pl = pkt[off:]
            sec = pl[1 + pl[0]:]
            if not sec or sec[0] != 0x00:
                continue
            sl = ((sec[1] & 0x0F) << 8) | sec[2]
            body = sec[8:3 + sl - 4]
            for j in range(0, len(body) - 3, 4):
                prog = (body[j] << 8) | body[j + 1]
                pid2 = ((body[j + 2] & 0x1F) << 8) | body[j + 3]
                if prog != 0:
                    pmt_pid = pid2
                    break
            break
        if pmt_pid is None:
            return "unknown"
        for i in range(0, len(seg) - 188, 188):
            pkt = seg[i:i + 188]
            if pkt[0] != 0x47:
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            if pid != pmt_pid:
                continue
            off = 4 + (pkt[4] if (pkt[3] & 0x20) else 0)
            pl = pkt[off:]
            sec = pl[1 + pl[0]:]
            if not sec or sec[0] != 0x02:
                continue
            sl = ((sec[1] & 0x0F) << 8) | sec[2]
            pos = 12
            kinds = []
            while pos + 5 <= 3 + sl - 4:
                st = sec[pos]
                info_len = ((sec[pos + 3] & 0x0F) << 8) | sec[pos + 4]
                kinds.append(STREAM_TYPE.get(st, "other:0x%02X" % st))
                pos += 5 + info_len
            for k in kinds:
                if k in ("h264", "hevc", "avs"):
                    return k
            return kinds[0] if kinds else "unknown"
    except Exception:
        pass
    return "unknown"


def detect_stream(url):
    """返回 (高度, 宽度, 码率Mbps)；未知返回 (0, 0, 0)。缓存 7 天。"""
    key = hashlib.md5(str(url).encode()).hexdigest() + ".res2"
    path = os.path.join(RES_CACHE_DIR, key)
    try:
        d = json.load(open(path, encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) < 7 * 86400 and "codec" in d:
            return int(d.get("h", 0)), int(d.get("w", 0)), float(d.get("mbps", 0)), d.get("codec", "unknown")
    except Exception:
        pass
    h = w = 0
    mbps = 0.0
    codec = "unknown"
    try:
        d = _get(url, timeout=6, limit=20000, deadline=8)
        txt = d.decode("utf-8", "replace")
        if "#EXT-X-STREAM-INF" in txt:
            rs = re.findall(r"RESOLUTION=(\d+)x(\d+)", txt)
            if rs:
                w, h = max((int(a), int(b)) for a, b in rs)
            bws = [int(x) for x in re.findall(r"BANDWIDTH=(\d+)", txt)]
            if bws:
                mbps = max(bws) / 1e6
        else:
            segs = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#")]
            if segs:
                dur = 6.0
                durs = re.findall(r"#EXTINF:([\d.]+)", txt)
                if durs:
                    dur = float(durs[0])
                su = urllib.parse.urljoin(url, segs[0])
                size = _segment_size(su)
                seg = _get(su, timeout=8, limit=400000, deadline=10)
                if seg:
                    codec = _ts_codec(seg)
                    if codec == "h264":
                        sps = _find_sps(seg)
                        if sps:
                            w, h = sps
                if size:
                    mbps = size * 8 / dur / 1e6
    except Exception:
        pass
    try:
        os.makedirs(RES_CACHE_DIR, exist_ok=True)
        json.dump({"ts": time.time(), "h": h, "w": w, "mbps": mbps, "codec": codec}, open(path, "w", encoding="utf-8"))
    except Exception:
        pass
    return h, w, mbps, codec


def detect_resolution(url):
    h, w, _, _ = detect_stream(url)
    return h, w


LIVE_PROBE_TTL_OK = 3600        # 成功测速缓存 1 小时
LIVE_PROBE_TTL_FAIL = 6 * 3600  # 失败结果缓存 6 小时


def _probe_cached(url, timeout=5):
    """带磁盘缓存的 live_probe：同一线路 1 小时内不重复测。"""
    key = hashlib.md5(("lp|" + str(url)).encode()).hexdigest() + ".lp"
    path = os.path.join(HERE, "cache", key)
    now = time.time()
    try:
        d = json.load(open(path, encoding="utf-8"))
        ms = d.get("ms")
        age = now - float(d.get("ts", 0))
        if ms is None and age < LIVE_PROBE_TTL_FAIL:
            return None
        if isinstance(ms, int) and ms >= 0 and age < LIVE_PROBE_TTL_OK:
            return ms
    except Exception:
        pass
    ms = live_probe(url, timeout)
    try:
        os.makedirs(os.path.join(HERE, "cache"), exist_ok=True)
        json.dump({"ts": now, "ms": ms}, open(path, "w", encoding="utf-8"))
    except Exception:
        pass
    return ms


def build_live(log=print):
    """收集所有直播列表 → 筛央视+卫视 → 全局并发测速 → 生成 m3u。"""
    log("正在收集直播列表…")
    pool = {}

    def add(name, url):
        url = clean_m3u_url(url)
        if not url.startswith("http") or "[" in url or "/rtp/" in url or "udp://" in url:
            return
        low = url.lower()
        # 过滤广播/境外源，避免把音频流或国外频道混进电视剧频道
        if any(x in low for x in ("qingting.fm", "radio", "/64k", "ebs.co.kr", "kbs.co.kr",
                                  "nhk.or.jp", "bbc.co.uk", "tvb.com.hk")):
            return
        # 已知兼容性差：这家的 1080p 在部分电视上只有声音没有画面
        if any(h in low for h in ("yncbn.ks-cdn.gitv.tv", "bp-resource-dfl.bestv.cn")):
            return
        if any(x in str(name) for x in ("广播", "电台", "FM")):
            return
        k = norm_live_name(name)
        pool.setdefault(k, [])
        if url not in pool[k]:
            pool[k].append(url)

    def load_list(pair):
        tag, u = pair
        try:
            # 列表缓存 6 小时，避免每次都重新下载（尤其 GitHub 源和小马 2MB 大列表）
            cache_dir = os.path.join(HERE, "cache")
            key = hashlib.md5(u.encode()).hexdigest() + ".list"
            cp = os.path.join(cache_dir, key)
            data = None
            if os.path.exists(cp) and time.time() - os.path.getmtime(cp) < 6 * 3600:
                try:
                    data = open(cp, "rb").read()
                except Exception:
                    data = None
            if not data:
                r = fetch_url(u, timeout=20, limit=6_000_000, deadline=25)
                if not r["ok"]:
                    return tag, []
                data = r["data"]
                try:
                    os.makedirs(cache_dir, exist_ok=True)
                    open(cp, "wb").write(data)
                except Exception:
                    pass
            return tag, parse_playlist_text(data.decode("utf-8-sig", "replace"))
        except Exception:
            return tag, []

    with ThreadPoolExecutor(max_workers=5) as ex:
        for tag, items in ex.map(load_list, LIVE_LISTS):
            for n, lu in items:
                add(n, lu)
            log("  %-10s %d 条" % (tag, len(items)))
    log("  候选频道：%d" % len(pool))

    targets = []
    for i in range(1, 18):
        targets.append(("央视", "CCTV-%d" % i,
                        [norm_live_name("CCTV%d" % i), norm_live_name("CCTV-%d" % i)]))
    for name in LIVE_WS:
        base = norm_live_name(name)
        keys = [base]
        for k in pool:
            if k != base and (k.startswith(base) or base in k):
                keys.append(k)
        targets.append(("卫视", name, keys[:8]))

    # 体育分类：央视系体育台优先，再用关键词补充，最多 6 个
    SPORT_NAMES = ["CCTV5+", "CCTV-5+", "CCTV5+体育赛事", "高尔夫网球", "风云足球", "央视台球", "五星体育"]
    SPORT_BAD = ("VS", "回放", "重播", "纬来", "MBC", "欧洲体育", "澳门", "澳视", "台湾", "TVB", "游戏风云")
    sport_seen = set()
    sport_targets = []
    for nm in SPORT_NAMES:
        k = norm_live_name(nm)
        if k in sport_seen or not pool.get(k):
            continue
        sport_targets.append(("体育", nm, [k]))
        sport_seen.add(k)
    if len(sport_targets) < 12:
        for k in pool:
            if len(sport_targets) >= 12:
                break
            if k in sport_seen or any(b in k for b in SPORT_BAD) or len(k) > 14:
                continue
            if any(w in k for w in ("体育", "足球", "篮球", "台球", "网球", "高尔夫", "赛车", "电竞", "极限")):
                sport_targets.append(("体育", k, [k]))
                sport_seen.add(k)
    # 去重：同一频道的不同叫法只留一个；CCTV5 已在央视组，不重复放
    dedup = []
    seen_fam = set()
    for grp, disp, keys in sport_targets:
        fam = keys[0].replace("CCTV", "").replace("央视", "").replace("-", "").strip()
        if fam.startswith("5"):
            if "+" not in fam:
                continue
            fam = "5+"  # CCTV5+ 与 CCTV5+体育赛事 视为同一频道
        if fam in seen_fam:
            continue
        seen_fam.add(fam)
        dedup.append((grp, disp, keys))
    sport_targets = dedup[:12]
    targets += sport_targets
    if sport_targets:
        log("  体育候选：%d 个频道" % len(sport_targets))


    jobs = []
    for grp, disp, keys in targets:
        cands = []
        for k in keys:
            cands += pool.get(k, [])
        cands = [c for c in dict.fromkeys(cands) if c][:10]
        for u in cands:
            jobs.append((disp, u))

    log("  待测线路：%d 条（并发 40）" % len(jobs))
    best_map = {}
    results_map = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=40) as ex:
            futs = {ex.submit(_probe_cached, u): (disp, u) for disp, u in jobs}
            for f, (disp, u) in futs.items():
                try:
                    ms = f.result()
                except Exception:
                    ms = None
                if ms is not None:
                    results_map.setdefault(disp, []).append((ms, u))
                    cur = best_map.get(disp)
                    if cur is None or ms < cur[0]:
                        best_map[disp] = (ms, u)

    # 第一轮没测到的频道，用更多候选补测一轮（避免误判缺台）
    extras = []
    for grp, disp, keys in targets:
        if disp in best_map:
            continue
        cands = []
        for k in keys:
            cands += pool.get(k, [])
        cands = [c for c in dict.fromkeys(cands) if c][:10]
        for u in cands:
            extras.append((disp, u))
    if extras:
        log("  补测 %d 条候选线路…" % len(extras))
        with ThreadPoolExecutor(max_workers=40) as ex:
            futs = {ex.submit(_probe_cached, u): (disp, u) for disp, u in extras}
            for f, (disp, u) in futs.items():
                try:
                    ms = f.result()
                except Exception:
                    ms = None
                if ms is not None:
                    results_map.setdefault(disp, []).append((ms, u))
                    cur = best_map.get(disp)
                    if cur is None or ms < cur[0]:
                        best_map[disp] = (ms, u)

    # 慢速/缺失频道复测：最多 3 轮，取最快成绩（江苏卫视测全部候选）
    def _retest_slow():
        retry_targets = []
        for grp, disp, keys in targets:
            cur = best_map.get(disp)
            if cur is None or cur[0] > LIVE_MAX_MS:
                retry_targets.append((grp, disp, keys))
        if not retry_targets:
            return
        log("  复测 %d 个慢速/缺失频道（最多 3 轮，取最快）…" % len(retry_targets))
        for rnd in range(3):
            if not retry_targets:
                break
            jobs2 = []
            for grp, disp, keys in retry_targets:
                cands = []
                for k in keys:
                    cands += pool.get(k, [])
                limit = 40 if disp == "江苏卫视" else 12
                cands = [c for c in dict.fromkeys(cands) if c][:limit]
                for u in cands:
                    jobs2.append((disp, u))
            if not jobs2:
                break
            with ThreadPoolExecutor(max_workers=40) as ex:
                futs = {ex.submit(live_probe, u): (disp, u) for disp, u in jobs2}
                for f, (disp, u) in futs.items():
                    try:
                        ms = f.result()
                    except Exception:
                        ms = None
                    if ms is not None:
                        results_map.setdefault(disp, []).append((ms, u))
                        cur = best_map.get(disp)
                        if cur is None or ms < cur[0]:
                            best_map[disp] = (ms, u)
            left = []
            for grp, disp, keys in retry_targets:
                cur = best_map.get(disp)
                if cur is None or cur[0] > LIVE_MAX_MS:
                    left.append((grp, disp, keys))
            retry_targets = left
            log("    第 %d 轮复测完成，仍偏慢/缺失 %d 个" % (rnd + 1, len(retry_targets)))

    _retest_slow()

    # 分辨率优选：仅央视/卫视，在最快线路 +100ms 窗口内优先 1080p
    def _upgrade_resolution():
        # 第一阶段：收集所有需要检测的候选（最多 8 个/频道）
        plan = []
        need = set()
        for grp, disp, keys in targets:
            if grp not in ("央视", "卫视"):
                continue
            cands = results_map.get(disp) or []
            if not cands:
                continue
            window = sorted(cands, key=lambda c: c[0])[:8]
            plan.append((grp, disp, window))
            for ms, u in window:
                need.add(u)
        if not need:
            return
        # 第二阶段：并发检测（detect_stream 自带 7 天缓存）
        info = {}

        def _detect(u):
            try:
                return u, detect_stream(u)
            except Exception:
                return u, (0, 0, 0, "unknown")

        with ThreadPoolExecutor(max_workers=24) as ex:
            for u, r in ex.map(_detect, list(need)):
                info[u] = r
        # 第三阶段：每频道择优
        changed = []
        for grp, disp, window in plan:
            cur = best_map.get(disp)
            if not cur:
                continue
            infos = []
            for ms, u in window:
                h, w, mbps, codec = info.get(u, (0, 0, 0, "unknown"))
                infos.append((ms, u, h, w, mbps, codec))
            good = [x for x in infos if x[5] in ("h264", "hevc")]
            if not good:
                good = infos
            hd_ok = [x for x in good if x[2] >= 1000 and 0 < x[4] <= 4.5]
            if not hd_ok and grp == "卫视":
                # 仅卫视：次选 1080p 高码率（4.5~11Mbps），央视保持原策略
                hd_ok = [x for x in good if x[2] >= 1000 and 4.5 < x[4] <= 11.0]
            if hd_ok:
                chosen = min(hd_ok, key=lambda x: (0 if x[5] == "h264" else 1, x[4], x[0]))
            else:
                mid_ok = [x for x in good if x[2] >= 720 and 0 < x[4] <= 3.0]
                if mid_ok:
                    chosen = min(mid_ok, key=lambda x: (0 if x[5] == "h264" else 1, x[4], x[0]))
                else:
                    low_ok = [x for x in good if 0 < x[4] <= 3.0]
                    known = [x for x in good if 0 < x[4]]
                    chosen = min(low_ok, key=lambda x: (x[4], x[0])) if low_ok else (
                        min(known, key=lambda x: (x[4], x[0])) if known else min(good, key=lambda x: x[0]))
            if chosen[1] != cur[1]:
                best_map[disp] = (chosen[0], chosen[1])
                changed.append((disp, chosen[2], chosen[4], chosen[0]))
        if changed:
            log("  清晰度优选：%s" % "、".join(
                "%s(%dp %.1fM %dms)" % (n, h, mbps, ms) for n, h, mbps, ms in changed[:14]))

    _upgrade_resolution()

    channels = []
    too_slow = []
    for grp, disp, keys in targets:
        r = best_map.get(disp)
        if not r:
            continue
        if grp in ("央视", "卫视"):
            # 央视/卫视：没有 300ms 以内也必须保留，用最快的一条
            channels.append((grp, disp, r[1], r[0]))
            continue
        if r[0] > LIVE_MAX_MS:
            too_slow.append((disp, r[0]))
            continue
        channels.append((grp, disp, r[1], r[0]))
    if too_slow:
        log("  超过 %dms 未采用：%s" % (LIVE_MAX_MS, "、".join("%s(%dms)" % (n, m) for n, m in too_slow[:6])))
    slow_kept = [(c[1], c[3]) for c in channels if c[0] in ("央视", "卫视") and c[3] > LIVE_MAX_MS]
    if slow_kept:
        log("  超过 %dms 但保留（央视/卫视）：%s" % (LIVE_MAX_MS, "、".join("%s(%dms)" % x for x in slow_kept[:8])))
    missing = [disp for grp, disp, keys in targets if grp in ("央视", "卫视") and disp not in best_map]
    if missing:
        log("  ⚠ 无可用线路（缺席）：%s" % "、".join(missing[:10]))
    sports = sorted([c for c in channels if c[0] == "体育"], key=lambda c: c[3])[:6]
    rest = [c for c in channels if c[0] != "体育"] + sports

    def _num(name):
        mm = re.search(r"CCTV[- ]?(\d+)", str(name))
        if mm:
            return int(mm.group(1))
        mm = re.search(r"(\d+)", str(name))
        return int(mm.group(1)) if mm else 99

    cctv = sorted([c for c in rest if c[0] == "央视"], key=lambda c: _num(c[1]))
    # 卫视固定按分辨率从高到低排序（同分辨率按台名固定），不再按延迟乱序
    ws = [c for c in rest if c[0] == "卫视"]
    sp = sorted([c for c in rest if c[0] == "体育"], key=lambda c: c[3])

    def _res_h(c):
        try:
            h = detect_stream(c[2])[0]
        except Exception:
            h = 0
        return h or RES_OVERRIDE.get(c[1], 0)

    ws = sorted(ws, key=lambda c: (-_res_h(c), c[1]))
    channels = cctv + ws + sp
    log("  可用频道：%d（央视 %d / 卫视 %d / 体育 %d）" % (
        len(channels), sum(1 for c in channels if c[0] == "央视"),
        sum(1 for c in channels if c[0] == "卫视"), len(sports)))

    lines = ["#EXTM3U"]
    for grp, name, url, ms in channels:
        lines.append('#EXTINF:-1 group-title="%s",%s' % (grp, name))
        lines.append(url)
    m3u = "\n".join(lines) + "\n"
    ms_list = [c[3] for c in channels] or [0]
    return {"m3u": m3u, "channels": channels, "count": len(channels),
            "avg": int(sum(ms_list) / len(ms_list)), "min": min(ms_list), "max": max(ms_list),
            "backups": LIVE_BACKUPS}


def upload_live(m3u, cloud):
    # 发布前自动备份线上现有直播（方便回滚）
    try:
        _https, _http = live_urls(cloud)
        old = _get(_http, timeout=15, limit=900000).decode("utf-8-sig", "replace")
        if old and old != m3u:
            bp = os.path.join(os.path.dirname(HERE), "备份",
                              "live_自动备份_%s.m3u" % time.strftime("%Y%m%d_%H%M%S"))
            os.makedirs(os.path.dirname(bp), exist_ok=True)
            open(bp, "w", encoding="utf-8").write(old)
    except Exception:
        pass
    base = cloud["upload_url"].replace("/upload", "/upload-live")
    url = base + "?key=" + urllib.parse.quote(cloud["upload_key"])
    req = urllib.request.Request(url, data=m3u.encode("utf-8"), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "audio/x-mpegurl"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
            if j.get("ok"):
                return True, "已发布 %d 个频道" % j.get("channels", 0)
            return False, "云端拒绝：" + str(j)[:80]
    except Exception as e:
        return False, "直播上传失败：%s" % repr(e)[:90]


def live_urls(cloud):
    if not cloud:
        return "", ""
    cfg = cloud.get("config_url") or ""
    base = cfg.rsplit("/", 1)[0] if cfg else cloud["upload_url"].rsplit("/upload", 1)[0]
    https_url = base + "/live.m3u"
    http_url = re.sub(r"^https://", "http://", https_url, flags=re.I)
    return https_url, http_url


# ---------------------------------------------------------------- 自定义单仓：数据源延迟

def site_home_latency(api, timeout=5):
    """测“首页数据加载”延迟：TVBox 选中数据源时请求的接口，取两次里最快的一次。"""
    if not str(api).startswith("http"):
        return None
    urls = []
    for ac in ("videolist", "detail"):
        if re.search(r"[?&]ac=", api):
            u = re.sub(r"([?&])ac=[^&]*", r"\1ac=" + ac, api)
        else:
            u = api + ("&" if "?" in api else "?") + "ac=" + ac
        u += ("&" if "?" in u else "?") + "pg=1"
        urls.append(u)
    for u in urls:
        try:
            t0 = time.time()
            d = _get(u, timeout=min(timeout, 3), limit=200_000, deadline=min(timeout, 3))
            ms = int((time.time() - t0) * 1000)
            if len(d) > 0:
                return ms
        except Exception:
            pass
    return None


def refresh_custom_names(cfg, log=print):
    """测速并按快慢排序，但不在名字后追加延迟（名称由用户自定义）。"""
    sites = cfg.get("sites") or []
    if not sites:
        return cfg

    def measure(s):
        name = re.sub(r"\s*[·•]\s*(\d+ms|\?)\s*$", "", str(s.get("name") or ""))
        api = str(s.get("api") or "")
        if not api.startswith("http"):
            return name, None, True
        return name, site_home_latency(api), False

    with ThreadPoolExecutor(max_workers=min(10, len(sites))) as ex:
        pairs = list(ex.map(measure, sites))
    rows = []
    for s, (base, ms, keep) in zip(sites, pairs):
        s["name"] = base
        rows.append((0 if keep else (ms if ms else 99999), s))
    rows.sort(key=lambda x: x[0])
    cfg["sites"] = [s for _, s in rows]
    log("  数据源已按延迟排序（名字不显示延迟）")
    return cfg


def cloud_status(cloud):
    """读取云端当前状态（站点数 / 直播频道数）"""
    cfg = cloud.get("config_url") or ""
    base = cfg.rsplit("/", 1)[0] if cfg else cloud["upload_url"].rsplit("/upload", 1)[0]
    try:
        req = urllib.request.Request(base + "/status", headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _live_pipeline(log=print):
    """完整直播流程：收集 → 测速 → 上传。"""
    cloud = load_cloud()
    if not cloud:
        return None
    try:
        live = build_live(log)
        https_url, http_url = live_urls(cloud)
        live.update({"https": https_url, "http": http_url, "upload_ok": None, "upload_msg": ""})
        if live["count"]:
            log("正在发布直播源…")
            ok, msg = upload_live(live["m3u"], cloud)
            live["upload_ok"], live["upload_msg"] = ok, msg
            log(("  ✓ " if ok else "  ✗ ") + msg)
        return live
    except Exception as e:
        log("直播源处理失败 " + repr(e)[:100])
        return {"count": 0, "m3u": "", "backups": LIVE_BACKUPS, "https": "", "http": "",
                "upload_ok": False, "upload_msg": repr(e)[:100], "avg": 0, "min": 0, "max": 0, "channels": []}


def verify_current_live(log=print, threshold=0.85):
    """轻量校验：只测云端现有直播清单里的频道，不全量重建。"""
    out = {"count": 0, "total": 0, "avg": 0, "healthy": False, "https": "", "http": "",
           "channels": [], "backups": LIVE_BACKUPS, "upload_msg": "未配置云端"}
    cloud = load_cloud()
    if not cloud:
        return out
    https_url, http_url = live_urls(cloud)
    out["https"], out["http"] = https_url, http_url
    try:
        d = _get(http_url, timeout=12, limit=500_000)
        items = parse_playlist_text(d.decode("utf-8-sig", "replace"))
    except Exception:
        out["upload_msg"] = "云端直播读取失败"
        return out
    items = items[:80]
    if not items:
        out["upload_msg"] = "云端直播为空"
        return out
    urls = [u for _, u in items]
    ok = []
    with ThreadPoolExecutor(max_workers=24) as ex:
        for (name, u), ms in zip(items, ex.map(live_probe, urls)):
            if ms is not None:
                grp = "央视" if "CCTV" in str(name).upper() or "央视" in str(name) else "卫视"
                ok.append((grp, name, u, ms))
    total, count = len(urls), len(ok)
    avg = int(sum(x[3] for x in ok) / count) if count else 0
    healthy = count >= max(1, int(total * threshold))
    log("  现有直播校验：%d/%d 可用（平均 %dms）" % (count, total, avg))
    out.update({"count": count, "total": total, "avg": avg, "healthy": healthy, "channels": ok,
                "upload_msg": ("沿用云端现有直播（%d/%d 可用）" % (count, total)) if healthy else "现有直播可用率偏低"})
    return out


def probe_sources_quick(log=print):
    """快速测所有配置源（只取配置、不查 spider），返回按延迟排序的记录。"""
    srcs = load_sources()

    def one(src):
        t0 = time.time()
        try:
            r = fetch_url(src["url"], timeout=8, limit=3_000_000, deadline=10)
            if not r["ok"]:
                return {"name": src["name"], "out_url": src["url"], "kind": "-", "sites": 0,
                        "ms": int((time.time() - t0) * 1000), "verdict": "失败"}
            obj, rule, strict = lenient_load(r["data"].decode("utf-8-sig", "replace"))
            sites = len(obj.get("sites") or []) if isinstance(obj, dict) else 0
            entries = len(obj.get("urls") or []) if isinstance(obj, dict) else 0
            kind = "单仓" if sites else ("多仓" if entries else "非配置")
            ms = int((time.time() - t0) * 1000)
            return {"name": src["name"], "out_url": src["url"], "kind": kind,
                    "sites": sites, "entries": entries, "ms": ms,
                    "verdict": "正常" if (sites or entries) else "非配置"}
        except Exception:
            return {"name": src["name"], "out_url": src["url"], "kind": "-", "sites": 0,
                    "ms": 99999, "verdict": "失败"}

    with ThreadPoolExecutor(max_workers=13) as ex:
        rows = list(ex.map(one, srcs))
    ok = [x for x in rows if x["verdict"] == "正常"]
    ok.sort(key=lambda x: x["ms"])
    if ok:
        log("  配置源延迟：%s" % "、".join("%s %dms" % (x["name"], x["ms"]) for x in ok[:5]))
    return ok


def probe_live_lists(log=print):
    """测所有备用直播列表，返回按延迟排序的记录。"""
    def one(item):
        name, url = item
        t0 = time.time()
        try:
            d = _get(url, timeout=10, limit=2_000_000, deadline=12)
            ms = int((time.time() - t0) * 1000)
            txt = d.decode("utf-8-sig", "replace")
            n = txt.count("#EXTINF")
            if n == 0:
                n = sum(1 for l in txt.splitlines() if "http" in l)
            return {"name": name, "url": url, "ms": ms, "count": n, "ok": n > 0}
        except Exception:
            return {"name": name, "url": url, "ms": 99999, "count": 0, "ok": False}

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(one, LIVE_BACKUPS))
    ok = [x for x in rows if x["ok"]]
    ok.sort(key=lambda x: x["ms"])
    if ok:
        log("  直播列表延迟：%s" % "、".join("%s %dms" % (x["name"], x["ms"]) for x in ok[:5]))
    return ok


def refresh_live_only(log=print):
    """只刷新直播：测所有备用直播列表，取最快 5 个，第一名填备用直链。"""
    cloud = load_cloud()
    rows = probe_live_lists(log)
    https_url, http_url = live_urls(cloud) if cloud else ("", "")
    st = cloud_status(cloud) if cloud else {}
    result = {"best": None, "backups": [], "all": [], "cloud": None}
    result["live"] = {
        "count": (st or {}).get("live_channels", 0) or 0,
        "m3u": "", "backups": [(r["name"], r["url"]) for r in rows],
        "fastest_backup": rows[0]["url"] if rows else "",
        "https": https_url, "http": http_url, "upload_ok": None,
        "upload_msg": "直播列表已刷新（频道未重建）", "avg": 0, "min": 0, "max": 0, "channels": [],
    }
    return result


def quick_refresh(log=print, with_live=True):
    """快速模式：只刷新自定义单仓（+ 轻量校验直播），不体检配置源。"""
    cloud = load_cloud()
    result = {"best": None, "backups": [], "all": [], "cloud": None, "live": None, "quick": True}
    cfg = None
    custom_path = os.path.join(HERE, "custom_config.json")

    # 直播轻量校验与站点延迟刷新并行
    live_verify = {}
    vthread = None
    if cloud and with_live:
        def _v():
            live_verify["v"] = verify_current_live(log)
        vthread = threading.Thread(target=_v, daemon=True)
        vthread.start()

    src_box = {}
    lists_box = {}
    t_src = threading.Thread(target=lambda: src_box.update(rows=probe_sources_quick(log)), daemon=True)
    t_src.start()
    t_lists = None
    if with_live:
        t_lists = threading.Thread(target=lambda: lists_box.update(rows=probe_live_lists(log)), daemon=True)
        t_lists.start()

    if os.path.exists(custom_path):
        try:
            cfg = json.load(open(custom_path, encoding="utf-8"))
            log("使用自定义单仓配置（%d 站）…" % len(cfg.get("sites") or []))
            cfg = refresh_custom_names(cfg, log)
            json.dump(cfg, open(custom_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            result["custom_sites"] = len(cfg.get("sites") or [])
        except Exception as e:
            log("自定义配置读取失败：%s" % repr(e)[:60])
            cfg = None

    t_src.join()
    if t_lists is not None:
        t_lists.join()
    src_rows = src_box.get("rows") or []
    result["backups"] = src_rows[:10]

    if vthread is not None:
        vthread.join()
    v = live_verify.get("v")
    live = None
    if cloud and with_live and v:
        if not v["healthy"]:
            log("  现有直播可用率偏低，开始全量重建…")
            live = _live_pipeline(log)
        else:
            live = {"count": v["count"], "m3u": "", "backups": LIVE_BACKUPS,
                    "https": v["https"], "http": v["http"], "upload_ok": None,
                    "upload_msg": v["upload_msg"], "avg": v["avg"], "min": 0, "max": 0,
                    "channels": v["channels"]}
    elif cloud:
        https_url, http_url = live_urls(cloud)
        st = cloud_status(cloud) or {}
        live = {"count": st.get("live_channels", 0) or 0, "m3u": "", "backups": LIVE_BACKUPS,
                "https": https_url, "http": http_url, "upload_ok": None,
                "upload_msg": "直播保持不动（未刷新）", "avg": 0, "min": 0, "max": 0, "channels": []}
    lst_rows = lists_box.get("rows") or []
    if live:
        live["backups"] = [(r["name"], r["url"]) for r in lst_rows] or live.get("backups") or LIVE_BACKUPS
        live["fastest_backup"] = lst_rows[0]["url"] if lst_rows else ""
    result["live"] = live
    if cloud and cfg:
        live_url = (live or {}).get("http") or ""
        if live_url:
            cfg["lives"] = [{"name": "精选直播", "type": 0, "url": live_url}]
        try:
            log("正在发布自定义单仓（%d 站）…" % len(cfg.get("sites") or []))
            ok, msg = publish_config(cfg, cloud)
            url = cloud.get("config_url") or cloud["upload_url"].rsplit("/upload", 1)[0] + "/tvbox.json"
            result["cloud"] = {"ok": ok, "msg": msg, "url": url, "time": time.strftime("%H:%M:%S")}
            log(("  ✓ " if ok else "  ✗ ") + msg)
        except Exception as e:
            result["cloud"] = {"ok": False, "msg": repr(e)[:120], "url": "", "time": time.strftime("%H:%M:%S")}
            log("  ✗ 发布异常 " + repr(e)[:120])
    return result


def run_check(log=print, with_live=True):
    cloud = load_cloud()
    live_box = {}
    live_thread = None
    if cloud and with_live:
        def _bg():
            live_box["live"] = _live_pipeline(log)
        live_thread = threading.Thread(target=_bg, daemon=True)
        live_thread.start()
        log("直播源处理已并行启动")
    srcs = load_sources()
    log("正在体检 %d 个配置源..." % len(srcs))
    records = []
    # 用 as_completed：哪个先检测完就先显示，慢的源不会挡住其他源的进度
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(evaluate, s): s for s in srcs}
        for f in as_completed(futs):
            src = futs[f]
            try:
                rec = f.result()
            except Exception as e:
                rec = {"name": src.get("name", "?"), "src": src.get("url", ""), "parsed": False,
                       "err": "异常 " + repr(e)[:60], "ms": 0}
            records.append(rec)
            slow = " ⚠慢" if (rec.get("ms") or 0) > 6000 else ""
            if rec.get("parsed"):
                amount = ("%d入口" % rec["entries"]) if rec["kind"] == "多仓" else ("%d站" % rec.get("sites", 0))
                log("  ✓ %-14s %-4s %-8s %sms%s" % (rec["name"], rec["kind"], amount, rec["ms"], slow))
            else:
                log("  ✗ %-14s %sms %s%s" % (rec["name"], rec.get("ms", 0), rec.get("err", ""), slow))

    # 先按分数排出单仓，直播源只从最靠前的几个里收集，省时间
    singles = [r for r in records if r.get("kind") == "单仓" and r.get("parsed")]
    singles.sort(key=lambda r: -(r.get("score") or -999))

    live_jobs = []
    skip_live = re.compile(r"一起看|轮播|B站|斗鱼|虎牙|音乐")
    for rec in singles[:5]:
        rec["live_list"] = []
        for lv in (rec.get("lives_raw") or [])[:6]:
            if isinstance(lv, dict) and skip_live.search(str(lv.get("name") or "")):
                continue
            live_jobs.append((rec, lv))
    if live_jobs:
        log("正在体检 %d 条直播源..." % len(live_jobs))
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [(rec, ex.submit(check_live, rec.get("src", rec["out_url"]), lv)) for rec, lv in live_jobs]
            for rec, f in futs:
                try:
                    item = f.result()
                except Exception:
                    item = None
                if item:
                    rec["live_list"].append(item)
                    if item.get("ok"):
                        rec["live_ok"] = True
        # 有直播的配置补一点分
        for rec in singles:
            if rec.get("live_ok"):
                rec["score"] = round((rec.get("score") or 0) + 5, 1)
        singles.sort(key=lambda r: -(r.get("score") or -999))

    best_live, other_lives = pick_live(records)

    best = singles[0] if singles else None
    backups = singles[1:15]
    if not best:
        multis = [r for r in records if r.get("kind") == "多仓"]
        multis.sort(key=lambda r: -(r.get("score") or -999))
        if multis:
            best = multis[0]
            backups = multis[1:6]

    result = {"best": best, "backups": backups, "all": records, "cloud": None, "live": None}
    live = None

    # 直播源：与配置体检并行，最后汇合
    if live_thread is not None:
        live_thread.join()
    if not cloud:
        result["live"] = {"count": 0, "m3u": "", "backups": LIVE_BACKUPS, "https": "", "http": "",
                          "upload_ok": None, "upload_msg": "未配置云端，仅显示备用直播源",
                          "avg": 0, "min": 0, "max": 0, "channels": []}
    elif with_live and live_box.get("live") is not None:
        result["live"] = live_box["live"]
    elif with_live:
        result["live"] = {"count": 0, "m3u": "", "backups": LIVE_BACKUPS, "https": "", "http": "",
                          "upload_ok": False, "upload_msg": "直播处理失败", "avg": 0, "min": 0, "max": 0, "channels": []}
    else:
        https_url, http_url = live_urls(cloud)
        live = {"count": 0, "m3u": "", "backups": LIVE_BACKUPS, "https": https_url, "http": http_url,
                "upload_ok": None, "upload_msg": "沿用云端现有直播（未检测全部）",
                "avg": 0, "min": 0, "max": 0, "channels": []}
        st = cloud_status(cloud)
        if st:
            live["count"] = st.get("live_channels", 0) or 0
            live["upload_msg"] = "沿用云端现有直播（%d 频道）" % live["count"]
        result["live"] = live

    # 2) 配置：优先用自定义单仓（custom_config.json），否则用自动挑选的最优配置
    custom_path = os.path.join(HERE, "custom_config.json")
    cfgj = None
    if cloud and os.path.exists(custom_path):
        try:
            cfgj = json.load(open(custom_path, encoding="utf-8"))
            log("使用自定义单仓配置（%d 站）…" % len(cfgj.get("sites") or []))
            try:
                cfgj = refresh_custom_names(cfgj, log)
                json.dump(cfgj, open(custom_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            except Exception as e:
                log("  数据源延迟刷新失败：%s" % repr(e)[:60])
        except Exception as e:
            log("自定义配置读取失败：%s" % repr(e)[:60])
            cfgj = None
    if cfgj is None and cloud and best and best.get("kind") == "单仓":
        cfgj = build_publish_config(best, best_live, (live or {}).get("http") or "")

    if cloud and cfgj:
        try:
            live_url = (result.get("live") or {}).get("http") or ""
            if live_url:
                cfgj["lives"] = [{"name": "精选直播", "type": 0, "url": live_url}]
            log("正在发布精选配置（已合并直播）…")
            ok, msg = publish_config(cfgj, cloud)
            url = cloud.get("config_url") or cloud["upload_url"].rsplit("/upload", 1)[0] + "/tvbox.json"
            if ok:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        j = json.loads(r.read().decode("utf-8-sig"))
                    got, want = len(j.get("sites") or []), len(cfgj.get("sites") or [])
                    lives_got = len(j.get("lives") or [])
                    if got == want and lives_got >= 1:
                        log("  ✓ 云端回读校验通过（%d 站 / %d 直播）" % (got, lives_got))
                    else:
                        log("  ⚠ 云端回读不一致：站点 %d≠%d，直播 %d" % (got, want, lives_got))
                        ok, msg = False, "回读不一致"
                except Exception as e:
                    log("  ⚠ 云端回读失败：%s" % repr(e)[:80])
                    ok, msg = False, "回读失败：%s" % repr(e)[:60]
            result["cloud"] = {"ok": ok, "msg": msg, "url": url, "time": time.strftime("%H:%M:%S")}
            log(("  ✓ " if ok else "  ✗ ") + msg)
        except Exception as e:
            result["cloud"] = {"ok": False, "msg": repr(e)[:120], "url": "", "time": time.strftime("%H:%M:%S")}
            log("  ✗ 发布异常 " + repr(e)[:120])

    return result


def format_report(result):
    b = result["best"]
    lines = []
    cloud = result.get("cloud")
    if cloud and cloud.get("ok"):
        lines.append("固定地址: %s" % cloud["url"])
        lines.append("  %s（%s）—— 电视里填这个，以后不用再改" % (cloud["msg"], cloud["time"]))
    elif cloud:
        lines.append("固定地址: 发布失败（%s）" % cloud["msg"])
    if b:
        lines.append("源站直链: %s" % b["out_url"])
        lines.append("  来源=%s  %s  %sms  %s" % (b["name"], b["verdict"], b["ms"], b.get("rule", "")))
    elif result.get("quick"):
        lines.append("自定义单仓: 已发布 %d 个站点" % (result.get("custom_sites") or 0))
    else:
        lines.append("配置地址: 没有可用的")
    lv = result.get("live") or {}
    if lv.get("count"):
        lines.append("精选直播: %s" % (lv.get("https") or "(未配置云端)"))
        lines.append("  老安卓用: %s" % (lv.get("http") or ""))
        lines.append("  %d 个频道（央视 %d / 卫视 %d），平均 %dms" % (
            lv["count"], sum(1 for c in lv["channels"] if c[0] == "央视"),
            sum(1 for c in lv["channels"] if c[0] == "卫视"), lv.get("avg", 0)))
        if lv.get("upload_msg"):
            lines.append("  %s" % lv["upload_msg"])
    lines.append("")
    lines.append("备选配置：")
    for r in result["backups"]:
        amount = ("%d入口" % r["entries"]) if r["kind"] == "多仓" else ("%d站" % r.get("sites", 0))
        lines.append("  %-14s %-4s %-8s %sms  %s" % (r["name"], r["kind"], amount, r["ms"], r["verdict"]))
    return "\n".join(lines)

# ---------------------------------------------------------------- GUI

def main_gui():
    import tkinter as tk
    from tkinter import ttk

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title("TVBox 地址工具 v%s" % VERSION)
    root.geometry("1180x800")
    root.minsize(1020, 700)
    ico = asset(os.path.join("assets", "tvbox.ico"))
    if ico:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass
    style.configure("URL.TEntry", font=("Consolas", 10))
    style.configure("Big.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    q = queue.Queue()
    state = {"running": False, "start": 0}
    http_mode = tk.BooleanVar(value=False)

    def tooltip(widget, text):
        holder = {"w": None}

        def show(_e):
            if holder["w"] is not None:
                return
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            try:
                w.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(w, text=text, justify="left", background="#ffffe1", relief="solid",
                     borderwidth=1, font=("Microsoft YaHei UI", 9), padx=8, pady=6,
                     wraplength=360).pack()
            w.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 16, widget.winfo_rooty() + widget.winfo_height() + 6))
            holder["w"] = w

        def hide(_e):
            if holder["w"] is not None:
                holder["w"].destroy()
                holder["w"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def set_status(text, color="#c60"):
        status.config(text=str(text)[:80], foreground=color)

    # ---- 顶部 ----
    head = ttk.Frame(root, padding=(14, 12, 14, 2))
    head.pack(fill="x")
    ttk.Label(head, text="TVBox 地址工具", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
    ttk.Label(head, text="v" + VERSION, foreground="#888", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(6, 0))
    def ask_owner_password():
        """站长密码校验：首次使用要求设置；之后只校验哈希。"""
        try:
            import owner_auth
        except Exception:
            return True
        if owner_auth.is_verified():
            return True
        from tkinter import messagebox
        dlg = tk.Toplevel(root)
        dlg.title("站长密码")
        dlg.resizable(False, False)
        dlg.transient(root)
        try:
            dlg.grab_set()
        except Exception:
            pass
        dlg.update_idletasks()
        w, h = 400, 200
        try:
            x = root.winfo_rootx() + (root.winfo_width() - w) // 2
            y = root.winfo_rooty() + (root.winfo_height() - h) // 3
            dlg.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            dlg.geometry("%dx%d" % (w, h))
        first = not owner_auth.exists()
        ttk.Label(dlg, text=("首次使用，请设置站长密码" if first else "请输入站长密码"),
                  font=("Microsoft YaHei UI", 12, "bold")).pack(pady=(18, 6))
        ttk.Label(dlg, text="（密码只保存哈希，不会写入配置文件或备份）", foreground="#888",
                  font=("Microsoft YaHei UI", 8)).pack()
        var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=var, show="*", width=26, font=("Microsoft YaHei UI", 12))
        ent.pack(pady=10)
        ent.focus_set()
        err = ttk.Label(dlg, text="", foreground="#c00", font=("Microsoft YaHei UI", 9))
        err.pack()
        state = {"ok": False}

        def submit(event=None):
            pwd = var.get()
            if not pwd:
                err.config(text="密码不能为空")
                return
            if first:
                if len(pwd) < 4:
                    err.config(text="密码至少 4 位")
                    return
                owner_auth.set_password(pwd)
                state["ok"] = True
                dlg.destroy()
                return
            if owner_auth.verify(pwd):
                state["ok"] = True
                dlg.destroy()
            else:
                err.config(text="密码错误")
                var.set("")

        bf = ttk.Frame(dlg)
        bf.pack(pady=4)
        ttk.Button(bf, text="确定", command=submit).pack(side="left", padx=6)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        dlg.bind("<Return>", submit)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        try:
            root.wait_window(dlg)
        except Exception:
            pass
        return state["ok"]

    def open_patrol_win():
        try:
            if not ask_owner_password():
                return
            from patrol import open_patrol as _open
            _open(root)
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("站长工具", "打开失败：%s" % repr(e)[:200])

    def open_source_finder_win():
        try:
            from source_finder import open_source_finder as _open
            _open(root)
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("源接口工具", "打开失败：%s" % repr(e)[:200])

    src_tip = ttk.Label(head, text="?", foreground="#1a73e8", cursor="question_arrow",
                        font=("Microsoft YaHei UI", 10, "bold"))
    src_tip.pack(side="right", padx=(0, 4))
    tooltip(src_tip, "当所有地址都失效、不能使用时，点击「源接口工具」可以获得新的接口。")
    btn_src = ttk.Button(head, text="源接口工具", style="Big.TButton", command=open_source_finder_win)
    btn_src.pack(side="right", padx=(0, 8))
    btn_check = ttk.Button(head, text="检测全部", style="Big.TButton")
    btn_check.pack(side="right", padx=(8, 0))
    btn_live = ttk.Button(head, text="只刷新直播", style="Big.TButton")
    btn_live.pack(side="right", padx=(8, 0))
    btn_cfg = ttk.Button(head, text="只刷新配置", style="Big.TButton")
    btn_cfg.pack(side="right")

    # ---- 状态条（右侧放全局 http 开关）----
    sbar = ttk.Frame(root, padding=(14, 4, 14, 8))
    sbar.pack(fill="x")
    prog = ttk.Progressbar(sbar, mode="indeterminate", length=170)
    prog.pack(side="left", padx=(0, 12))
    status = ttk.Label(sbar, text="就绪 · 请点击右上角按钮开始检测", font=("Microsoft YaHei UI", 14, "bold"), foreground="#0a7")
    status.pack(side="left")

    HTTP_TIP = ("勾选后，左右两栏所有地址都会切换成 http:// 版本。\n\n"
                "老安卓电视（4.4 及更早）不认新版 HTTPS 证书，用 https 会拉取失败，\n"
                "必须用 http；安卓 9 及以上两种都可以，用 https 更安全。\n\n"
                "只需要设置一次，配置地址、直播地址会一起切换。")
    qm = ttk.Label(sbar, text="?", foreground="#1a73e8", cursor="question_arrow",
                   font=("Microsoft YaHei UI", 10, "bold"))
    qm.pack(side="right", padx=(4, 0))
    tooltip(qm, HTTP_TIP)
    http_cb = ttk.Checkbutton(sbar, text="复制 http 地址（老安卓电视勾选）", variable=http_mode,
                              style="Big.TCheckbutton")
    http_cb.pack(side="right", padx=(10, 2))

    # ---- 底部日志（先 pack）----
    bot = ttk.Frame(root, padding=(10, 0, 10, 10))
    bot.pack(fill="x", side="bottom")
    logf = ttk.Frame(bot)
    logbtn = ttk.Button(bot, text="▸ 检测日志")
    btn_merge = ttk.Button(bot, text="站长工具", command=open_patrol_win)
    btn_merge.pack(side="left", padx=(10, 0))
    mq = ttk.Label(bot, text="?", foreground="#1a73e8", cursor="question_arrow",
                   font=("Microsoft YaHei UI", 10, "bold"))
    mq.pack(side="left", padx=(4, 0))
    tooltip(mq, "站长专用：一键巡检站点和直播（含合并站点入口）。\n普通用户请忽略（点错也不会影响使用，分享版不可用）。")
    if IS_SHARE:
        btn_merge.config(state="disabled")
    side = ttk.Frame(bot)
    side.pack(side="right", anchor="e")
    ttk.Button(side, text="联系我", command=lambda: show_contact()).pack(side="left", padx=(4, 6))
    ttk.Button(side, text="支持我", command=lambda: show_support()).pack(side="left")
    logbtn.pack(anchor="w")
    logtxt = tk.Text(logf, height=8, font=("Consolas", 9), wrap="word")
    logtxt.pack(fill="both", expand=True)
    logtxt.configure(state="disabled")

    def toggle_log():
        if logf.winfo_ismapped():
            logf.pack_forget()
            logbtn.config(text="▸ 检测日志")
        else:
            logf.pack(fill="both", expand=True, pady=(4, 0))
            logbtn.config(text="▾ 检测日志")

    logbtn.config(command=toggle_log)

    def append_log(msg):
        logtxt.configure(state="normal")
        logtxt.insert("end", str(msg) + "\n")
        logtxt.see("end")
        logtxt.configure(state="disabled")

    def _copy_val(val, label):
        root.clipboard_clear()
        root.clipboard_append(val)
        set_status("已复制%s：%s" % (label, val), "#0a7")

    def _center_popup(w):
        w.update_idletasks()
        rw = max(w.winfo_reqwidth(), 320)
        rh = w.winfo_reqheight()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - rw) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - rh) // 2)
        w.geometry("%dx%d+%d+%d" % (rw, rh, x, y))
        try:
            w.transient(root)
        except Exception:
            pass
        w.deiconify()
        w.lift()
        try:
            w.grab_set()
        except Exception:
            pass
        w.focus_force()

    def show_contact():
        w = tk.Toplevel(root)
        w.title("联系我")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="联系方式", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(0, 10))
        for label, val in (("邮箱", "your@email.com"), ("微信", "your_wechat")):
            r = ttk.Frame(f)
            r.pack(fill="x", pady=6)
            ttk.Label(r, text=label + "：", width=6).pack(side="left")
            ttk.Label(r, text=val, font=("Consolas", 11, "bold"), foreground="#1a3d6d").pack(side="left")
            ttk.Button(r, text="复制", width=5,
                       command=lambda v=val, l=label: _copy_val(v, l)).pack(side="left", padx=(10, 0))
        ttk.Button(f, text="关闭", command=w.destroy).pack(anchor="e", pady=(14, 0))
        _center_popup(w)

    def show_support():
        w = tk.Toplevel(root)
        w.title("支持我")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=18)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="感谢使用！", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(f, text=("如果这个小工具帮助到了您，您可以扫码支持一下，\n"
                           "无论一分还是 1 块，都是对我 Token 的回报（/dog），非常感谢！"),
                  wraplength=320, justify="left").pack(anchor="w", pady=(8, 12))
        img_path = asset("zhichi.png")
        if img_path:
            try:
                img = tk.PhotoImage(file=img_path)
                factor = max(1, int(max(img.width(), img.height()) / 280))
                if factor > 1:
                    img = img.subsample(factor, factor)
                lab = ttk.Label(f, image=img)
                lab.image = img
                lab.pack()
            except Exception as e:
                ttk.Label(f, text="（二维码加载失败：%s）" % repr(e)[:60]).pack()
        else:
            ttk.Label(f, text="（未找到 zhichi.png）").pack()
        ttk.Button(f, text="关闭", command=w.destroy).pack(anchor="e", pady=(12, 0))
        _center_popup(w)

    # ---- 分栏 ----
    paned = ttk.PanedWindow(root, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=10, pady=(0, 4))
    left = ttk.Frame(paned)
    right = ttk.Frame(paned)
    paned.add(left, weight=3)
    paned.add(right, weight=2)

    def set_url(st, https_url):
        st["https_url"] = https_url or ""
        st["http_url"] = re.sub(r"^https://", "http://", st["https_url"], flags=re.I)
        st["var"].set(st["http_url"] if http_mode.get() else st["https_url"])

    def apply_http(*_a):
        for st in (cfg, bak, lv, zbak):
            st["var"].set(st["http_url"] if http_mode.get() else st["https_url"])

    def do_copy(st, label):
        val = st["var"].get().strip()
        if not val:
            return
        root.clipboard_clear()
        root.clipboard_append(val)
        set_status("已复制%s：%s" % (label, val), "#0a7")

    def make_url_row(parent, st, tag):
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=st["var"], style="URL.TEntry").pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="复制", width=6, command=lambda: do_copy(st, tag)).pack(side="left", padx=(6, 0))

    def make_tree(parent, cols, widths, stretch_col, height=5):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tv = ttk.Treeview(wrap, columns=cols, show="headings", height=height)
        for c, (title, w) in zip(cols, widths):
            tv.heading(c, text=title)
            tv.column(c, width=w, anchor="w", stretch=(c == stretch_col))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    def bind_copy(tree, idx):
        def handler(e):
            item = tree.identify_row(e.y)
            if not item:
                return
            vals = tree.item(item, "values")
            if len(vals) > idx:
                root.clipboard_clear()
                root.clipboard_append(str(vals[idx]))
                set_status("已复制：%s" % str(vals[idx])[:70], "#0a7")

        tree.bind("<Double-1>", handler)

    def detail_card(parent, title, tip):
        f = ttk.LabelFrame(parent, text=title, padding=8)
        f.pack(fill="x", padx=4, pady=(8, 0))
        lab = ttk.Label(f, text="—", justify="left", foreground="#555",
                        font=("Microsoft YaHei UI", 9), wraplength=380)
        lab.pack(anchor="w")
        ttk.Label(f, text=tip, foreground="#1a73e8", font=("Microsoft YaHei UI", 8),
                  wraplength=380, justify="left").pack(anchor="w", pady=(4, 0))
        return f, lab

    # ---- 左栏 ----
    cfg = {"var": tk.StringVar(), "https_url": "", "http_url": ""}
    f1 = ttk.LabelFrame(left, text="① 精选配置地址（已合并直播，填进 TVBox 的「配置 / 接口」栏）", padding=8)
    f1.pack(fill="x", padx=4, pady=(6, 0))
    make_url_row(f1, cfg, "配置地址")
    cfg_info = ttk.Label(f1, text="", foreground="#0a7", font=("Microsoft YaHei UI", 9))
    cfg_info.pack(anchor="w", pady=(4, 0))

    bak = {"var": tk.StringVar(), "https_url": "", "http_url": ""}
    f2 = ttk.LabelFrame(left, text="② 备用：源站直链", padding=8)
    f2.pack(fill="x", padx=4, pady=(8, 0))
    make_url_row(f2, bak, "源站直链")

    _, cfg_detail = detail_card(left, "③ 配置详情",
                                "提示：电视上只需填上面①一个地址，直播会随配置一起加载。")

    f4 = ttk.LabelFrame(left, text="", padding=6)
    f4.pack(fill="both", expand=True, padx=4, pady=(8, 0))
    f4head = ttk.Frame(f4)
    ttk.Label(f4head, text="④ 备选配置源（双击一行复制地址）").pack(side="left")
    q4 = ttk.Label(f4head, text="?", foreground="#1a73e8", cursor="question_arrow",
                   font=("Microsoft YaHei UI", 10, "bold"))
    q4.pack(side="left", padx=(4, 0))
    f4.configure(labelwidget=f4head)
    tooltip(q4, "这里的延迟只代表\"打开接口/加载首页\"的速度，不代表看电视时会不会卡顿。\n"
                "卡不卡还取决于播放线路的码率、你家网络、线路本身质量：延迟低的站也可能卡，延迟高的站也可能很流畅。")
    tv_cfg = make_tree(f4, ("name", "kind", "sites", "ms", "url", "verdict"),
                       [("来源", 90), ("类型", 46), ("站点", 46), ("延迟", 54), ("地址", 250), ("结论", 140)],
                       "url", height=10)
    bind_copy(tv_cfg, 4)

    # ---- 右栏 ----
    lv = {"var": tk.StringVar(), "https_url": "", "http_url": ""}
    g1 = ttk.LabelFrame(right, text="① 精选直播地址（独立地址，备用）", padding=8)
    g1.pack(fill="x", padx=4, pady=(6, 0))
    make_url_row(g1, lv, "直播地址")
    lv_info = ttk.Label(g1, text="", foreground="#0a7", font=("Microsoft YaHei UI", 9),
                        wraplength=380, justify="left")
    lv_info.pack(anchor="w", pady=(4, 0))

    zbak = {"var": tk.StringVar(), "https_url": "", "http_url": ""}
    g2 = ttk.LabelFrame(right, text="② 备用：直播直链", padding=8)
    g2.pack(fill="x", padx=4, pady=(8, 0))
    make_url_row(g2, zbak, "备用直播直链")

    _, live_detail = detail_card(right, "③ 直播详情",
                                 "提示：直播已合并进配置；只有不支持内置直播的壳，才需要单独填①。")

    g4 = ttk.LabelFrame(right, text="④ 备用直播源（双击一行复制地址）", padding=6)
    g4.pack(fill="both", expand=True, padx=4, pady=(8, 0))
    tv_live = make_tree(g4, ("name", "url"), [("名称", 120), ("地址", 300)], "url", height=8)
    bind_copy(tv_live, 1)

    http_mode.trace_add("write", apply_http)

    # ---- 运行 ----
    def clear_all(mode="all"):
        if mode in ("all", "config"):
            for i in tv_cfg.get_children():
                tv_cfg.delete(i)
            for k in (cfg, bak):
                k["https_url"] = ""
                k["http_url"] = ""
                k["var"].set("")
            cfg_info.config(text="")
            cfg_detail.config(text="—")
        if mode in ("all", "live"):
            for i in tv_live.get_children():
                tv_live.delete(i)
            lv["https_url"] = ""
            lv["http_url"] = ""
            lv["var"].set("")
            lv_info.config(text="")
            live_detail.config(text="—")

    def finish():
        state["running"] = False
        prog.stop()
        btn_check.config(state="normal")
        btn_live.config(state="normal")
        btn_cfg.config(state="normal")

    def start(mode):
        if state["running"]:
            return
        state["running"] = True
        state["start"] = time.time()
        for b in (btn_check, btn_live, btn_cfg):
            b.config(state="disabled")
        clear_all(mode)
        prog.start(12)
        if mode == "live":
            set_status("⏳ 正在全量重建直播，请稍候（约 20 秒）…", "#1a73e8")
        elif mode == "config":
            set_status("⏳ 正在刷新自定义单仓，请稍候（约 10 秒）…", "#1a73e8")
        elif mode == "quick":
            set_status("⏳ 正在快速检查（自定义单仓 + 直播存活），约 10~15 秒…", "#1a73e8")
        else:
            set_status("⏳ 正在全量检测配置源 + 直播，请稍候（约 30 秒）…", "#1a73e8")
        threading.Thread(target=worker, args=(mode,), daemon=True).start()

    def worker(mode):
        try:
            if mode == "live":
                q.put(("done_live", refresh_live_only(lambda m: q.put(("log", m)))))
            elif mode == "quick":
                if load_cloud() is None or not os.path.exists(os.path.join(HERE, "custom_config.json")):
                    q.put(("done", run_check(log=lambda m: q.put(("log", m)))))
                else:
                    q.put(("done_quick", quick_refresh(lambda m: q.put(("log", m)), with_live=True)))
            elif mode == "config":
                if load_cloud() is None or not os.path.exists(os.path.join(HERE, "custom_config.json")):
                    q.put(("done", run_check(log=lambda m: q.put(("log", m)), with_live=False)))
                else:
                    q.put(("done_cfg", quick_refresh(lambda m: q.put(("log", m)), with_live=False)))
            else:
                q.put(("done", run_check(log=lambda m: q.put(("log", m)))))
        except Exception as e:
            q.put(("err", repr(e)))

    def render_live(live):
        if not live:
            return
        if live.get("https"):
            set_url(lv, live["https"])
        if live.get("fastest_backup"):
            set_url(zbak, live["fastest_backup"])
        elif live.get("backups"):
            set_url(zbak, live["backups"][0][1])
        parts = []
        n_cctv = n_ws = 0
        if live.get("count"):
            chans = live.get("channels") or []
            if chans:
                n_cctv = sum(1 for c in chans if c[0] == "央视")
                n_ws = sum(1 for c in chans if c[0] == "卫视")
                parts.append("%d 频道（央视 %d / 卫视 %d）· 平均 %dms" % (
                    live["count"], n_cctv, n_ws, live.get("avg", 0)))
            else:
                parts.append("%d 频道" % live["count"])
        if live.get("upload_msg"):
            parts.append(live["upload_msg"])
        if parts:
            lv_info.config(text=" · ".join(parts))
        dl = []
        if live.get("count"):
            dl.append("频道：%d 个（央视 %d / 卫视 %d）" % (live["count"], n_cctv, n_ws))
            dl.append("平均延迟：%dms" % live.get("avg", 0))
        if live.get("upload_msg"):
            dl.append(live["upload_msg"])
        if dl:
            live_detail.config(text="\n".join(dl))
        for name, url in (live.get("backups") or []):
            tv_live.insert("", "end", values=(name, url))

    def render_config(result):
        cloud = result.get("cloud") or {}
        b = result.get("best")
        if result.get("quick") or (result.get("backups") and result.get("custom_sites")):
            bs = result.get("backups") or []
            if cloud.get("ok"):
                set_url(cfg, cloud["url"])
                cfg_info.config(text="自定义单仓 · %s · %s" % (cloud.get("msg", ""), cloud.get("time", "")))
            if bs:
                set_url(bak, bs[0].get("out_url", ""))
            cfg_detail.config(text="自定义单仓：%s 个站点\n最快配置源：%s" % (
                result.get("custom_sites") or 0,
                ("%s %dms" % (bs[0].get("name"), bs[0].get("ms", 0))) if bs else "—"))
            for r in bs[:10]:
                tv_cfg.insert("", "end", values=(r.get("name", ""), r.get("kind", ""), r.get("sites", 0),
                                                 "%dms" % r.get("ms", 0), r.get("out_url", ""), r.get("verdict", "")))
            append_log("")
            append_log(format_report(result))
            return
        if cloud.get("ok"):
            set_url(cfg, cloud["url"])
            cfg_info.config(text="精选配置 · %s · %s" % (cloud.get("msg", ""), cloud.get("time", "")))
        elif b:
            set_url(cfg, b.get("out_url", ""))
            extra = "· 站点 %s · %dms" % (b.get("sites", "-"), b.get("ms", 0))
            if cloud:
                extra = "⚠ 发布失败（%s）" % cloud.get("msg", "") + " " + extra
            cfg_info.config(text="%s %s · %s" % (b.get("name", ""), extra, b.get("verdict", "")))
        if b:
            set_url(bak, b.get("out_url", ""))
            d = ["来源：%s" % b.get("name", ""),
                 "站点：%s 个 · 解析：%s 个" % (b.get("sites", "-"), b.get("parses", "-")),
                 "延迟：%dms · 规则：%s" % (b.get("ms", 0), b.get("rule", ""))]
            if cloud.get("time"):
                d.append("发布：%s" % cloud["time"])
            cfg_detail.config(text="\n".join(d))
        if b is None and result.get("custom_sites"):
            cfg_detail.config(text="自定义单仓：%d 个站点\n发布：%s" % (
                result["custom_sites"], (result.get("cloud") or {}).get("time", "")))
        for r in result.get("backups") or []:
            amount = r.get("entries", 0) if r.get("kind") == "多仓" else r.get("sites", 0)
            tv_cfg.insert("", "end", values=(r.get("name", ""), r.get("kind", ""), amount,
                                             "%dms" % r.get("ms", 0), r.get("out_url", ""), r.get("verdict", "")))
        append_log("")
        append_log(format_report(result))

    def render(result):
        render_config(result)
        render_live(result.get("live"))

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    append_log(payload)
                    if state["running"]:
                        set_status("⏳ " + str(payload), "#1a73e8")
                elif kind == "err":
                    append_log("出错：" + str(payload))
                    set_status("❌ 出错：" + str(payload)[:60], "#c00")
                    finish()
                elif kind == "done":
                    render(payload)
                    finish()
                    used = int(time.time() - state["start"]) if state["start"] else 0
                    set_status("✅ 完成 · 用时 %d 秒 · 双击备选行可复制地址" % used, "#0a7")
                elif kind == "done_quick":
                    render(payload)
                    finish()
                    used = int(time.time() - state["start"]) if state["start"] else 0
                    set_status("✅ 快速检查完成 · 用时 %d 秒" % used, "#0a7")
                elif kind == "done_cfg":
                    render_config(payload)
                    finish()
                    used = int(time.time() - state["start"]) if state["start"] else 0
                    set_status("✅ 自定义单仓已刷新 · 用时 %d 秒 · 直播保持不动" % used, "#0a7")
                elif kind == "done_live":
                    payload = payload or {}
                    live = payload.get("live") or payload
                    render_live(live)
                    finish()
                    set_status("✅ 直播已刷新 · %d 个频道 · 配置保持不动" % (live.get("count") or 0), "#0a7")
        except queue.Empty:
            pass
        root.after(120, poll)

    btn_check.config(command=lambda: start("all"))
    btn_live.config(command=lambda: start("live"))
    btn_cfg.config(command=lambda: start("config"))
    root.after(400, lambda: set_status("就绪 · 请点击右上角按钮开始检测", "#0a7"))
    root.after(150, poll)
    root.mainloop()


def main_cli():
    def log(m):
        print(m, flush=True)
    t0 = time.time()
    result = run_check(log=log)
    print("\n" + "=" * 60)
    print(format_report(result))
    print("=" * 60)
    print("用时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    if "--cli" in sys.argv:
        main_cli()
    else:
        main_gui()
