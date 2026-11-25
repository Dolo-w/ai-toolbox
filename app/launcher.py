#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小工具箱桌面启动器 v2
- 先弹窗（加载页），后端就绪后自动跳转，杜绝"点了没反应"
- 全程异常写 %TEMP%\\toolbox-launcher.log，pythonw 静默失败可排查
- pywebview 原生窗口优先，Edge --app 兜底
"""
import os
import sys
import time
import threading
import traceback
import subprocess
import urllib.request

LOG_PATH = os.path.join(os.environ.get("TEMP", "."), "toolbox-launcher.log")

def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass

try:
    log("=== 启动器开始 ===")
    if getattr(sys, "frozen", False):
        # PyInstaller 打包态：资源定位基于 exe 自身位置
        TOOLBOX_DIR = os.path.dirname(os.path.abspath(sys.executable))
        BASE_DIR = os.path.join(TOOLBOX_DIR, "app")
    else:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))      # toolbox/app
        TOOLBOX_DIR = os.path.dirname(BASE_DIR)                    # toolbox

    # --- 便携模式检测：exe 与 backend/runtime-* 同级（绿色包根）---
    PORTABLE_BACKEND = os.path.join(TOOLBOX_DIR, "backend")
    IS_PORTABLE = os.path.isdir(PORTABLE_BACKEND) and os.path.exists(
        os.path.join(TOOLBOX_DIR, "runtime-hivision", "python.exe"))
    if IS_PORTABLE:
        log("便携模式: " + TOOLBOX_DIR)
        PY_8080 = os.path.join(TOOLBOX_DIR, "runtime-hivision", "python.exe")
        PY_8081 = os.path.join(TOOLBOX_DIR, "runtime-tools", "python.exe")
        SCRIPT_8080 = os.path.join(PORTABLE_BACKEND, "deploy_api.py")
        SCRIPT_8081 = os.path.join(PORTABLE_BACKEND, "toolbox_api.py")
        CWD_8080 = PORTABLE_BACKEND
        CWD_8081 = PORTABLE_BACKEND
    else:
        PY_8080 = os.path.join(TOOLBOX_DIR, "HivisionIDPhotos", ".venv", "Scripts", "python.exe")
        PY_8081 = os.path.join(TOOLBOX_DIR, ".venv", "Scripts", "python.exe")
        SCRIPT_8080 = "deploy_api.py"
        SCRIPT_8081 = "toolbox_api.py"
        CWD_8080 = os.path.join(TOOLBOX_DIR, "HivisionIDPhotos")
        CWD_8081 = BASE_DIR
    URL = "http://127.0.0.1:8081/"
    CREATE_NO_WINDOW = 0x08000000

    LOADING_HTML = """
    <!DOCTYPE html><html><head><meta charset="utf-8"><style>
      body { margin:0; height:100vh; display:flex; flex-direction:column; align-items:center;
             justify-content:center; background:#f7f8fa; font-family:"Segoe UI","Microsoft YaHei",sans-serif; }
      .box { width:72px; height:72px; border:6px solid #e5e7eb; border-top-color:#185fa5;
             border-radius:50%; animation:r .9s linear infinite; }
      @keyframes r { to { transform:rotate(360deg); } }
      p { color:#656d76; font-size:15px; margin-top:24px; }
      small { color:#9aa3ad; font-size:12px; margin-top:6px; }
    </style></head><body>
      <div class="box"></div>
      <p>小工具箱启动中…</p>
      <small>首次冷启动约需 10~20 秒，服务就绪后自动进入</small>
    </body></html>
    """

    def port_alive(url, timeout=1.2):
        try:
            urllib.request.urlopen(url, timeout=timeout)
            return True
        except urllib.error.HTTPError:
            return True  # 4xx/5xx 也是 HTTP 响应，说明端口有人
        except Exception:
            return False

    TARGETS = [
        (8080, "http://127.0.0.1:8080/docs", PY_8080, SCRIPT_8080, CWD_8080),
        (8081, URL, PY_8081, SCRIPT_8081, CWD_8081),
    ]

    def start_backend(exe, script, cwd):
        return subprocess.Popen(
            [exe, script], cwd=cwd,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def ensure_backends(backends):
        """backends: {port: Popen} —— 只登记本启动器亲手拉起的进程（身份凭证）。
        端口已被占用时一律跳过不拉起：是不是自己的进程，交给守护线程按身份判定。"""
        for port, url, exe, script, cwd in TARGETS:
            if not port_alive(url):
                log("%d 未运行，拉起 (%s)" % (port, "便携" if IS_PORTABLE else "开发"))
                backends[port] = start_backend(exe, script, cwd)
            else:
                log("%d 已运行，跳过（归属由守护线程判定）" % port)
        return backends

    def watchdog(backends, stop_event, window=None):
        """后端守护：按进程身份管理，绝不碰别人的进程。
        - 身份凭证 = 自己 Popen 句柄（backends[port]），只对自己拉起的进程负责
        - 端口活着 + 自己进程在 → 健康
        - 端口活着 + 自己进程不在 → 外部程序占用，告警一次并放弃接管（不杀、不抢）
        - 端口死了 + 自己进程在 → 冷启动中，耐心等
        - 端口死了 + 自己进程不在 → 确认是自己的后端挂了，自动拉起（每端口限 5 次）
        - 8081 复活后自动刷新窗口页面，用户无感恢复
        """
        restarts = {8080: 0, 8081: 0}
        foreign_announced = {8080: False, 8081: False}
        launch_time = time.time()
        while not stop_event.wait(5):
            for port, url, exe, script, cwd in TARGETS:
                if restarts[port] >= 5:
                    continue
                proc = backends.get(port)
                mine = proc is not None and proc.poll() is None  # 自己的进程还活着
                if port_alive(url):
                    if mine:
                        foreign_announced[port] = False
                        continue
                    if proc is not None and time.time() - launch_time < 120:
                        continue  # 自己的进程在但还没监听：冷启动宽限
                    if not foreign_announced[port]:
                        foreign_announced[port] = True
                        log("守护: %d 端口被外部程序占用（非本程序进程），不接管不杀进程，等待其释放" % port)
                    continue
                # 端口死了：外部占用者若已退出，端口释放，此时接管是安全的
                was_foreign = foreign_announced[port]
                foreign_announced[port] = False
                if mine:
                    continue  # 自己的进程还活着（可能在启动），不重复拉起
                restarts[port] += 1
                if was_foreign:
                    log("守护: %d 外部占用已释放，接管拉起自己的后端（第 %d 次）" % (port, restarts[port]))
                else:
                    log("守护: %d 后端已退出，第 %d 次自动拉起" % (port, restarts[port]))
                try:
                    backends[port] = start_backend(exe, script, cwd)
                except Exception as e:
                    log("守护拉起失败(%d): %r" % (port, e))
                if port == 8081:
                    deadline = time.time() + 60
                    while time.time() < deadline:
                        if port_alive(url):
                            log("守护: 8081 复活，刷新页面")
                            try:
                                if window:
                                    window.load_url(url)
                            except Exception as e:
                                log("守护刷新页面失败: %r" % e)
                            break
                        time.sleep(1)

    def probe_and_navigate(window):
        deadline = time.time() + 90
        while time.time() < deadline:
            if port_alive(URL):
                log("8081 就绪，跳转主页")
                window.load_url(URL)
                return
            time.sleep(0.5)
        log("等待 8081 超时")
        window.load_html("<body style='font-family:sans-serif;text-align:center;padding-top:40vh'>服务启动超时，请查看日志 " + LOG_PATH + "</body>")

    def run_webview():
        import webview
        window = webview.create_window(
            "小工具箱", html=LOADING_HTML,
            width=1080, height=800, min_size=(860, 640),
        )
        stop = threading.Event()
        t = threading.Thread(target=watchdog, args=(procs, stop, window),
                             name="toolbox-backend-watchdog", daemon=True)
        t.start()
        try:
            webview.start(probe_and_navigate, window)
        finally:
            stop.set()
        return True

    def run_edge():
        edge_candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        edge = next((p for p in edge_candidates if os.path.exists(p)), None)
        if edge:
            subprocess.Popen([edge, "--app=" + URL])
            return True
        import webbrowser
        webbrowser.open(URL)
        return False

    def register_first_run():
        """首次运行自动注册：开始菜单快捷方式（Win 搜索可开）+ App Paths（Win+R 可开）
        全部写 HKCU/当前用户目录，无需管理员权限；快捷方式只在缺失时创建，App Paths 每次刷新（搬家自愈）"""
        exe_path = os.path.abspath(sys.executable)
        # 1) 开始菜单快捷方式（Windows 搜索的主索引来源）
        try:
            lnk_path = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                                    "Start Menu", "Programs", "小工具箱.lnk")
            if os.path.exists(lnk_path):
                log("开始菜单快捷方式已存在，跳过")
            else:
                import win32com.client
                shell = win32com.client.Dispatch("WScript.Shell")
                lnk = shell.CreateShortCut(lnk_path)
                lnk.TargetPath = exe_path
                lnk.WorkingDirectory = TOOLBOX_DIR
                lnk.Description = "小工具箱：证件照/字幕/消除笔/老照片修复"
                icon = os.path.join(TOOLBOX_DIR, "backend", "icon.ico")
                lnk.IconLocation = (icon if os.path.exists(icon) else exe_path) + ",0"
                lnk.Save()
                log("已注册开始菜单快捷方式: " + lnk_path)
        except Exception as e:
            log("开始菜单注册失败(不影响使用): " + repr(e))
        # 2) App Paths：Win+R 输入 xiaoGongJuXiang 即可启动；每次运行刷新，exe 搬家后自动修正
        try:
            import winreg
            key = winreg.CreateKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\App Paths\xiaoGongJuXiang.exe")
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, exe_path)
            winreg.CloseKey(key)
            log("App Paths 已注册/刷新（Win+R xiaoGongJuXiang 可用）")
        except Exception as e:
            log("App Paths 注册失败(不影响使用): " + repr(e))

    procs = {}  # {port: Popen} 只登记本启动器亲手拉起的后端
    ensure_backends(procs)

    if getattr(sys, "frozen", False):
        register_first_run()

    if not port_alive(URL) and 8081 not in procs:
        sys.exit(1)

    try:
        run_webview()
        log("pywebview 窗口正常退出")
    except Exception as e:
        log("pywebview 失败: " + repr(e))
        traceback.print_exc(file=sys.stderr)
        # Edge 兜底模式也开守护（无窗口句柄，仅自动拉起后端）
        stop = threading.Event()
        threading.Thread(target=watchdog, args=(procs, stop),
                         name="toolbox-backend-watchdog", daemon=True).start()
        run_edge()
        time.sleep(8)  # 给 Edge 一点打开时间
        stop.set()

    # 只清理本启动器拉起的后端（按身份清理，绝不误杀别人的进程）
    for p in procs.values():
        try:
            if p:
                p.terminate()
        except Exception:
            pass
    log("=== 启动器结束 ===")
except Exception:
    log("启动器顶层异常:\n" + traceback.format_exc())
