#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小工具箱统一后端（端口 8081）
  /transcribe  语音/视频 -> 字幕（SenseVoice / sherpa-onnx，SRT+纯文本）
  /inpaint     图片消除笔（OpenCV Telea 修复）
  /upscale     图片 4x 放大增强（OpenCV dnn_superres，EDSR/FSRCNN 自适应）
与证件照服务(8080, deploy_api.py)相互独立。
"""
import os
import sys
import io
import re
import json
import time
import base64
import asyncio
import shutil
import threading
import tempfile
import subprocess
import traceback

import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- 便携版是嵌入式 Python，site 不会处理 lib-tools 里的 .pth ----
# pywin32 的 win32 / win32\lib 两个子目录必须自己补进 sys.path，
# 否则 import win32com 会报 No module named 'pywintypes'。
_LIB_TOOLS = os.path.join(BASE_DIR, "lib-tools")
for _p in (os.path.join(_LIB_TOOLS, "win32", "lib"),
           os.path.join(_LIB_TOOLS, "win32"),
           _LIB_TOOLS):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
MODELS_DIR = os.path.join(os.path.dirname(BASE_DIR), "models")
os.makedirs(MODELS_DIR, exist_ok=True)

app = FastAPI(title="小工具箱 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_blocking(fn):
    """把重活丢进线程池，避免阻塞事件循环"""
    return asyncio.get_running_loop().run_in_executor(None, fn)


def b64_png(img_bgr):
    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        raise RuntimeError("PNG 编码失败")
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()


def decode_upload(data: bytes):
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("图片解码失败，请上传 JPG/PNG")
    return img


# ============================================================
# 字幕转写（SenseVoice 内核，sherpa-onnx，随包分发）
# ============================================================
# SenseVoice 内核（sherpa-onnx，int8 约 230MB，随包分发无需下载）
SENSEVOICE_DIR = os.path.join(BASE_DIR, "models", "sensevoice")
_sv_recognizer = None
_sv_lock = threading.Lock()


def get_sensevoice():
    global _sv_recognizer
    with _sv_lock:
        if _sv_recognizer is None:
            import sherpa_onnx
            _sv_recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=os.path.join(SENSEVOICE_DIR, "model.int8.onnx"),
                tokens=os.path.join(SENSEVOICE_DIR, "tokens.txt"),
                num_threads=4, use_itn=True,
            )
        return _sv_recognizer


# 热词后处理（SenseVoice 无原生热词）：同长度同拼音 → 替换。
# 每个字有自己的时间戳，替换不改变字数，时间轴不受影响。
def _hotword_fix(text: str, hotwords) -> str:
    """hotwords: 词列表（或逗号分隔字符串）。同长度同拼音 → 替换。"""
    if isinstance(hotwords, str):
        hotwords = re.split(r"[,，、;；\n]+", hotwords)
    hw_str = " ".join(hotwords or []).strip()
    if not hw_str or not text:
        return text
    try:
        from pypinyin import lazy_pinyin, Style
    except ImportError:
        return text
    def _py(s):
        return lazy_pinyin(s, style=Style.TONE3, errors=lambda x: list(x))
    for hw in hotwords:
        hw = hw.strip()
        # 只处理含汉字的热词（纯英文热词在中文里不会以同音形式出错）
        if len(hw) < 2 or not re.search(r"[\u4e00-\u9fff]", hw):
            continue
        n = len(hw)
        py_hw = _py(hw)
        if len(py_hw) != n:
            continue
        i = 0
        while i + n <= len(text):
            window = text[i:i + n]
            if window != hw and _py(window) == py_hw:
                text = text[:i] + hw + text[i + n:]
            i += 1
    return text


# 内置热词表：零操作成本自动纠错。文件可自行编辑（一行一个，# 开头为注释）
HOTWORDS_FILE = os.path.join(BASE_DIR, "hotwords.txt")


def _load_hotwords(extra: str = "") -> list:
    parts = []
    if os.path.isfile(HOTWORDS_FILE):
        try:
            with open(HOTWORDS_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts.append(line)
        except OSError:
            pass
    parts.extend(h for h in re.split(r"[,，、;；\n]+", extra or "") if h.strip())
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _fmt_srt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ============================================================
# 异步任务进度（转写/烧录共用）：前端轮询 /progress 渲染进度条
# ============================================================
_TASKS = {}          # task_id -> 状态 dict
_TASKS_LOCK = threading.Lock()


def _task_new(kind: str, message: str) -> str:
    tid = f"{kind}_{int(time.time()*1000)}_{threading.get_ident()}"
    with _TASKS_LOCK:
        _TASKS[tid] = {
            "phase": "queue", "progress": 0, "message": message,
            "done": False, "ok": None, "result": None, "error": None,
        }
        # 只留最近 20 个已完成任务，防内存涨
        done_ids = [k for k, v in _TASKS.items() if v["done"]]
        for k in done_ids[:-20]:
            _TASKS.pop(k, None)
    return tid


def _task_set(tid: str, **kw):
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if t:
            t.update(kw)


@app.get("/progress")
async def progress(task_id: str):
    with _TASKS_LOCK:
        t = _TASKS.get(task_id)
    if not t:
        return JSONResponse({"status": False, "message": "任务不存在或已过期"})
    return {"status": True, **t}


# 繁体 -> 简体（中文语音识别容易输出繁体，opencc 兜底转换）
_t2s = None

def _get_t2s():
    global _t2s
    if _t2s is None:
        try:
            import opencc
            _t2s = opencc.OpenCC("t2s")
        except Exception:
            _t2s = False  # opencc 不可用时跳过转换，不阻塞主流程
    return _t2s


def _to_simplified(text: str) -> str:
    cc = _get_t2s()
    if cc is False:
        return text
    try:
        return cc.convert(text)
    except Exception:
        return text


# 中文输出的半角标点转全角（观感自然）
_ZH_PUNCT = str.maketrans({",": "，", ".": "。", "?": "？", "!": "！",
                           ":": "：", ";": "；"})


def _zh_normalize(text: str) -> str:
    return text.translate(_ZH_PUNCT)


def _extract_audio(src_path: str, dst_path: str):
    """统一走 ffmpeg 转成 16k 单声道 wav（音视频通吃）"""
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, "-y", "-i", src_path, "-vn", "-ac", "1", "-ar", "16000", dst_path]
    p = subprocess.run(cmd, capture_output=True, timeout=1800)
    if p.returncode != 0 or not os.path.exists(dst_path):
        raise RuntimeError("音频提取失败: " + p.stderr.decode(errors="ignore")[-400:])


def _display_width(s: str) -> float:
    """显示宽度：全角（CJK/中文标点）算 1，半角算 0.5，单位=全角字符"""
    w = 0.0
    for ch in s:
        w += 1.0 if ord(ch) > 0x2E7F else 0.5  # CJK 统一表意+全角标点区
    return w


def _refine_segments(raw_segments, max_chars: float = 18.0):
    """
    字幕排版整形：
    1) 用词级时间戳收紧每条字幕的起止（解决段尾拖音、音画不同步）
    2) 超过 max_chars 全角宽的长句，在词边界切成多条短字幕
       （每条时间随词戳精确分配，且单条单行绝不超出画面宽度）
    """
    out = []
    for seg in raw_segments:
        words = [w for w in seg.get("words") or [] if w.get("word", "").strip()]
        if not words:
            # 没有词级时间戳的兜底：整段原样保留（但太长也硬切，时间均分不可靠——只折行不切条）
            text = seg["text"]
            if _display_width(text) > max_chars * 2:  # 超两行宽度 -> 按 max_chars 折成多行
                lines, cur, cur_w = [], "", 0.0
                for ch in text:
                    cw = _display_width(ch)
                    if cur_w + cw > max_chars and cur:
                        lines.append(cur)
                        cur, cur_w = ch, cw
                    else:
                        cur += ch
                        cur_w += cw
                if cur:
                    lines.append(cur)
                n = max(1, len(lines))
                dur = max(0.6, seg["end"] - seg["start"])
                for i, ln in enumerate(lines):
                    out.append({
                        "start": round(seg["start"] + dur * i / n, 2),
                        "end": round(seg["start"] + dur * (i + 1) / n, 2),
                        "text": ln,
                    })
            else:
                out.append({"start": seg["start"], "end": seg["end"], "text": text})
            continue

        # 先按标点切成短句（字幕贴标点断行，断句观感远好于硬切宽度）
        punct = "。！？；!?;，,、：:"
        clauses, cur = [], []
        for w in words:
            cur.append(w)
            wt = w["word"].strip()
            if wt and wt[-1] in punct:
                clauses.append(cur)
                cur = []
        if cur:
            clauses.append(cur)
        clauses = [
            {
                "words": c,
                "start": c[0]["start"],
                "end": c[-1]["end"],
                "text": "".join(w["word"] for w in c),
                "width": sum(_display_width(w["word"]) for w in c),
            }
            for c in clauses if c
        ]

        # 再把短句按宽度打包成行；单句超宽时按词边界硬切
        line, line_start, line_end, line_w = "", None, None, 0.0

        def _flush():
            nonlocal line, line_start, line_end, line_w
            if line.strip():
                out.append({
                    "start": round(line_start, 2),
                    "end": round(max(line_end, line_start + 0.3), 2),
                    "text": line.strip(),
                })
            line, line_start, line_end, line_w = "", None, None, 0.0

        for cl in clauses:
            if line and line_w + cl["width"] > max_chars:
                _flush()
            if cl["width"] <= max_chars:
                line += cl["text"]
                line_w += cl["width"]
                if line_start is None:
                    line_start = cl["start"]
                line_end = cl["end"]
            else:
                # 单句超过整行宽度：按词边界硬切成多行，时间随词戳分配
                if line:
                    _flush()
                sub, sub_start, sub_end = "", None, None
                for w in cl["words"]:
                    if sub and _display_width(sub) + _display_width(w["word"]) > max_chars:
                        out.append({
                            "start": round(sub_start, 2),
                            "end": round(max(sub_end, sub_start + 0.3), 2),
                            "text": sub.strip(),
                        })
                        sub = ""
                    if sub_start is None:
                        sub_start = w["start"]
                    sub += w["word"]
                    sub_end = w["end"]
                if sub.strip():
                    out.append({
                        "start": round(sub_start, 2),
                        "end": round(max(sub_end, sub_start + 0.3), 2),
                        "text": sub.strip(),
                    })
        _flush()
    return out


def _do_transcribe(tid: str, data: bytes, model_size: str, language: str,
                   hotwords: str = "", engine: str = "sensevoice"):
    _task_set(tid, phase="audio", progress=2, message="提取音频…")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "input.bin")
        wav = os.path.join(td, "audio.wav")
        with open(src, "wb") as f:
            f.write(data)
        _extract_audio(src, wav)

        return _transcribe_sensevoice(tid, wav, hotwords)


def _pack_result(segments, texts, language, duration):
    """SRT 组装收尾（切分整形已完成，segments 里已是修正后的文本）"""
    srt_lines = []
    for i, s in enumerate(segments, 1):
        srt_lines.append(
            f"{i}\n{_fmt_srt_ts(s['start'])} --> {_fmt_srt_ts(s['end'])}\n{s['text']}\n"
        )
    return {
        "status": True,
        "language": language,
        "duration": duration,
        "count": len(segments),
        "text": "\n".join(texts),
        "srt": "\n".join(srt_lines),
    }


def _transcribe_sensevoice(tid: str, wav: str, hotwords: str):
    """SenseVoice 内核：RTF≈0.03（快 30 倍），自带标点与逐字时间戳。
    逐字 token/时间戳一一对应 → 现有切分管线直接复用。"""
    import wave as _wave
    _task_set(tid, phase="model", progress=3, message="加载语音模型…")
    rec = get_sensevoice()
    with _wave.open(wav, "rb") as w:
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    total = max(0.1, len(audio) / sr)
    chunk_n = sr * 30                    # 30 秒一块，防长音频内存爆
    n_chunks = max(1, -(-len(audio) // chunk_n))
    raw, texts = [], []
    lang_tag = "zh"
    _task_set(tid, phase="asr", progress=5, message="语音识别中…")
    for ci in range(n_chunks):
        seg = audio[ci * chunk_n:(ci + 1) * chunk_n]
        if len(seg) < sr * 0.2:          # 尾部不足 0.2s 丢弃
            break
        offset = ci * 30.0
        stream = rec.create_stream()
        stream.accept_waveform(sr, seg)
        rec.decode_stream(stream)
        r = stream.result
        if not r.text.strip():
            continue
        lang_tag = (r.lang or "").strip("<|>") or "zh"
        words = []
        ts = r.timestamps or []
        durs = getattr(r, "durations", None) or []
        for i, tok in enumerate(r.tokens):
            st = ts[i] + offset
            if i + 1 < len(ts):
                en = ts[i + 1] + offset
            elif i < len(durs):
                en = st + durs[i]
            else:
                en = st + 0.08
            words.append({"word": tok, "start": round(st, 2), "end": round(en, 2)})
        raw.append({
            "start": offset,
            "end": offset + len(seg) / sr,
            "text": r.text,
            "words": words,
        })
        texts.append(r.text)
        # 识别阶段 5% -> 92%
        p = min(92, 5 + 87 * (ci + 1) / n_chunks)
        asr_pct = int((p - 5) / 87 * 100)
        _task_set(tid, progress=int(p), message=f"语音识别中 {asr_pct}%")
    if not raw:
        raise RuntimeError("未识别到语音内容")
    # 排版整形：词戳收紧 + 超宽切条（保证不超画面宽度）
    segments = _refine_segments(raw)
    # 热词修正：同长度同拼音替换，字数不变 → 逐字时间戳不受影响
    hw_str = _load_hotwords(hotwords)
    if hw_str:
        for s in segments:
            s["text"] = _hotword_fix(s["text"], hw_str)
    texts = [_hotword_fix(t, hw_str) for t in texts]
    _task_set(tid, progress=94, message="整理字幕…")
    return _pack_result(segments, texts, lang_tag, round(total, 1))


def _run_transcribe(tid: str, data: bytes, model_size: str, language: str,
                    hotwords: str = "", engine: str = "sensevoice"):
    try:
        result = _do_transcribe(tid, data, model_size, language, hotwords, engine)
        _task_set(tid, done=True, ok=True, phase="done", progress=100,
                  message="转写完成", result=result)
    except Exception as e:
        traceback.print_exc()
        _task_set(tid, done=True, ok=False, phase="error",
                  message="转写失败", error=f"{type(e).__name__}: {e}")


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    model_size: str = Form("small"),
    language: str = Form("auto"),
    hotwords: str = Form(""),
):
    data = await file.read()
    if not data:
        return JSONResponse({"status": False, "message": "未收到文件"})
    tid = _task_new("asr", "准备中…")
    threading.Thread(
        target=_run_transcribe, args=(tid, data, model_size, language, hotwords),
        daemon=True,
    ).start()
    return {"status": True, "task_id": tid}


# ============================================================
# 字幕烧录（ffmpeg subtitles 滤镜，直接出带字幕的视频）
# ============================================================
FONTS_DIR = os.path.join(BASE_DIR, "fonts")            # 内置可商用字体
USER_FONTS_DIR = os.path.join(BASE_DIR, "user_fonts")  # 用户导入字体
USER_FONTS_INDEX = os.path.join(USER_FONTS_DIR, "fonts.json")
# 成品视频固定保存到这里，生成完前端自动打开文件夹选中文件
BURN_OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "AIToolbox")

# 内置字体（family 名供 libass 按名字匹配；file=None 表示系统字体不内置）
# 许可均为免费商用：HarmonyOS Sans（华为官方）、Smiley Sans 得意黑（OFL）
BUILTIN_FONTS = [
    {"id": "harmonyos_medium", "family": "HarmonyOS Sans SC Medium",
     "file": "HarmonyOS_Sans_SC_Medium.ttf", "label": "鸿蒙黑体·中黑（内置·可商用）"},
    {"id": "harmonyos_bold", "family": "HarmonyOS Sans SC",
     "file": "HarmonyOS_Sans_SC_Bold.ttf", "label": "鸿蒙黑体·粗（内置·可商用）"},
    {"id": "smiley", "family": "Smiley Sans Oblique",
     "file": "SmileySans-Oblique.ttf", "label": "得意黑·斜（内置·可商用）"},
    {"id": "msyh", "family": "Microsoft YaHei",
     "file": None, "label": "微软雅黑（系统）"},
]


def _read_user_fonts():
    try:
        with open(USER_FONTS_INDEX, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_user_fonts(lst):
    os.makedirs(USER_FONTS_DIR, exist_ok=True)
    with open(USER_FONTS_INDEX, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False, indent=1)


@app.get("/font/list")
async def font_list():
    fonts = [dict(x, builtin=True) for x in BUILTIN_FONTS]
    fonts += [dict(x, builtin=False) for x in _read_user_fonts()]
    return {"status": True, "fonts": fonts}


@app.post("/font/upload")
async def font_upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return JSONResponse({"status": False, "message": "未收到字体文件"})
    if len(data) > 64 * 1024 * 1024:
        return JSONResponse({"status": False, "message": "字体文件超过 64MB"})
    name = file.filename or ""
    if not name.lower().endswith((".ttf", ".otf", ".ttc")):
        return JSONResponse({"status": False, "message": "仅支持 TTF / OTF / TTC 字体文件"})
    try:
        from fontTools.ttLib import TTFont
        import io as _io
        f = TTFont(_io.BytesIO(data), fontNumber=0, lazy=True)
        family = (f["name"].getDebugName(1) or "").strip()
        f.close()
        if not family:
            return JSONResponse({"status": False, "message": "无法读取字体名称，文件可能已损坏"})
        lst = _read_user_fonts()
        for x in lst:
            if x["family"] == family:
                return JSONResponse({"status": False, "message": f"字体「{family}」已导入过"})
        os.makedirs(USER_FONTS_DIR, exist_ok=True)
        ext = name.rsplit(".", 1)[-1].lower()
        safe_name = f"userfont_{int(time.time()*1000)}_{os.getpid()}.{ext}"
        with open(os.path.join(USER_FONTS_DIR, safe_name), "wb") as fh:
            fh.write(data)
        lst.append({"id": safe_name.rsplit(".", 1)[0], "family": family,
                    "file": f"user_fonts/{safe_name}", "label": f"{family}（导入）"})
        _write_user_fonts(lst)
        return {"status": True, "family": family, "fonts": [dict(x, builtin=True) for x in BUILTIN_FONTS] + [dict(x, builtin=False) for x in lst]}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"status": False, "message": f"字体解析失败: {type(e).__name__}: {e}"})


def _resolve_font(font_id: str):
    """返回 (family, 字体文件的绝对路径或 None)。None=系统字体。"""
    for x in BUILTIN_FONTS:
        if x["id"] == font_id:
            p = os.path.join(FONTS_DIR, x["file"]) if x["file"] else None
            return x["family"], (p if p and os.path.exists(p) else None)
    for x in _read_user_fonts():
        if x["id"] == font_id or x["family"] == font_id:
            p = os.path.join(BASE_DIR, x["file"])
            return x["family"], (p if os.path.exists(p) else None)
    # 兜底：微软雅黑
    return "Microsoft YaHei", None


def _hex_to_ass(color: str) -> str:
    """#RRGGBB -> ASS 的 &HBBGGRR&（BGR 顺序）"""
    import re
    c = (color or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c or ""):
        return "&HFFFFFF&"
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H{b}{g}{r}&".upper()


def _do_burn(tid: str, data: bytes, srt_text: str, font_id: str = "msyh",
             font_size: int = 18, font_color: str = "#FFFFFF") -> str:
    """烧录字幕，直接输出到 Downloads\\AIToolbox，返回输出文件路径"""
    import re as _re
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    family, font_path = _resolve_font(font_id)
    font_size = max(8, min(48, int(font_size or 18)))
    primary = _hex_to_ass(font_color)
    os.makedirs(BURN_OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(
        BURN_OUTPUT_DIR, f"带字幕视频_{time.strftime('%H%M%S')}.mp4"
    )
    _task_set(tid, phase="burn", progress=2, message="烧录中…")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.bin")
        sub = os.path.join(td, "subs.srt")
        with open(src, "wb") as f:
            f.write(data)
        # UTF-8 BOM 让 libass 稳定识别编码；临时目录名可能含特殊字符，
        # 滤镜里用纯文件名相对路径 + cwd 切目录，绕开路径转义地狱
        with open(sub, "w", encoding="utf-8-sig") as f:
            f.write(srt_text)
        # 自定义字体：复制进临时 fonts/ 目录，libass 经 fontsdir 加载
        fdir = os.path.join(td, "fonts")
        os.makedirs(fdir, exist_ok=True)
        if font_path:
            shutil.copy2(font_path, os.path.join(fdir, os.path.basename(font_path)))
        vf = ("subtitles=subs.srt:fontsdir=fonts:"
              f"force_style=FontName={family}\\,FontSize={font_size}\\,"
              f"PrimaryColour={primary}\\,OutlineColour=&H000000&\\,"
              "Outline=1\\,Shadow=0\\,MarginV=25")
        cmd = [
            exe, "-y", "-i", src, "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",  # 音频重编码 aac，保证输出 mp4 兼容
            "-progress", "pipe:1", "-nostats",  # 机器可读进度 -> stdout
            out_path,
        ]
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=td, text=True
        )
        duration_s = None
        # 兜底：从 SRT 最后一条的结束时间估算视频时长
        # （stderr 的 Duration 行与编码并行读取，快手视频可能来不及先到）
        est = 0.0
        for m in _re.finditer(
            r"-->\s*(\d+):(\d+):(\d+),(\d+)", srt_text
        ):
            h, mi, s, ms = (int(x) for x in m.groups())
            est = max(est, h * 3600 + mi * 60 + s + ms / 1000)
        err_tail = ""
        # stderr 与 stdout 并行读，防管道写满卡死
        def _drain_err():
            nonlocal err_tail, duration_s
            for line in p.stderr:
                err_tail = line
                if duration_s is None and "Duration:" in line:
                    m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", line)
                    if m:
                        h, mi, s, ms = (int(x) for x in m.groups())
                        duration_s = h * 3600 + mi * 60 + s + ms / 100
        err_thread = threading.Thread(target=_drain_err, daemon=True)
        err_thread.start()
        for line in p.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                total = duration_s or est
                if total and total > 0:
                    pct = min(98, 2 + 96 * max(0.0, us / 1e6 / total))
                    _task_set(tid, progress=int(pct),
                              message=f"烧录中 {int((pct - 2) / 96 * 100)}%")
            elif line == "progress=end":
                _task_set(tid, progress=99, message="收尾…")
        p.wait(timeout=3600)
        err_thread.join(timeout=5)
        if p.returncode != 0 or not os.path.exists(out_path):
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            raise RuntimeError("字幕烧录失败: " + err_tail[-400:])
    return out_path


def _run_burn(tid: str, data: bytes, srt_text: str, font_id: str,
              font_size: int, font_color: str):
    try:
        path = _do_burn(tid, data, srt_text, font_id, font_size, font_color)
        _task_set(tid, done=True, ok=True, phase="done", progress=100,
                  message="烧录完成", result={"path": path, "folder": BURN_OUTPUT_DIR})
    except Exception as e:
        traceback.print_exc()
        _task_set(tid, done=True, ok=False, phase="error",
                  message="烧录失败", error=f"{type(e).__name__}: {e}")


@app.post("/burn")
async def burn(
    file: UploadFile = File(...),
    srt: str = Form(...),
    font: str = Form("msyh"),
    font_size: int = Form(18),
    font_color: str = Form("#FFFFFF"),
):
    data = await file.read()
    if not data:
        return JSONResponse({"status": False, "message": "未收到视频文件"})
    if not (srt or "").strip():
        return JSONResponse({"status": False, "message": "字幕内容为空"})
    tid = _task_new("burn", "准备中…")
    threading.Thread(
        target=_run_burn, args=(tid, data, srt, font, font_size, font_color),
        daemon=True,
    ).start()
    return {"status": True, "task_id": tid}


@app.post("/reveal")
async def reveal(path: str = Form(...)):
    """在资源管理器中定位文件。仅允许打开成品输出目录下的文件。"""
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(BURN_OUTPUT_DIR) + os.sep):
        return JSONResponse({"status": False, "message": "仅支持定位工具箱生成的文件"})
    if not os.path.exists(real):
        return JSONResponse({"status": False, "message": "文件不存在"})
    try:
        subprocess.Popen(["explorer", "/select,", real])
        return {"status": True}
    except Exception as e:
        return JSONResponse({"status": False, "message": f"{type(e).__name__}: {e}"})


# ============================================================
# Word <-> PDF 互转（本机 Word COM，不联网、不上传）
# ============================================================
CONVERT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "AIToolbox")
os.makedirs(CONVERT_DIR, exist_ok=True)
_WORD_LOCK = threading.Lock()

WD_DOCX = 16   # wdFormatXMLDocument
WD_PDF = 17    # wdFormatPDF


def _pdf2docx_convert(src, dst):
    """PDF → Word 用 pdf2docx（PyMuPDF 底层），完全不依赖 Microsoft Word。
    先写临时文件再搬，避免目标目录被占用时留下半截文件。"""
    from pdf2docx import Converter
    tmp_dst = os.path.join(tempfile.gettempdir(), "toolbox_p2d_%d.docx" % int(time.time() * 1000))
    cv = Converter(src)
    try:
        cv.convert(tmp_dst)
    finally:
        try:
            cv.close()
        except Exception:
            pass
    if not os.path.exists(tmp_dst) or os.path.getsize(tmp_dst) == 0:
        raise RuntimeError("pdf2docx 没有产出文件（可能 PDF 是扫描件或受密码保护）")
    shutil.move(tmp_dst, dst)


def _word_convert(src, dst, fmt):
    """用本机 Word 转格式。先存到临时文件再搬到目标目录：
    直接 SaveAs2 到 Downloads 下偶尔会报「命令失败」，两步走更稳。"""
    import pythoncom
    import win32com.client as win32
    ext = ".pdf" if fmt == WD_PDF else ".docx"
    tmp_dst = os.path.join(tempfile.gettempdir(), "toolbox_conv_%d%s" % (int(time.time() * 1000), ext))
    pythoncom.CoInitialize()
    app = None
    try:
        app = win32.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(
            src, ReadOnly=False, ConfirmConversions=False, AddToRecentFiles=False,
        )
        try:
            doc.SaveAs2(tmp_dst, FileFormat=fmt)
        finally:
            doc.Close(False)
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()
    if not os.path.exists(tmp_dst) or os.path.getsize(tmp_dst) == 0:
        raise RuntimeError("Word 没有产出文件（可能源文件损坏或受密码保护）")
    shutil.move(tmp_dst, dst)


def _convert_job(tid, data, ext, out_name, fmt):
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "input" + ext)
            with open(src, "wb") as f:
                f.write(data)
            dst = os.path.join(CONVERT_DIR, out_name)
            if fmt == WD_DOCX:
                _task_set(tid, phase="convert", progress=20, message="正在解析 PDF，重建 Word 排版…")
                _pdf2docx_convert(src, dst)
            else:
                _task_set(tid, phase="convert", progress=20, message="正在调用本机 Word 转换…")
                with _WORD_LOCK:
                    _word_convert(src, dst, fmt)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            raise RuntimeError("转换没有产出文件，可能源文件损坏或受密码保护")
        _task_set(tid, done=True, ok=True, phase="done", progress=100,
                  message="转换完成",
                  result={"path": dst, "size": os.path.getsize(dst), "name": out_name})
    except Exception as e:
        traceback.print_exc()
        msg = f"{type(e).__name__}: {e}"
        if "com_error" in msg.lower():
            msg = ("Word → PDF 这一步要用你机器上的 Word，需要「已激活」的桌面版。"
                   "未激活、订阅已过期、或只装了网页版 / WPS 都用不了。"
                   "PDF → Word 走的是内置引擎，不需要 Word。")
        _task_set(tid, done=True, ok=False, phase="error", message="转换失败", error=msg)


def _safe_stem(filename):
    """上传文件名可能是 RFC2047 编码（=?utf-8?B?...?=），也可能带非法字符。
    统一解出来再洗一遍，返回可安全用作文件名的主干。"""
    import email.header
    name = filename or "document"
    try:
        parts = email.header.decode_header(name)
        name = "".join(
            (p.decode(c or "utf-8", "ignore") if isinstance(p, bytes) else p)
            for p, c in parts
        )
    except Exception:
        pass
    name = os.path.basename(name.replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" ._")
    return (stem[:60] or "document")


def _stamp():
    return time.strftime("%Y%m%d_%H%M%S")


def _sniff_kind(data):
    """按文件头判断真实类型：不信扩展名，也不信上传时的 filename。"""
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "docx"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "doc"
    return ""


@app.post("/word2pdf")
async def word2pdf(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return JSONResponse({"status": False, "message": "未收到文件"})
    kind = _sniff_kind(data)
    if kind not in ("docx", "doc"):
        return JSONResponse({"status": False, "message": "这不是 Word 文件（.docx / .doc），要么是 PDF，请换另一个按钮"})
    stem = _safe_stem(file.filename)
    tid = _task_new("convert", "准备中…")
    threading.Thread(
        target=_convert_job,
        args=(tid, data, "." + kind, f"{_stamp()}_{stem}.pdf", WD_PDF),
        daemon=True,
    ).start()
    return {"status": True, "task_id": tid}


@app.post("/pdf2word")
async def pdf2word(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return JSONResponse({"status": False, "message": "未收到文件"})
    kind = _sniff_kind(data)
    if kind != "pdf":
        return JSONResponse({"status": False, "message": "这不是 PDF 文件，要么是 Word，请换另一个按钮"})
    stem = _safe_stem(file.filename)
    tid = _task_new("convert", "准备中…")
    threading.Thread(
        target=_convert_job,
        args=(tid, data, ".pdf", f"{_stamp()}_{stem}.docx", WD_DOCX),
        daemon=True,
    ).start()
    return {"status": True, "task_id": tid}


# ============================================================
# 消除笔（OpenCV inpaint）
# ============================================================
@app.post("/inpaint")
async def inpaint(
    image: UploadFile = File(...),
    mask: UploadFile = File(...),
    radius: int = Form(5),
    method: str = Form("telea"),
):
    try:
        img = decode_upload(await image.read())
        m = decode_upload(await mask.read())
        if m.shape[:2] != img.shape[:2]:
            m = cv2.resize(m, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)
        # mask: 任何明显笔迹都算待擦除区
        mask_gray = cv2.cvtColor(m, cv2.COLOR_BGR2GRAY)
        _, mask_bin = cv2.threshold(mask_gray, 20, 255, cv2.THRESH_BINARY)
        # 稍微膨胀，盖住笔迹边缘残留
        mask_bin = cv2.dilate(mask_bin, np.ones((5, 5), np.uint8), iterations=2)
        algo = cv2.INPAINT_NS if method == "ns" else cv2.INPAINT_TELEA
        result = cv2.inpaint(img, mask_bin, radius, algo)
        return {"status": True, "image_base64": b64_png(result)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"status": False, "message": f"{type(e).__name__}: {e}"})


# ============================================================
# 老照片修复 / 图片放大（dnn_superres）
# ============================================================
_sr = {}

def _get_sr(scale: int, model_name: str):
    key = (scale, model_name)
    if key not in _sr:
        sr = cv2.dnn_superres.DnnSuperResImpl_create()
        path = os.path.join(MODELS_DIR, f"{model_name}_x{scale}.pb")
        if not os.path.exists(path):
            raise FileNotFoundError(f"模型缺失: {path}")
        sr.readModel(path)
        sr.setModel(model_name.lower(), scale)
        _sr[key] = sr
    return _sr[key]


def _do_upscale(img, scale: int):
    # 大图用 FSRCNN（快），小图用 EDSR（质量好）；模型缺失时降级 Lanczos+锐化
    pixels = img.shape[0] * img.shape[1]
    name = "FSRCNN" if pixels > 700_000 else "EDSR"
    model_path = os.path.join(MODELS_DIR, f"{name}_x{scale}.pb")
    if os.path.exists(model_path) and hasattr(cv2, "dnn_superres"):
        sr = _get_sr(scale, name)
        out = sr.upsample(img)
        engine = name
    else:
        # 兜底：Lanczos 放大 + USM 锐化
        w, h = img.shape[1] * scale, img.shape[0] * scale
        out = cv2.resize(img, (w, h), interpolation=cv2.INTER_LANCZOS4)
        engine = "LanczosFallback"
    # 轻度锐化收尾，观感更"修复"
    blur = cv2.GaussianBlur(out, (0, 0), 1.0)
    out = cv2.addWeighted(out, 1.4, blur, -0.4, 0)
    return out, engine


@app.post("/upscale")
async def upscale(
    image: UploadFile = File(...),
    scale: int = Form(4),
):
    try:
        img = decode_upload(await image.read())
        if scale not in (2, 3, 4):
            scale = 4
        if img.shape[0] * img.shape[1] > 4_000_000:
            r = (4_000_000 / (img.shape[0] * img.shape[1])) ** 0.5
            img = cv2.resize(img, (int(img.shape[1] * r), int(img.shape[0] * r)))
        result, used = await run_blocking(lambda: _do_upscale(img, scale))
        return {"status": True, "engine": used, "image_base64": b64_png(result)}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"status": False, "message": f"{type(e).__name__}: {e}"})


# ============================================================
# 磁盘体检（只读扫描，任务式异步 + 进度轮询）
# ============================================================
_disk_tasks = {}  # scan_id -> {"progress": .., "total_files": .., "current": .., "done": .., "result": .., "error": ..}

# FILE_ATTRIBUTE_REPARSE_POINT：junction/符号链接一律不跟随（避免体积虚报）
_REPARSE = 0x400


def _dir_size_ex(root, state, big_files, min_big=100 * 1024 * 1024):
    """递归算目录体积；不跟随任何重解析点；顺手收集大文件。返回 (体积, 是否完整)"""
    total = 0
    complete = True
    try:
        entries = list(os.scandir(root))
    except (PermissionError, OSError):
        return 0, False
    for e in entries:
        try:
            if e.is_symlink():
                continue
            st = e.stat(follow_symlinks=False)
            if st.st_file_attributes & _REPARSE:
                continue
            if e.is_dir(follow_symlinks=False):
                state["total_files"] += 1
                if state["total_files"] % 3000 == 0:
                    state["current"] = e.path
                sub, ok = _dir_size_ex(e.path, state, big_files, min_big)
                total += sub
                complete = complete and ok
            else:
                total += st.st_size
                state["total_files"] += 1
                if st.st_size >= min_big:
                    big_files.append({"path": e.path, "size": st.st_size,
                                      "mtime": int(st.st_mtime)})
        except (PermissionError, OSError):
            complete = False
    return total, complete


def _probe_known_targets():
    """探测已知大户口径（缓存/回收站/系统文件），返回结构化清单"""
    import shutil
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    win = os.environ.get("WINDIR", r"C:\Windows")
    targets = []

    def add(name, path, kind, advice):
        if path and os.path.isdir(path):
            s, _ = _dir_size_ex(path, {"total_files": 0, "current": ""}, [])
            if s > 32 * 1024 * 1024:  # 小于 32MB 不值得一提
                targets.append({"name": name, "path": path, "size": s,
                                "kind": kind, "advice": advice})

    # —— 可直接清理的缓存（删除后自动重建）——
    add("pip 缓存", os.path.join(local, "pip", "cache"), "clean", "包管理缓存，可安全清空")
    add("npm 缓存", os.path.join(local, "npm-cache"), "clean", "包管理缓存，可安全清空")
    add("uv 缓存", os.path.join(local, "uv", "cache"), "clean", "包管理缓存，可安全清空")
    add("NVIDIA 着色器缓存", os.path.join(local, "NVIDIA", "DXCache"), "clean",
        "游戏首次启动会自动重建")
    add("NVIDIA GLCache", os.path.join(local, "NVIDIA", "GLCache"), "clean",
        "游戏首次启动会自动重建")
    add("用户 TEMP", os.environ.get("TEMP", ""), "clean", "临时文件，可清空")
    add("系统 TEMP", os.path.join(win, "Temp"), "clean", "临时文件，可清空（部分被占用属正常）")
    add("Windows 更新缓存", os.path.join(win, "SoftwareDistribution", "Download"),
        "clean", "已装完的更新包，可清空")
    add("CrashDumps 崩溃转储", os.path.join(local, "CrashDumps"), "clean",
        "程序崩溃日志，可清空")
    add("Chrome 缓存", os.path.join(local, "Google", "Chrome", "User Data",
        "Default", "Cache"), "clean", "浏览器缓存，可清空")
    add("Edge 缓存", os.path.join(local, "Microsoft", "Edge", "User Data",
        "Default", "Cache"), "clean", "浏览器缓存，可清空")
    add("HuggingFace 模型缓存", os.path.join(home, ".cache", "huggingface"),
        "confirm", "AI 模型缓存，确认不再使用的模型可删")
    add("Torch 模型缓存", os.path.join(home, ".cache", "torch"), "confirm",
        "AI 模型缓存，确认后可删")
    # —— 回收站 ——
    for drive in "CDEFGH":
        rb = f"{drive}:\\$Recycle.Bin"
        if os.path.isdir(rb):
            s, _ = _dir_size_ex(rb, {"total_files": 0, "current": ""}, [])
            if s > 0:
                targets.append({"name": f"{drive} 盘回收站", "path": rb,
                                "size": s, "kind": "confirm",
                                "advice": "清空前请确认里面没有需要的文件"})
    # —— 系统/需管理员 ——
    for f, name, advice in [
        ("hiberfil.sys", "休眠文件", "不用休眠可在管理员终端执行 powercfg /h off"),
        ("pagefile.sys", "虚拟内存", "系统托管，不建议动"),
    ]:
        p = os.path.join(f"{os.path.splitdrive(win)[0]}\\", f)
        try:
            if os.path.exists(p):
                st = os.stat(p)
                targets.append({"name": name, "path": p, "size": st.st_size,
                                "kind": "system", "advice": advice})
        except (PermissionError, OSError):
            pass
    targets.sort(key=lambda x: -x["size"])
    return targets


def _do_disk_scan(scan_id: str, target: str):
    state = _disk_tasks[scan_id]
    try:
        # 1. 磁盘概况
        usage = shutil.disk_usage(target)
        overview = {"total": usage.total, "used": usage.used, "free": usage.free}
        # 扫的是盘根还是子目录：子目录时容量数据是整个卷的，前端要标注
        drive_part = os.path.splitdrive(target)[0]
        sub_path = os.path.splitdrive(target)[1]
        is_drive = sub_path.strip("\\") == ""

        # 2. 一层目录体积排行 + 全量大文件收集
        big_files = []
        rows = []
        try:
            top_entries = list(os.scandir(target))
        except (PermissionError, OSError):
            top_entries = []
        files_done_before = 0
        for e in top_entries:
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
                if st.st_file_attributes & _REPARSE:
                    continue
                if e.is_dir(follow_symlinks=False):
                    state["current"] = e.path
                    s, _ = _dir_size_ex(e.path, state, big_files)
                    rows.append({"name": e.name, "path": e.path, "size": s,
                                 "is_dir": True})
                else:
                    rows.append({"name": e.name, "path": e.path,
                                 "size": st.st_size, "is_dir": False})
                    if st.st_size >= 100 * 1024 * 1024:
                        big_files.append({"path": e.path, "size": st.st_size,
                                          "mtime": int(st.st_mtime)})
            except (PermissionError, OSError):
                continue
            files_done_before = state["total_files"]

        # 3. 对体积 Top5 的目录下钻一层（供展开查看）
        drill = {}
        for r in sorted([r for r in rows if r["is_dir"]],
                        key=lambda x: -x["size"])[:5]:
            sub = []
            try:
                for e2 in os.scandir(r["path"]):
                    try:
                        if e2.is_symlink():
                            continue
                        st2 = e2.stat(follow_symlinks=False)
                        if st2.st_file_attributes & _REPARSE:
                            continue
                        if e2.is_dir(follow_symlinks=False):
                            s2, _ = _dir_size_ex(e2.path, {"total_files": 0, "current": ""}, [])
                            sub.append({"name": e2.name, "size": s2})
                        else:
                            sub.append({"name": e2.name, "size": st2.st_size})
                    except (PermissionError, OSError):
                        continue
            except (PermissionError, OSError):
                pass
            sub.sort(key=lambda x: -x["size"])
            drill[r["name"]] = sub[:15]
            state["current"] = r["path"] + " (下钻)"

        # 4. 已知大户探测
        state["current"] = "探测已知缓存大户…"
        known = _probe_known_targets()

        big_files.sort(key=lambda x: -x["size"])
        # 回收站里的"大文件"不进大文件榜（已在已知大户单独列出）
        big_files = [f for f in big_files if "$recycle.bin" not in f["path"].lower()]
        state["result"] = {
            "target": target,
            "is_drive": is_drive,
            "drive": drive_part,
            "overview": overview,
            "top_dirs": sorted(rows, key=lambda x: -x["size"])[:25],
            "drill": drill,
            "big_files": big_files[:30],
            "known": known,
            "scanned_files": state["total_files"],
        }
        state["done"] = True
    except Exception as e:
        traceback.print_exc()
        state["error"] = f"{type(e).__name__}: {e}"
        state["done"] = True


@app.get("/api/disk/drives")
async def disk_drives():
    """枚举本机所有逻辑盘（本地磁盘/U盘/网络盘/光驱）"""
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        type_names = {2: "可移动磁盘", 3: "本地磁盘", 4: "网络驱动器", 5: "光驱"}
        drives = []
        for i in range(26):
            if bitmask & (1 << i):
                letter = chr(ord("A") + i)
                root = f"{letter}:\\"
                t = ctypes.windll.kernel32.GetDriveTypeW(root)
                drives.append({"letter": letter, "path": root,
                               "type": type_names.get(t, "未知")})
        return {"status": True, "drives": drives}
    except Exception as e:
        # 非 Windows 或 API 异常时退化为逐盘符探测
        drives = []
        for letter in "CDEFGH":
            if os.path.exists(f"{letter}:\\"):
                drives.append({"letter": letter, "path": f"{letter}:\\",
                               "type": "本地磁盘"})
        if drives:
            return {"status": True, "drives": drives}
        return JSONResponse({"status": False, "message": f"{type(e).__name__}: {e}"})


@app.post("/api/disk/scan")
async def disk_scan_start(target: str = Form(...)):
    target = target.strip() or "C:\\"
    if not os.path.exists(target):
        return JSONResponse({"status": False, "message": f"路径不存在: {target}"})
    scan_id = f"scan_{int(time.time() * 1000)}"
    _disk_tasks[scan_id] = {"progress": 0, "total_files": 0, "current": "准备中…",
                            "done": False, "result": None, "error": None}
    threading.Thread(target=_do_disk_scan, args=(scan_id, target), daemon=True,
                     name="toolbox-disk-scan").start()
    return {"status": True, "scan_id": scan_id}


@app.get("/api/disk/status")
async def disk_scan_status(scan_id: str):
    t = _disk_tasks.get(scan_id)
    if not t:
        return JSONResponse({"status": False, "message": "任务不存在或已过期"})
    return {"status": True, "done": t["done"], "error": t["error"],
            "scanned_files": t["total_files"], "current": t["current"],
            "result": t["result"]}


@app.post("/api/disk/open")
async def disk_open_path(path: str = Form(...)):
    """在资源管理器中打开目录（只允许目录，防误用）"""
    path = path.strip()
    if not os.path.isdir(path):
        return JSONResponse({"status": False, "message": "不是有效目录"})
    subprocess.Popen(["explorer", os.path.normpath(path)])
    return {"status": True}


# ---- 清理（白名单：仅允许清理探测列表里的非系统项）----
@app.post("/api/disk/clean")
async def disk_clean(path: str = Form(...)):
    path = os.path.normpath(path.strip())
    known = _probe_known_targets()
    hit = next((k for k in known if os.path.normpath(k["path"]) == path), None)
    if not hit:
        return JSONResponse({"status": False,
            "message": "该路径不在可清理白名单内，拒绝操作（只允许清理工具识别出的缓存大户）"})
    if hit["kind"] == "system":
        return JSONResponse({"status": False, "message": "系统项不允许清理"})
    freed = 0
    errors = 0
    try:
        for e in os.scandir(path):
            try:
                if e.is_dir(follow_symlinks=False):
                    for root, dirs, files in os.walk(e.path):
                        for f in files:
                            try:
                                freed += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                    shutil.rmtree(e.path, ignore_errors=True)
                else:
                    freed += e.stat(follow_symlinks=False).st_size
                    os.remove(e.path)
            except (PermissionError, OSError):
                errors += 1
        remain = 0
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    remain += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return {"status": True, "name": hit["name"], "freed": max(0, freed - remain),
                "remain": remain, "errors": errors}
    except Exception as e:
        return JSONResponse({"status": False, "message": f"{type(e).__name__}: {e}"})


# ---- 清空回收站（Windows 官方 Shell API，比手动删干净）----
@app.post("/api/disk/empty-recycle")
async def disk_empty_recycle():
    try:
        import ctypes
        # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND = 7
        ret = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)
        remain = 0
        for drive in "CDEFGH":
            rb = f"{drive}:\\$Recycle.Bin"
            if os.path.isdir(rb):
                s, _ = _dir_size_ex(rb, {"total_files": 0, "current": ""}, [])
                remain += s
        # ret=0 成功；-1（0xFFFFFFFF）多为本来就空
        return {"status": True, "remain": remain, "api_ret": ret}
    except Exception as e:
        return JSONResponse({"status": False, "message": f"{type(e).__name__}: {e}"})


# ---- 大文件夹迁移（跨盘复制→校验→删源；系统关键目录黑名单拒绝）----
_move_tasks = {}
_MOVE_FORBIDDEN = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "c:\\$recycle.bin", "c:\\system volume information", "c:\\boot",
]


def _forbidden_reason(src: str):
    low = os.path.normpath(src).lower().rstrip("\\")
    for f in _MOVE_FORBIDDEN:
        if low == f or low.startswith(f + "\\"):
            return "系统关键目录，禁止迁移"
    if "\\appdata\\" in low + "\\" or low.endswith("\\appdata"):
        return "AppData 是应用数据目录，移动会导致软件损坏；请用应用内的「更改存储路径」功能迁移"
    if low in ("c:\\", "d:\\", "c:", "d:"):
        return "不能迁移整个盘根"
    return None


def _do_move(tid: str, src: str, dst: str):
    st = _move_tasks[tid]
    try:
        same = os.path.splitdrive(src)[0].lower() == os.path.splitdrive(dst)[0].lower()
        if same:
            os.rename(src, dst)  # 同盘秒完成
            st.update(moved=st["total"], done=True, current="完成（同盘改名，秒完成）")
            return
        def copy_dir(s, d):
            os.makedirs(d, exist_ok=True)
            for e in os.scandir(s):
                if st.get("cancel"):
                    raise RuntimeError("用户取消")
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        copy_dir(e.path, os.path.join(d, e.name))
                    else:
                        dst_f = os.path.join(d, e.name)
                        shutil.copy2(e.path, dst_f)
                        if os.path.getsize(dst_f) == e.stat(follow_symlinks=False).st_size:
                            os.remove(e.path)  # 校验一致才删源
                            st["moved"] += e.stat(follow_symlinks=False).st_size
                        else:
                            st.setdefault("verify_failed", []).append(e.path)
                        st["current"] = e.path
                except (PermissionError, OSError) as ex:
                    st.setdefault("skipped", []).append(f"{e.path}: {ex}")
        copy_dir(src, dst)
        # 源目录清空后删除（可能因占用文件残留，残留则保留）
        try:
            os.rmdir(src)
        except OSError:
            pass
        st.update(done=True, current="迁移完成")
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"
        st["done"] = True


@app.post("/api/disk/move")
async def disk_move(src: str = Form(...), dst_parent: str = Form(...)):
    src = os.path.normpath(src.strip().strip('"'))
    dst_parent = os.path.normpath(dst_parent.strip().strip('"'))
    if not os.path.isdir(src):
        return JSONResponse({"status": False, "message": f"源目录不存在: {src}"})
    if not os.path.isdir(dst_parent):
        return JSONResponse({"status": False, "message": f"目标目录不存在: {dst_parent}"})
    reason = _forbidden_reason(src)
    if reason:
        return JSONResponse({"status": False, "message": reason})
    dst = os.path.join(dst_parent, os.path.basename(src.rstrip("\\\\")))
    if os.path.exists(dst):
        dst = dst + "_" + time.strftime("%Y%m%d_%H%M%S")
    # 目标盘空间检查
    total, _ = _dir_size_ex(src, {"total_files": 0, "current": ""}, [])
    du = shutil.disk_usage(dst_parent)
    if total > du.free:
        return JSONResponse({"status": False,
            "message": f"目标盘空间不足：需 {total/2**30:.1f} GB，仅剩 {du.free/2**30:.1f} GB"})
    tid = f"move_{int(time.time() * 1000)}"
    _move_tasks[tid] = {"total": total, "moved": 0, "current": "准备中…",
                        "done": False, "error": None, "skipped": [],
                        "verify_failed": [], "src": src, "dst": dst}
    threading.Thread(target=_do_move, args=(tid, src, dst), daemon=True,
                     name="toolbox-disk-move").start()
    return {"status": True, "task_id": tid, "total": total, "dst": dst}


@app.get("/api/disk/move-status")
async def disk_move_status(task_id: str):
    t = _move_tasks.get(task_id)
    if not t:
        return JSONResponse({"status": False, "message": "任务不存在或已过期"})
    return {"status": True, "done": t["done"], "error": t["error"],
            "total": t["total"], "moved": t["moved"], "current": t["current"],
            "skipped": t["skipped"][:5], "verify_failed": t["verify_failed"][:5],
            "skipped_count": len(t["skipped"]),
            "dst": t["dst"]}


@app.get("/health")
async def health():
    return {"status": True, "models": sorted(os.listdir(MODELS_DIR))}


@app.get("/")
async def index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TOOLBOX_PORT", "8081")))




