# -*- coding: utf-8 -*-
"""源接口工具（测试版）：从配置的入口页采集 TVBox 接口，初筛 + 多仓展开 + 去重排序。

独立窗口，不影响主工具；扫描在后台线程，结果默认只展示与导出，不自动改配置。
"""
import json
import os
import re
import threading
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import tvbox_finder as t

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "数据")
IDX_PATH = os.path.join(DATA, "source_index.json")
POOL_PATH = os.path.join(DATA, "candidates_pool.json")
DEAD_PATH = os.path.join(DATA, "dead_links.json")
DEAD_TTL_DAYS = 7


def load_dead():
    try:
        return json.load(open(DEAD_PATH, encoding="utf-8")).get("items", {})
    except Exception:
        return {}


def save_dead(items):
    try:
        os.makedirs(DATA, exist_ok=True)
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items},
                  open(DEAD_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def is_recent_dead(dead, url, ttl_days=DEAD_TTL_DAYS):
    rec = dead.get(url)
    if not rec:
        return False
    try:
        return (time.time() - float(rec.get("ts", 0))) < ttl_days * 86400
    except Exception:
        return False

URL_RE = re.compile(r"""https?://[^\s"'<>)\]}]+""")
BAD_EXT = re.compile(r"\.(png|jpe?g|gif|webp|ico|css|js|svg|woff2?|ttf|mp4|apk|zip|rar|7z|exe|jar|m3u8?|ts|html?)(\?|$)", re.I)
SKIP_HOST = re.compile(r"(github\.com|gitee\.com|gitlab\.com|jihulab\.com|t\.me|qq\.com|youtube\.com|bilibili\.com|zhihu\.com|csdn\.net|img\.|shields\.io|badge)", re.I)
PAN = re.compile(r"pan\.|alist|quark|115\.com|baidu|aliyundrive|emby|uc\.cn", re.I)


def clean(u):
    return u.rstrip('.,;:）)】]}')


def config_like(u):
    low = u.lower()
    if BAD_EXT.search(low) or SKIP_HOST.search(low):
        return False
    return any(k in low for k in (".json", ".txt", "api", "provide/vod", "tvbox", "/tv", "box"))


def load_index():
    try:
        path = IDX_PATH
        try:
            cand = t.asset(os.path.join("数据", "source_index.json"))
            if cand and os.path.exists(cand):
                path = cand
        except Exception:
            pass
        idx = json.load(open(path, encoding="utf-8"))
        return idx
    except Exception:
        return {"pages": [], "seeds": [], "expand_multiwarehouse": True, "max_candidates": 50}


def scan(log, stop_flag, min_sites=15, max_ms=2500, expand=True, max_pool=50, progress=None):
    """执行采集→初筛→展开→排序，返回 (good, stats)。log(msg) 可为 GUI 回调。"""
    idx = load_index()
    t0 = time.time()
    raw = {}
    n_entries = len(idx.get("pages", [])) + len(idx.get("seeds", []))
    if progress:
        progress("采集", 0, max(1, n_entries))
    for i, p in enumerate(idx.get("pages", [])):
        if stop_flag():
            return [], {"aborted": True}
        url = p["url"]
        if p.get("kind") == "github":
            url = "https://gh-proxy.com/" + url
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": t.UA}), timeout=25, context=t.CTX)
            txt = r.read(3_000_000).decode("utf-8", "replace")
        except Exception as e:
            log("采集失败 %-20s %s" % (p.get("name", "?"), repr(e)[:50]))
            continue
        n = 0
        for m in URL_RE.finditer(txt):
            u = clean(m.group(0))
            if not config_like(u):
                continue
            if u not in raw:
                raw[u] = p.get("name", "?")
                n += 1
        log("采集 %-22s 新增 %d 条" % (p.get("name", "?"), n))
        if progress:
            progress("采集", i + 1, max(1, n_entries))
    for s in idx.get("seeds", []):
        if s["url"] not in raw:
            raw[s["url"]] = "seed"
    dead = load_dead()
    skipped = [u for u in raw if is_recent_dead(dead, u)]
    for u in skipped:
        raw.pop(u, None)
    if skipped:
        log("跳过近期确认失效的地址 %d 个（%d 天内不再重测）" % (len(skipped), DEAD_TTL_DAYS))
    log("原始候选：%d 个" % len(raw))

    def probe(item):
        u, src = item
        t1 = time.time()
        try:
            r = t.fetch_url(u, timeout=8, limit=3_000_000, deadline=10)
            if not r["ok"]:
                return {"url": u, "src": src, "ok": False, "kind": "死链", "ms": int((time.time() - t1) * 1000)}
            txt = r["data"].decode("utf-8-sig", "replace")
            obj, rule, strict = t.lenient_load(txt)
            if isinstance(obj, dict) and obj.get("sites"):
                apis = set()
                for s in obj["sites"]:
                    a = str(s.get("api") or "")
                    if a.startswith("http"):
                        apis.add(urllib.parse.urlsplit(a).netloc)
                return {"url": u, "src": src, "ok": True, "kind": "单仓", "sites": len(obj["sites"]),
                        "spider": bool(obj.get("spider")), "strict": strict, "ms": int(r["ms"]),
                        "apis": sorted(apis)}
            if isinstance(obj, dict) and isinstance(obj.get("urls"), list):
                return {"url": u, "src": src, "ok": True, "kind": "多仓", "entries": len(obj["urls"]), "ms": int(r["ms"])}
            return {"url": u, "src": src, "ok": False, "kind": "非配置", "ms": int(r["ms"])}
        except Exception:
            return {"url": u, "src": src, "ok": False, "kind": "异常", "ms": 99999}

    t1 = time.time()
    items = list(raw.items())
    res = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for i, r in enumerate(ex.map(probe, items), 1):
            res.append(r)
            if progress:
                progress("初筛", i, max(1, len(items)))
            if i % 25 == 0:
                log("初筛进度 %d/%d" % (i, len(items)))
    log("初筛完成 %.1fs" % (time.time() - t1))
    dead = load_dead()
    now = time.time()
    added = 0
    for r in res:
        if r.get("kind") in ("死链", "非配置", "异常"):
            if r["url"] not in dead:
                added += 1
            dead[r["url"]] = {"ts": now, "kind": r.get("kind")}
        else:
            dead.pop(r["url"], None)
    save_dead(dead)
    log("失效地址已记入缓存：新增 %d 个（下次跳过）" % added)
    singles = [r for r in res if r.get("kind") == "单仓"]
    multis = [r for r in res if r.get("kind") == "多仓"]
    log("单仓 %d，多仓 %d，死链/非配置 %d" % (len(singles), len(multis), len(res) - len(singles) - len(multis)))

    if expand and multis:
        subs = []
        for mi, m in enumerate(multis):
            if stop_flag():
                break
            if progress:
                progress("展开", mi + 1, max(1, len(multis)) * 2)
            try:
                r = t.fetch_url(m["url"], timeout=8, limit=3_000_000, deadline=10)
                obj, rule, strict = t.lenient_load(r["data"].decode("utf-8-sig", "replace"))
                for e in (obj.get("urls") or [])[:40]:
                    if isinstance(e, dict) and str(e.get("url") or "").startswith("http"):
                        subs.append(e["url"])
            except Exception:
                pass
        subs = list(dict.fromkeys(subs))
        if subs:
            with ThreadPoolExecutor(max_workers=16) as ex:
                subres = list(ex.map(probe, [(u, "多仓展开") for u in subs]))
            more = [r for r in subres if r.get("kind") == "单仓"]
            if progress:
                progress("展开", max(1, len(multis)) * 2, max(1, len(multis)) * 2)
            log("多仓展开：%d 个子地址，新增单仓 %d" % (len(subs), len(more)))
            singles += more

    # 过滤 + 去重（同 API 域名集合视为同一家族，保留最快）
    good = []
    seen = {}
    for r in singles:
        if r.get("sites", 0) < min_sites or r.get("ms", 99999) > max_ms or not r.get("strict"):
            continue
        key = ",".join(r.get("apis") or []) or r["url"]
        prev = seen.get(key)
        if prev is None or r["ms"] < prev["ms"]:
            seen[key] = r
    good = sorted(seen.values(), key=lambda r: r["ms"])[:max_pool]
    stats = {"raw": len(raw), "singles": len(singles), "multis": len(multis), "good": len(good),
             "secs": round(time.time() - t0, 1)}
    if progress:
        progress("完成", 1, 1)
    log("可用单仓：%d 个（耗时 %.1fs）" % (len(good), stats["secs"]))
    return good, stats


def open_source_finder(root):
    import tkinter as tk
    from tkinter import ttk, messagebox

    win = tk.Toplevel(root)
    win.title("源接口工具（测试版）")
    win.minsize(900, 560)
    win.update_idletasks()
    ww, wh = 1040, 660
    try:
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        rw, rh = root.winfo_width(), root.winfo_height()
        x = rx + max(0, (rw - ww) // 2)
        y = ry + max(0, int((rh - wh) * 0.35))
        win.geometry("%dx%d+%d+%d" % (ww, wh, x, y))
    except Exception:
        win.geometry("%dx%d" % (ww, wh))
    try:
        win.transient(root)
    except Exception:
        pass

    q = []
    state = {"running": False, "stop": False, "t0": 0.0}

    STAGE = {"采集": (0, 8), "初筛": (8, 72), "展开": (80, 15), "完成": (100, 0)}

    def on_progress(stage, done, total):
        base, span = STAGE.get(stage, (0, 100))
        frac = 0.0 if not total else min(1.0, done / float(total))
        pct = int(base + span * frac)
        return pct, "%s %d%%（%d/%d）" % (stage, pct, done, total)

    def tip(widget, text):
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
            txt = text() if callable(text) else text
            tk.Label(w, text=txt, justify="left", background="#ffffe1", relief="solid",
                     borderwidth=1, font=("Microsoft YaHei UI", 9), padx=8, pady=6,
                     wraplength=380).pack()
            w.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 16, widget.winfo_rooty() + widget.winfo_height() + 6))
            holder["w"] = w

        def hide(_e):
            if holder["w"] is not None:
                holder["w"].destroy()
                holder["w"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    top = ttk.Frame(win, padding=(12, 10, 12, 4))
    top.pack(fill="x")
    ttk.Label(top, text="源接口工具", font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
    ttk.Label(top, text="采集 → 初筛 → 多仓展开 → 去重排序（默认只展示，不改配置）",
              foreground="#888", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=12)

    sbar = ttk.Frame(win, padding=(12, 2, 12, 6))
    sbar.pack(fill="x")
    prog = ttk.Progressbar(sbar, mode="determinate", maximum=100, length=170)
    prog.pack(side="left", padx=(0, 10))
    status = ttk.Label(sbar, text="就绪", font=("Microsoft YaHei UI", 11, "bold"), foreground="#0a7")
    status.pack(side="left")

    ctl = ttk.Frame(win, padding=(12, 0, 12, 6))
    ctl.pack(fill="x")
    idx0 = load_index()
    n_entries = len(idx0.get("pages") or []) + len(idx0.get("seeds") or [])
    max_pool = idx0.get("max_candidates") or 50
    btn_start = ttk.Button(ctl, text="开始扫描")
    btn_start.pack(side="left")
    q1 = ttk.Label(ctl, text="?", foreground="#1a73e8", cursor="question_arrow",
                   font=("Microsoft YaHei UI", 10, "bold"))
    q1.pack(side="left", padx=(4, 10))
    tip(q1, "我们将从 %d 个接口发布页扫描所有接口地址，并优选 %d 个可用单仓（按延迟从快到慢排序）。第一次扫描地址较多、速度较慢，请耐心等候。" % (n_entries, max_pool))
    btn_export = ttk.Button(ctl, text="导出候选池", state="disabled")
    btn_export.pack(side="left", padx=8)
    q2 = ttk.Label(ctl, text="?", foreground="#1a73e8", cursor="question_arrow",
                   font=("Microsoft YaHei UI", 10, "bold"))
    q2.pack(side="left")
    tip(q2, lambda: "点击导出会将下面所有优选的 %d 个地址放进备选地址源（candidates_pool.json）。\n导出后请重新刷新配置地址，得到最新最快的配置地址。" % (len(last.get("good") or []) or max_pool))
    ttk.Button(ctl, text="关闭", command=win.destroy).pack(side="right")

    logf = ttk.Frame(win, padding=(12, 0, 12, 4))
    logf.pack(fill="x", side="bottom")
    logbox = tk.Text(logf, height=6, font=("Consolas", 9), wrap="word")
    logbox.pack(fill="x")
    logbox.configure(state="disabled")

    wrap = ttk.Frame(win, padding=(12, 0, 12, 8))
    wrap.pack(fill="both", expand=True)
    cols = ("src", "kind", "sites", "ms", "url")
    tv = ttk.Treeview(wrap, columns=cols, show="headings", height=16)
    for c, (title, w) in zip(cols, [("来源", 118), ("类型", 56), ("站点", 56), ("延迟", 68), ("地址（双击复制）", 600)]):
        tv.heading(c, text=title)
        tv.column(c, width=w, anchor="w", stretch=(c == "url"))
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tv.bind("<Double-1>", lambda e: copy_one())

    def append_log(msg):
        logbox.configure(state="normal")
        logbox.insert("end", str(msg) + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    last = {"good": []}

    def copy_one():
        sel = tv.selection()
        if not sel:
            return
        vals = tv.item(sel[0], "values")
        if len(vals) >= 5:
            win.clipboard_clear()
            win.clipboard_append(str(vals[4]))
            status.config(text="已复制地址", foreground="#0a7")

    def export():
        good = last.get("good") or []
        if not good:
            return
        old = []
        try:
            old = json.load(open(POOL_PATH, encoding="utf-8")).get("items", [])
        except Exception:
            old = []
        by_url = {x.get("url"): x for x in old}
        for r in good:
            x = dict(r)
            x["found_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            by_url[x["url"]] = x
        items = list(by_url.values())[-50:]
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items},
                  open(POOL_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        status.config(text="已导出到 数据\\candidates_pool.json（%d 条）" % len(items), foreground="#0a7")

    def poll():
        while q:
            kind, payload = q.pop(0)
            if kind == "log":
                append_log(payload)
                status.config(text="⏳ " + str(payload)[:70], foreground="#1a73e8")
            elif kind == "prog":
                st, d, t = payload
                pct, txt = on_progress(st, d, t)
                try:
                    prog.config(value=pct)
                except Exception:
                    pass
                status.config(text="⏳ " + txt, foreground="#1a73e8")
            elif kind == "done":
                good, stats = payload
                last["good"] = good
                for r in good:
                    tv.insert("", "end", values=(r.get("src", ""), r.get("kind", ""), r.get("sites", ""),
                                                 "%dms" % r.get("ms", 0), r.get("url", "")))
                btn_start.config(state="normal")
                btn_export.config(state="normal" if good else "disabled")
                state["running"] = False
                try:
                    prog.config(value=100)
                except Exception:
                    pass
                status.config(text="✅ 扫描完成：%d 个可用（耗时 %ss）" % (len(good), stats.get("secs")), foreground="#0a7")
            elif kind == "err":
                append_log("出错：" + str(payload))
                btn_start.config(state="normal")
                state["running"] = False
                try:
                    prog.config(value=0)
                except Exception:
                    pass
                status.config(text="❌ 出错：" + str(payload)[:60], foreground="#c00")
        win.after(150, poll)

    def worker():
        good, stats = [], {}
        try:
            good, stats = scan(lambda m: q.append(("log", m)), lambda: state["stop"],
                               progress=lambda st, d, t: q.append(("prog", (st, d, t))))
        except Exception as e:
            q.append(("err", repr(e)[:120]))
            return
        q.append(("done", (good, stats)))

    def start():
        if state["running"]:
            return
        state["running"] = True
        state["stop"] = False
        for i in tv.get_children():
            tv.delete(i)
        btn_start.config(state="disabled")
        btn_export.config(state="disabled")
        state["t0"] = time.time()
        prog.config(mode="determinate", value=0)
        status.config(text="⏳ 正在扫描…", foreground="#1a73e8")
        threading.Thread(target=worker, daemon=True).start()

    btn_start.config(command=start)
    btn_export.config(command=export)
    win.after(150, poll)
    return win
