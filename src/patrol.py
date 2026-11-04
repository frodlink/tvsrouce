# -*- coding: utf-8 -*-

# 你自己的域名（自建 Worker 的域名，例：tv.example.com）
LIVE_DOMAIN = "YOUR_DOMAIN"
"""站长工具（站长专用）：一键巡检（站点体检 + 直播巡检 + 发布），以及合并站点入口。

由底部「站长工具」按钮打开；分享版不可用。
"""
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import tvbox_finder as t
import merge_warehouse as m
import owner_auth

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "数据")
CUSTOM = os.path.join(HERE, "custom_config.json")
POOL = os.path.join(DATA, "candidates_pool.json")
BACKUP_DIR = os.path.join(os.path.dirname(HERE), "备份")

CAP = 9
MIN_SITES = 8
LIVE_HEALTH = 0.85
STAGE_W = {"读取": (0, 3), "站点体检": (3, 32), "站点择优": (35, 10),
           "直播巡检": (45, 40), "直播补源": (85, 5), "发布": (90, 6), "校验": (96, 4)}


def load_custom():
    return json.load(open(CUSTOM, encoding="utf-8"))


def backup_config(tag="巡检前"):
    name = "custom_config_%s_%s.json" % (tag, time.strftime("%Y%m%d_%H%M%S"))
    path = os.path.join(BACKUP_DIR, name)
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        open(path, "w", encoding="utf-8").write(open(CUSTOM, encoding="utf-8").read())
        return path
    except Exception:
        return ""


def restore_latest(log=print):
    """恢复最近一次巡检/合并前的配置并发布。"""
    cands = []
    try:
        for fn in os.listdir(BACKUP_DIR):
            if re.match(r"custom_config_(巡检前|合并前)_\d{8}_\d{6}\.json$", fn):
                cands.append((os.path.getmtime(os.path.join(BACKUP_DIR, fn)), fn))
    except Exception:
        pass
    if not cands:
        log("没有找到可恢复的备份")
        return False, "无备份"
    cands.sort(reverse=True)
    src = os.path.join(BACKUP_DIR, cands[0][1])
    log("恢复备份：" + cands[0][1])
    open(CUSTOM, "w", encoding="utf-8").write(open(src, encoding="utf-8").read())
    cloud = t.load_cloud()
    if not cloud:
        log("未配置云端，仅恢复本地文件")
        return False, "仅本地"
    cfg = load_custom()
    ok, msg = t.publish_config(cfg, cloud)
    log(("发布成功: " if ok else "发布失败: ") + msg)
    return ok, msg


def _probe_site(site):
    """先测一次，失败则换关键词复测一次，降低误判。"""
    try:
        ok, ms, detail = m.probe_site(site, timeout=5)
        if ok:
            return site, True, ms, detail
        ok2, ms2, detail2 = m.probe_site(site, timeout=6, kw="斗罗大陆")
        if ok2:
            return site, True, ms2, "复测通过"
        return site, False, ms, detail + "/" + str(detail2)
    except Exception:
        return site, False, 99999, "异常"


def topup_from_pool(need, log=print, progress=None, stop=None):
    """从候选池补站（不扫新源）：取配置延迟最低的候选，抽测其站点，通过则补入。"""
    added = []
    if need <= 0:
        return added
    try:
        items = json.load(open(POOL, encoding="utf-8")).get("items", [])
    except Exception:
        items = []
    items = sorted(items, key=lambda x: (x.get("ms") or 99999))[:6]
    if not items:
        log("候选池为空，无法补站（可用「源接口工具」扫描后导出）")
        return added
    for it in items:
        if stop and stop():
            break
        if len(added) >= need:
            break
        u = it.get("url")
        log("  从候选补站：%s" % str(u)[:70])
        sites = m.extract_sites(u)
        if not sites:
            continue
        tested = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for s, ok, ms, detail in ex.map(_probe_site, sites[:12]):
                tested.append((s, ok, ms))
        for s, ok, ms in tested:
            if ok:
                s = dict(s)
                s["_lat"] = ms
                added.append(s)
                if len(added) >= need:
                    break
        if progress:
            progress("站点择优", len(added), need)
    return added


def check_live(log=print, progress=None, stop=None):
    """巡检云端现有直播清单，逐频道测；返回 (count, total)。"""
    cloud = t.load_cloud()
    if not cloud:
        log("未配置云端，跳过直播巡检")
        return 0, 0
    https_url, http_url = t.live_urls(cloud)
    try:
        d = t._get(http_url, timeout=15, limit=800_000)
        items = t.parse_playlist_text(d.decode("utf-8-sig", "replace"))
    except Exception as e:
        log("读取云端直播失败：%s" % repr(e)[:60])
        return 0, 0
    if not items:
        log("云端直播为空")
        return 0, 0
    total = len(items)
    ok = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(t.live_probe, u, 5) for _, u in items]
        for i, f in enumerate(futs, 1):
            if stop and stop():
                break
            try:
                ms = f.result()
            except Exception:
                ms = None
            if ms is not None:
                ok += 1
            if progress and (i % 3 == 0 or i == total):
                progress("直播巡检", i, total)
    log("直播巡检：%d/%d 可用" % (ok, total))
    return ok, total


def run_patrol(log=print, progress=None, stop=None):
    """一键巡检：站点体检 → 择优补站 → 直播巡检 → 补源 → 发布 → 校验。"""
    t0 = time.time()
    if progress:
        progress("读取", 1, 1)
    cfg = load_custom()
    sites = return_sites = cfg.get("sites") or []
    log("读取当前配置：%d 个站点" % len(sites))

    # 站点体检
    tested = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_probe_site, s) for s in sites]
        for i, f in enumerate(futs, 1):
            if stop and stop():
                break
            try:
                tested.append(f.result())
            except Exception:
                pass
            if progress:
                progress("站点体检", i, len(sites))
    kept, dropped = [], []
    for s, ok, ms, detail in tested:
        if ok:
            kept.append(s)
        else:
            dropped.append((s.get("name"), detail))
    log("站点体检：%d 站，正常 %d，淘汰 %d" % (len(sites), len(kept), len(dropped)))
    for name, detail in dropped[:6]:
        log("  淘汰：%s（%s）" % (str(name)[:20], detail))

    # 低于最少站数则从候选池补
    added = []
    if len(kept) < MIN_SITES:
        need = min(CAP, MIN_SITES) - len(kept)
        log("站点不足 %d 个，尝试从候选池补 %d 个（不扫新源）" % (MIN_SITES, need))
        added = topup_from_pool(need, log=log, progress=progress, stop=stop)
        log("补入 %d 个站点" % len(added))
        kept += added
    kept = kept[:CAP]

    # 直播巡检
    if progress:
        progress("直播巡检", 0, 1)
    live_ok, live_total = check_live(log=log, progress=progress, stop=stop)
    live_action = ""
    if live_total and live_ok / float(live_total) < LIVE_HEALTH:
        log("直播可用率 %.0f%%，低于 %.0f%%，正在重建直播源…" % (100.0 * live_ok / live_total, LIVE_HEALTH * 100))
        if progress:
            progress("直播补源", 0, 1)
        try:
            live = t._live_pipeline(log)
            live_action = "已重建直播（%d 频道）" % ((live or {}).get("count") or 0)
        except Exception as e:
            live_action = "重建直播失败：" + repr(e)[:50]
        log(live_action)
    else:
        live_action = "沿用现有直播（%d/%d 可用）" % (live_ok, live_total)

    # 发布
    if progress:
        progress("发布", 0, 1)
    backup = backup_config("巡检前")
    if backup:
        log("已备份配置：" + os.path.basename(backup))
    cfg["sites"] = kept
    cfg["lives"] = cfg.get("lives") or [{"name": "精选直播", "type": 0, "url": "http://" + LIVE_DOMAIN + "/live.m3u"}]
    cloud = t.load_cloud()
    pub_ok, pub_msg = False, "未配置云端"
    if cloud:
        pub_ok, pub_msg = t.publish_config(cfg, cloud)
        log(("发布成功: " if pub_ok else "发布失败: ") + pub_msg)
    else:
        log("未配置云端，仅更新本地配置")

    # 校验
    if progress:
        progress("校验", 0, 1)
    if pub_ok and cloud:
        try:
            url = cloud.get("config_url") or t.live_urls(cloud)[1].replace("live.m3u", "tvbox.json")
            j = json.loads(t._get(url, timeout=15, limit=800_000).decode("utf-8-sig", "replace"))
            log("云端回读：%d 站" % len(j.get("sites") or []))
        except Exception as e:
            log("回读失败：%s" % repr(e)[:50])
    if progress:
        progress("校验", 1, 1)
    summary = {
        "kept": len(kept), "dropped": len(dropped), "added": len(added),
        "live_ok": live_ok, "live_total": live_total, "live_action": live_action,
        "pub_ok": pub_ok, "pub_msg": pub_msg, "secs": round(time.time() - t0, 1),
    }
    log("巡检完成：%d 站（淘汰 %d，补入 %d）· %s · 耗时 %.0fs" % (
        len(kept), len(dropped), len(added), live_action, summary["secs"]))
    return summary


# ------------------------------------------------------------------ GUI

def open_patrol(root):
    import tkinter as tk
    from tkinter import ttk, messagebox

    if getattr(t, "IS_SHARE", False):
        messagebox.showinfo("站长工具", "分享版不可用。\n该功能为站长维护专用。")
        return
    try:
        if not owner_auth.is_verified():
            messagebox.showinfo("站长工具", "请先通过主界面的「站长工具」按钮输入站长密码。")
            return
    except Exception:
        pass

    win = tk.Toplevel(root)
    win.title("站长工具（站长专用）")
    win.minsize(1000, 640)
    win.update_idletasks()
    ww, wh = 1120, 720
    try:
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        x = rx + max(0, (root.winfo_width() - ww) // 2)
        y = ry + max(0, int((root.winfo_height() - wh) * 0.28))
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(10, min(x, sw - ww - 20))
        y = max(10, min(y, sh - wh - 40))
        win.geometry("%dx%d+%d+%d" % (ww, wh, x, y))
    except Exception:
        win.geometry("%dx%d" % (ww, wh))

    q = []
    state = {"running": False, "stop": False}

    top = ttk.Frame(win, padding=(14, 12, 14, 2))
    top.pack(fill="x")
    ttk.Label(top, text="站长工具", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
    ttk.Label(top, text="一键巡检：站点体检 → 择优补站 → 直播巡检 → 补源 → 发布",
              foreground="#888", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=12)
    ttk.Label(top, text="规则：站点上限 %d · 最少 %d · 直播可用率 ≥ %d%%" % (
        CAP, MIN_SITES, int(LIVE_HEALTH * 100)), foreground="#888",
        font=("Microsoft YaHei UI", 9)).pack(side="right")

    sbar = ttk.Frame(win, padding=(14, 4, 14, 8))
    sbar.pack(fill="x")
    prog = ttk.Progressbar(sbar, mode="determinate", maximum=100, length=220)
    prog.pack(side="left", padx=(0, 12))
    status = ttk.Label(sbar, text="就绪", font=("Microsoft YaHei UI", 12, "bold"), foreground="#0a7")
    status.pack(side="left")

    ctl = ttk.Frame(win, padding=(14, 0, 14, 8))
    ctl.pack(fill="x")
    btn_go = ttk.Button(ctl, text="一键巡检")
    btn_go.pack(side="left")
    btn_merge = ttk.Button(ctl, text="只合并站点")
    btn_merge.pack(side="left", padx=8)
    btn_restore = ttk.Button(ctl, text="恢复上次配置")
    btn_restore.pack(side="left", padx=8)
    ttk.Button(ctl, text="关闭", command=win.destroy).pack(side="right")

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
                     wraplength=420).pack()
            w.wm_geometry("+%d+%d" % (widget.winfo_rootx() + 16, widget.winfo_rooty() + widget.winfo_height() + 6))
            holder["w"] = w

        def hide(_e):
            if holder["w"] is not None:
                holder["w"].destroy()
                holder["w"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    q1 = ttk.Label(ctl, text="?", foreground="#1a73e8", cursor="question_arrow",
                   font=("Microsoft YaHei UI", 10, "bold"))
    q1.pack(side="left", padx=(4, 0))
    tip(q1, "一键巡检（不扫新源）：\n"
            "1. 逐站测接口 + 搜索 + 首片播放，淘汰失效站点\n"
            "2. 站点少于 %d 个时从候选池补；否则不加站\n"
            "3. 逐频道检查云端的直播清单，可用率低于 %d%% 就重建\n"
            "4. 自动备份 → 发布配置 → 云端回读校验" % (MIN_SITES, int(LIVE_HEALTH * 100)))

    logf = ttk.Frame(win, padding=(14, 0, 14, 10))
    logf.pack(fill="both", expand=True)
    logbox = tk.Text(logf, font=("Consolas", 10), wrap="word")
    logbox.pack(fill="both", expand=True)
    logbox.configure(state="disabled")
    sb = ttk.Scrollbar(logf, orient="vertical", command=logbox.yview)
    logbox.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")

    def on_progress(stage, done, total):
        base, span = STAGE_W.get(stage, (0, 0))
        frac = 0 if not total else min(1.0, done / float(total))
        pct = int(base + span * frac)
        try:
            prog.config(value=pct)
        except Exception:
            pass
        return pct, "%s %d%%（%d/%d）" % (stage, pct, done, total)

    def poll():
        while q:
            kind, payload = q.pop(0)
            if kind == "log":
                logbox.configure(state="normal")
                logbox.insert("end", str(payload) + "\n")
                logbox.see("end")
                logbox.configure(state="disabled")
                status.config(text="⏳ " + str(payload)[:80], foreground="#1a73e8")
            elif kind == "prog":
                pct, txt = on_progress(*payload)
                status.config(text="⏳ " + txt, foreground="#1a73e8")
            elif kind == "done":
                state["running"] = False
                btn_go.config(state="normal")
                try:
                    prog.config(value=100)
                except Exception:
                    pass
                s = payload
                status.config(text="✅ 巡检完成：%d 站（淘汰 %d / 补入 %d，耗时 %ss）" % (
                    s.get("kept", 0), s.get("dropped", 0), s.get("added", 0), s.get("secs", 0)), foreground="#0a7")
                win.title("站长工具（站长专用）")
            elif kind == "err":
                state["running"] = False
                btn_go.config(state="normal")
                status.config(text="❌ " + str(payload)[:70], foreground="#c00")
                win.title("站长工具（站长专用）")
        win.after(150, poll)

    def worker():
        try:
            s = run_patrol(log=lambda x: q.append(("log", x)),
                           progress=lambda *a: q.append(("prog", a)),
                           stop=lambda: state["stop"])
            q.append(("done", s))
        except Exception as e:
            q.append(("err", repr(e)[:120]))

    def start():
        if state["running"]:
            return
        state["running"] = True
        state["stop"] = False
        btn_go.config(state="disabled")
        prog.config(value=0)
        status.config(text="⏳ 开始巡检…", foreground="#1a73e8")
        win.title("站长工具（正在巡检，请等待…）")
        threading.Thread(target=worker, daemon=True).start()

    def do_restore():
        if not messagebox.askyesno("恢复配置", "用最近一次备份覆盖当前配置并发布？", parent=win):
            return
        ok, msg = restore_latest(lambda x: q.append(("log", x)))
        status.config(text=("✅ 恢复并发布成功" if ok else ("⚠ 恢复完成：" + str(msg)[:50])),
                      foreground="#0a7" if ok else "#c60")

    def do_merge():
        try:
            m.open_merge_dialog(root)
        except Exception as e:
            messagebox.showerror("合并站点", "打开失败：%s" % repr(e)[:150], parent=win)

    btn_go.config(command=start)
    btn_merge.config(command=do_merge)
    btn_restore.config(command=do_restore)
    win.after(150, poll)
    return win
