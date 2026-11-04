# -*- coding: utf-8 -*-
"""分享版入口：关闭固定地址发布，打包成 exe 发给别人用。"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tvbox_finder as t

t.ALLOW_PUBLISH = False
t.IS_SHARE = True
t.VERSION = "1.6 分享版"


def main():
    if "--selftest" in sys.argv:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.destroy()
        print("selftest ok | version=%s | here=%s | publish=%s" % (t.VERSION, t.HERE, t.ALLOW_PUBLISH))
        return
    try:
        t.main_gui()
    except Exception:
        msg = traceback.format_exc()
        try:
            with open(os.path.join(t.HERE, "error.log"), "w", encoding="utf-8") as f:
                f.write(msg)
        except Exception:
            pass
        try:
            import tkinter.messagebox as mb
            mb.showerror("TVBox 地址工具", "启动失败：\n\n" + msg[-900:])
        except Exception:
            pass


if __name__ == "__main__":
    main()
