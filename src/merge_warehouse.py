# -*- coding: utf-8 -*-

# 你自己的域名（自建 Worker 的域名，例：tv.example.com）
LIVE_DOMAIN = "YOUR_DOMAIN"
"""合并源地址（站长专用）：从源接口工具的候选池中抽测站点，择优合并进当前单仓并发布。

门控：
- 只有个人版可用（tvbox_finder.IS_SHARE 为 False）；
- 需要 cloud.json（发布密钥）与 custom_config.json；
- 合并前自动备份配置。
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import tvbox_finder as t
import owner_auth

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "数据")
CUSTOM = os.path.join(HERE, "custom_config.json")
POOL = os.path.join(DATA, "candidates_pool.json")
BACKUP_DIR = os.path.join(os.path.dirname(HERE), "备份")

PAN = re.compile(r"pan\.|alist|quark|115\.com|baidu|aliyundrive|emby|uc\.cn", re.I)
SKIP = re.compile(r"直播|live|电视|轮播|斗鱼|虎牙|B站|bilibili|抖音|快手|音乐|片单|榜单|设置|文件|预告|盘搜|网盘|云盘|解说|短剧", re.I)
TARGET_CAP = 9
MAX_TEST = 40
KW = "庆余年"


def load_current():
    return json.load(open(CUSTOM, encoding="utf-8"))


def load_pool():
    try:
        return json.load(open(POOL, encoding="utf-8")).get("items", [])
    except Exception:
        return []


def net_of(api):
    try:
        return urllib.parse.urlsplit(api).netloc.lower()
    except Exception:
        return ""


def site_ok(s):
    api = str(s.get("api") or "")
    name = str(s.get("name") or "")
    if s.get("type") not in (0, 1) or not api.startswith("http"):
        return False
    if PAN.search(api) or PAN.search(name) or SKIP.search(name):
        return False
    return bool(net_of(api))


def extract_sites(cfg_url):
    """下载候选仓，抽出可用的直连站点。"""
    try:
        r = t.fetch_url(cfg_url, timeout=10, limit=3_000_000, deadline=12)
        if not r["ok"]:
            return []
        obj, rule, strict = t.lenient_load(r["data"].decode("utf-8-sig", "replace"))
        if not isinstance(obj, dict):
            return []
        return [dict(s) for s in (obj.get("sites") or []) if site_ok(s)]
    except Exception:
        return []


def _api_base(api, ac):
    if re.search(r"[?&]ac=", api):
        u = re.sub(r"([?&])ac=[^&]*", r"\1ac=" + ac, api)
    else:
        u = api + ("&" if "?" in api else "?") + "ac=" + ac
    return u


def probe_site(site, timeout=4, kw=KW):
    """搜索 + 详情 + 取第一条播放地址 + 首片验证，返回 (ok, ms, detail)。"""
    api = str(site.get("api") or "")
    if not api.startswith("http"):
        return False, 99999, "无 api"
    t0 = time.time()
    try:
        d = t._get(_api_base(api, "videolist") + "&wd=" + urllib.parse.quote(kw),
                   timeout=timeout, limit=300_000, deadline=timeout)
        txt = d.decode("utf-8", "replace")
    except Exception:
        return False, 99999, "搜索失败"
    item = None
    try:
        j = json.loads(txt)
        lst = j.get("list") or []
        item = lst[0] if lst else None
    except Exception:
        m = re.search(r"<id>(\d+)</id>", txt)
        if m:
            item = {"vod_id": m.group(1)}
    if not item:
        return False, 99999, "搜索无结果"
    vid = item.get("vod_id") or item.get("id")
    try:
        d = t._get(_api_base(api, "detail") + "&ids=" + urllib.parse.quote(str(vid)),
                   timeout=timeout, limit=300_000, deadline=timeout)
        txt = d.decode("utf-8", "replace")
    except Exception:
        return False, 99999, "详情失败"
    purl = ""
    try:
        j = json.loads(txt)
        lst = j.get("list") or []
        purl = str(lst[0].get("vod_play_url") or "") if lst else ""
    except Exception:
        m = re.search(r'<dd flag="[^"]*">\s*(\S+?)\s*</dd>', txt)
        purl = m.group(1) if m else ""
    first = ""
    for part in purl.split("#"):
        if "$" in part:
            first = part.split("$")[-1]
            break
    if not first.startswith("http"):
        return False, 99999, "播放串无效"
    try:
        if ".m3u8" in first.lower():
            pd = t._get(first, timeout=timeout, limit=200_000, deadline=timeout).decode("utf-8", "replace")
            if "#EXT-X-STREAM-INF" in pd:
                lines = [l.strip() for l in pd.splitlines() if l.strip() and not l.startswith("#")]
                if lines:
                    first = urllib.parse.urljoin(first, lines[0])
                    pd = t._get(first, timeout=timeout, limit=200_000, deadline=timeout).decode("utf-8", "replace")
            segs = [l.strip() for l in pd.splitlines() if l.strip() and not l.startswith("#")]
            if not segs:
                return False, 99999, "无分片"
            su = urllib.parse.urljoin(first, segs[0])
            sd = t._get(su, timeout=timeout, limit=4096, deadline=timeout)
            if not sd:
                return False, 99999, "首片为空"
        else:
            sd = t._get(first, timeout=timeout, limit=4096, deadline=timeout)
            if not sd:
                return False, 99999, "首包为空"
    except Exception:
        return False, 99999, "播放失败"
    return True, int((time.time() - t0) * 1000), "OK"


def merge_sites(current, tested, cap=TARGET_CAP):
    """现有 + 通过的候选，按 API 域去重并按实测延迟排序，保留前 cap 个。"""
    pool = {}
    for s, ok, ms in tested:
        if not ok:
            continue
        s = dict(s)
        s["_ms"] = ms
        key = net_of(s.get("api") or "")
        if key and (key not in pool or ms < pool[key]["_ms"]):
            pool[key] = s
    result = []
    cur_net = set()
    for s in current:
        s = dict(s)
        cur_net.add(net_of(s.get("api") or ""))
        if "_ms" not in s:
            s["_ms"] = 0  # 现有站点优先保留
        result.append(s)
    for k, s in pool.items():
        if k in cur_net:
            continue
        result.append(s)
    # 现有站点排前面（优先保留），再按延迟排序补位
    existing = [s for s in result if s["_ms"] == 0]
    others = sorted([s for s in result if s["_ms"] != 0], key=lambda x: x["_ms"])
    merged = existing[:cap]
    if len(merged) < cap:
        merged += others[: cap - len(merged)]
    for i, s in enumerate(merged):
        s.pop("_ms", None)
        if not s.get("key") or not str(s.get("key")).startswith("cms_"):
            s["key"] = "cms_%02d" % (i + 1)
    return merged


def apply_publish(merged, log=print):
    backup = os.path.join(BACKUP_DIR, "custom_config_合并前_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(backup, "w", encoding="utf-8") as f:
            f.write(open(CUSTOM, encoding="utf-8").read())
        log("已备份原配置: " + backup)
    except Exception as e:
        log("备份失败（继续）: %s" % repr(e)[:60])
    cfg = load_current()
    cfg["sites"] = merged
    cfg["lives"] = cfg.get("lives") or [{"name": "精选直播", "type": 0,
                                         "url": "http://" + LIVE_DOMAIN + "/live.m3u"}]
    json.dump(cfg, open(CUSTOM, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    cloud = t.load_cloud()
    if not cloud:
        log("未配置云端，仅保存到本地配置")
        return False, "仅保存本地"
    ok, msg = t.publish_config(cfg, cloud)
    log(("发布成功: " if ok else "发布失败: ") + msg)
    if ok:
        try:
            url = cloud.get("config_url") or cloud["upload_url"].rsplit("/upload", 1)[0] + "/tvbox.json"
            j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": t.UA}),
                                                  timeout=20, context=t.CTX).read().decode("utf-8-sig"))
            log("云端回读: %d 站" % len(j.get("sites") or []))
        except Exception as e:
            log("回读失败: %s" % repr(e)[:60])
    return ok, msg


def open_merge_dialog(root):
    import tkinter as tk
    from tkinter import ttk, messagebox

    if getattr(t, "IS_SHARE", False):
        messagebox.showinfo("合并源地址", "分享版不可用。\n该功能为站长更新专用。")
        return
    try:
        if not owner_auth.is_verified():
            messagebox.showinfo("合并源地址", "请从「站长工具」进入（需要站长密码）。")
            return
    except Exception:
        pass
    if not os.path.exists(CUSTOM) or not os.path.exists(POOL):
        messagebox.showinfo("合并源地址", "缺少 custom_config.json 或候选池，请先扫描并导出候选池。")
        return

    win = tk.Toplevel(root)
    win.title("合并源地址（站长专用）")
    win.minsize(940, 620)
    win.update_idletasks()
    ww, wh = 980, 660
    try:
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        x = rx + max(0, (root.winfo_width() - ww) // 2)
        y = ry + max(0, int((root.winfo_height() - wh) * 0.3))
        win.geometry("%dx%d+%d+%d" % (ww, wh, x, y))
    except Exception:
        win.geometry("%dx%d" % (ww, wh))

    q = []
    state = {"running": False, "rows": [], "merged": [], "tested": []}

    top = ttk.Frame(win, padding=(12, 10, 12, 2))
    top.pack(fill="x")
    ttk.Label(top, text="合并源地址", font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
    ttk.Label(top, text="从候选池抽测站点 → 择优合并进当前单仓（≤%d 站）" % TARGET_CAP,
              foreground="#888", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=10)

    sbar = ttk.Frame(win, padding=(12, 2, 12, 6))
    sbar.pack(fill="x")
    prog = ttk.Progressbar(sbar, mode="determinate", maximum=100, length=170)
    prog.pack(side="left", padx=(0, 10))
    status = ttk.Label(sbar, text="就绪", font=("Microsoft YaHei UI", 11, "bold"), foreground="#0a7")
    status.pack(side="left")

    ctl = ttk.Frame(win, padding=(12, 0, 12, 6))
    ctl.pack(fill="x")
    btn_all = ttk.Button(ctl, text="全选")
    btn_all.pack(side="left")
    btn_none = ttk.Button(ctl, text="清空")
    btn_none.pack(side="left", padx=6)
    btn_smart = ttk.Button(ctl, text="智能勾选")
    btn_smart.pack(side="left", padx=6)
    qs = ttk.Label(ctl, text="?", foreground="#1a73e8", cursor="question_arrow",
                   font=("Microsoft YaHei UI", 10, "bold"))
    qs.pack(side="left", padx=(2, 8))

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
            tk.Label(w, text=text, justify="left", background="#ffffe1", relief="solid",
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

    tip(qs, "建议勾选标 ★推荐 的候选（延迟≤800ms 且站点≥40）。\n"
            "全选会抽测更多站点、更慢；智能勾选通常结果接近全选，但快很多。\n"
            "最终合并仍按「实测播放延迟」择优，最多保留 15 个站点。")
    btn_go = ttk.Button(ctl, text="开始合并")
    btn_go.pack(side="left", padx=12)
    ttk.Button(ctl, text="关闭", command=win.destroy).pack(side="right")
    ttk.Label(ctl, text="（点击行切换勾选）", foreground="#888").pack(side="left", padx=4)

    wrap = ttk.Frame(win, padding=(12, 0, 12, 6))
    wrap.pack(fill="both", expand=True)
    cols = ("sel", "rec", "src", "sites", "ms", "url")
    tv = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
    for c, (title, w) in zip(cols, [("选", 40), ("建议", 64), ("来源", 110), ("站点", 55), ("延迟", 70), ("地址", 500)]):
        tv.heading(c, text=title)
        tv.column(c, width=w, anchor="w", stretch=(c == "url"))
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    logf = ttk.Frame(win, padding=(12, 0, 12, 8))
    logf.pack(fill="x")
    logtxt = tk.Text(logf, height=6, font=("Consolas", 9), wrap="word")
    logtxt.pack(fill="x")
    logtxt.configure(state="disabled")

    def log(msg):
        q.append(("log", msg))

    def append_log(msg):
        logtxt.configure(state="normal")
        logtxt.insert("end", str(msg) + "\n")
        logtxt.see("end")
        logtxt.configure(state="disabled")

    def toggle(event=None):
        row = tv.identify_row(event.y) if event is not None else (tv.selection()[0] if tv.selection() else None)
        if not row:
            return
        vals = list(tv.item(row, "values"))
        vals[0] = "☑" if vals[0] != "☑" else "☐"
        tv.item(row, values=vals)

    tv.bind("<ButtonRelease-1>", toggle)

    def rec_of(it):
        ms = it.get("ms") or 99999
        sites = it.get("sites") or 0
        if ms <= 800 and sites >= 40:
            return "★推荐"
        if ms <= 1500 and sites >= 20:
            return "○可选"
        return "·"

    def load_rows():
        for x in tv.get_children():
            tv.delete(x)
        cur = load_current()
        items = sorted(load_pool(), key=lambda x: (x.get("ms") or 99999))[:80]
        status.config(text="现有 %d 个站点 · 候选池 %d 条 · 已按延迟排序" % (
            len(cur.get("sites") or []), len(items)), foreground="#0a7")
        for it in items:
            tv.insert("", "end", values=("☐", rec_of(it), it.get("src", ""), it.get("sites", ""),
                                         "%dms" % (it.get("ms") or 0), it.get("url", "")))

    def selected():
        out = []
        for r in tv.get_children():
            v = tv.item(r, "values")
            if v[0] == "☑":
                out.append((v[5], int(str(v[4]).replace("ms", "") or 99999)))
        out.sort(key=lambda x: x[1])
        return [u for u, _ in out]

    def set_all(flag):
        for r in tv.get_children():
            v = list(tv.item(r, "values"))
            v[0] = "☑" if flag else "☐"
            tv.item(r, values=v)

    def poll():
        while q:
            kind, payload = q.pop(0)
            if kind == "log":
                append_log(payload)
                status.config(text="⏳ " + str(payload)[:70], foreground="#1a73e8")
            elif kind == "prog":
                pct, txt = payload
                prog.config(value=pct)
                status.config(text="⏳ " + txt, foreground="#1a73e8")
            elif kind == "done":
                state["running"] = False
                prog.config(value=100)
                merged, tested, stats = payload
                state["merged"], state["tested"] = merged, tested
                btn_go.config(state="normal")
                try:
                    win.title("合并源地址（站长专用）")
                except Exception:
                    pass
                cur_n = len((load_current().get("sites") or []))
                extra = len([s for s in merged if s.get("key", "").startswith("cms_") and s not in (load_current().get("sites") or [])])
                summary = ("测试通过 %d 个候选站点\n现有 %d 站 → 合并后 %d 站（上限 %d）\n\n是否写入配置并发布？"
                           % (stats.get("passed", 0), cur_n, len(merged), TARGET_CAP))
                status.config(text="✅ 抽测完成，等待确认", foreground="#0a7")
                win.lift()
                if messagebox.askyesno("确认合并", summary, parent=win):
                    status.config(text="⏳ 正在备份并发布配置…", foreground="#1a73e8")
                    try:
                        win.update_idletasks()
                    except Exception:
                        pass
                    ok, msg = apply_publish(merged, append_log)
                    status.config(text=("✅ 已发布 %d 站" % len(merged)) if ok else ("❌ " + str(msg)[:60]),
                                  foreground="#0a7" if ok else "#c00")
                else:
                    status.config(text="已取消", foreground="#c60")
            elif kind == "err":
                state["running"] = False
                try:
                    win.title("合并源地址（站长专用）")
                except Exception:
                    pass
                status.config(text="❌ " + str(payload)[:60], foreground="#c00")
                append_log("出错：" + str(payload))
        win.after(150, poll)

    def worker(urls):
        try:
            log("开始处理 %d 个候选（并行提取）…" % len(urls))
            sites = []
            done = 0
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = [ex.submit(extract_sites, u) for u in urls]
                for f in futs:
                    try:
                        got = f.result()
                    except Exception:
                        got = []
                    sites += got
                    done += 1
                    if done % 3 == 0 or done == len(urls):
                        log("候选 %d/%d 已提取，累计可用站点 %d" % (done, len(urls), len(sites)))
                        q.append(("prog", (int(done / max(1, len(urls)) * 25),
                                           "提取站点 %d/%d" % (done, len(urls)))))
            # 去重（按 API 域）
            uniq = {}
            for s in sites:
                k = net_of(s.get("api") or "")
                if k and k not in uniq:
                    uniq[k] = s
            sites = list(uniq.values())[:MAX_TEST]
            cur = load_current().get("sites") or []
            tasks = [s for s in cur] + sites
            log("待抽测 %d 个站点（现有 %d + 新增 %d）" % (len(tasks), len(cur), len(sites)))
            tested = []
            done = 0
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(probe_site, s): s for s in tasks}
                for f in futs:
                    s = futs[f]
                    try:
                        ok, ms, detail = f.result()
                    except Exception:
                        ok, ms, detail = False, 99999, "异常"
                    tested.append((s, ok, ms))
                    done += 1
                    if done % 5 == 0:
                        q.append(("prog", (25 + int(done / max(1, len(tasks)) * 70), "抽测 %d/%d" % (done, len(tasks)))))
            passed = [x for x in tested if x[1]]
            merged = merge_sites(cur, tested)
            q.append(("prog", (100, "完成")))
            q.append(("done", (merged, tested, {"passed": len(passed)})))
        except Exception as e:
            q.append(("err", repr(e)[:120]))

    def start():
        if state["running"]:
            return
        urls = selected()
        if not urls:
            messagebox.showinfo("合并源地址", "请先勾选候选（点击行切换）")
            return
        if len(urls) > 10:
            if not messagebox.askyesno("确认", "勾选了 %d 个候选，抽测会比较慢，继续？" % len(urls), parent=win):
                return
            win.lift()
        state["running"] = True
        btn_go.config(state="disabled")
        prog.config(value=0)
        status.config(text="⏳ 已开始：正在下载候选配置…", foreground="#1a73e8")
        try:
            win.title("合并源地址（正在抽测，请等待…）")
            win.lift()
        except Exception:
            pass
        threading.Thread(target=worker, args=(urls,), daemon=True).start()

    def smart_select():
        rows = tv.get_children()
        picked = 0
        for r in rows:
            v = list(tv.item(r, "values"))
            if str(v[1]).startswith("★"):
                v[0] = "☑"
                picked += 1
                tv.item(r, values=v)
        if picked == 0:
            for r in rows[:5]:
                v = list(tv.item(r, "values"))
                v[0] = "☑"
                tv.item(r, values=v)
                picked += 1
        status.config(text="已智能勾选 %d 个候选（按延迟和站点数推荐）" % picked, foreground="#0a7")

    btn_all.config(command=lambda: set_all(True))
    btn_none.config(command=lambda: set_all(False))
    btn_smart.config(command=smart_select)
    btn_go.config(command=start)
    load_rows()
    win.after(150, poll)
    return win
