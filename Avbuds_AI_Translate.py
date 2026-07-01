import os
import re
import sys
import ctypes
import customtkinter as ctk
import threading
import asyncio
import websockets
import queue
import numpy as np
import soundcard as sc
import json
import base64
import time
import logging
import traceback
from openai import OpenAI
from PIL import Image
import config

# Lõi logic thuần (đã có self-test: python avbuds_core.py)
from avbuds_core import (
    TranscriptEngine, RollingSummary, mock_summary_data,
    clean_protocol_tags, LANG_CODE_MAP, REPORT_SCHEMA,
    build_markdown, seconds_to_mmss, Glossary, extract_candidate_terms,
)


import urllib.request  # tải/đẩy glossary lên GitHub (stdlib)


def _app_dir():
    """Thư mục cạnh app để lưu file bền (cache): cạnh .exe khi đóng gói, else cạnh script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def glossary_cache_path():
    """File cache glossary cục bộ (dùng khi offline). Bị .gitignore."""
    return os.path.join(_app_dir(), "glossary.cache.json")


def load_glossary():
    """Nạp NHANH (không mạng): cache cục bộ nếu có, else bản đóng gói kèm exe.
    Bản mới nhất từ cloud được kéo về ở luồng nền (xem App._refresh_glossary_from_cloud)."""
    cache = glossary_cache_path()
    return Glossary.load(cache) if os.path.exists(cache) else Glossary.load(resource_path("glossary.json"))


# ===== ĐỒNG BỘ GLOSSARY VỚI GITHUB (cloud) =====
def _gh(attr, default):
    return getattr(config, attr, default) or default


def _github_token():
    """Token GitHub để ĐẨY thuật ngữ mới lên cloud. Nạp theo thứ tự ưu tiên (2 cách đầu
    KHÔNG cần build lại exe & KHÔNG bị đóng gói vào exe → an toàn hơn):
      1) biến môi trường AVBUDS_GH_TOKEN
      2) file 'github_token.txt' đặt CẠNH app (.exe) — mỗi máy tự bỏ token của mình
      3) config.GLOSSARY_GITHUB_TOKEN (chạy từ nguồn)"""
    t = (os.environ.get("AVBUDS_GH_TOKEN", "") or "").strip()
    if t:
        return t
    try:
        p = os.path.join(_app_dir(), "github_token.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    except Exception:
        pass
    return (getattr(config, "GLOSSARY_GITHUB_TOKEN", "") or "").strip()


def github_glossary_fetch(timeout=6):
    """Tải glossary.json từ GitHub. Có token -> Contents API (đọc cả repo private); không ->
    raw URL (repo public). Trả (data_dict, sha) hoặc (None, None)."""
    repo = _gh("GLOSSARY_GITHUB_REPO", "Detoc92/AI-Translate")
    branch = _gh("GLOSSARY_GITHUB_BRANCH", "main")
    path = _gh("GLOSSARY_GITHUB_PATH", "glossary.json")
    token = _github_token()
    try:
        if token:
            url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "User-Agent": "Avbuds-Glossary"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8"))
            content = base64.b64decode(j["content"]).decode("utf-8")
            return json.loads(content), j.get("sha")
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "Avbuds-Glossary"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:
        logger.warning(f"[GLOSSARY] Không tải được từ GitHub: {e}")
        return None, None


def github_glossary_push(data_dict, message="update glossary via app"):
    """Đẩy glossary.json lên GitHub (Contents API, CẦN token). Trả (ok, new_sha|errmsg)."""
    token = _github_token()
    if not token:
        return False, "Chưa có token (AVBUDS_GH_TOKEN / github_token.txt / config.GLOSSARY_GITHUB_TOKEN)"
    repo = _gh("GLOSSARY_GITHUB_REPO", "Detoc92/AI-Translate")
    branch = _gh("GLOSSARY_GITHUB_BRANCH", "main")
    path = _gh("GLOSSARY_GITHUB_PATH", "glossary.json")
    _d, sha = github_glossary_fetch(timeout=6)   # lấy sha mới nhất để không đè nhầm
    body = {"message": message, "branch": branch,
            "content": base64.b64encode(
                json.dumps(data_dict, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="PUT", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "Avbuds-Glossary", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            j = json.loads(r.read().decode("utf-8"))
        return True, j.get("content", {}).get("sha")
    except Exception as e:
        return False, str(e)

# Tránh crash UnicodeEncodeError khi console không phải UTF-8 (vd cp949/cp1252)
for _stream in (sys.stdout, sys.stderr):
    try: _stream.reconfigure(encoding="utf-8")
    except Exception: pass

# ================= CỜ CHẾ ĐỘ TEST =================
# python Avbuds_AI_Translate.py --demo            -> bơm transcript giả lập, KHÔNG cần mic
# python Avbuds_AI_Translate.py --demo --mock-ai  -> + summary giả lập, KHÔNG cần API key
# (--demo tự bật mock-ai trừ khi đặt env AVBUDS_REAL_AI=1)
DEMO = ("--demo" in sys.argv) or os.environ.get("AVBUDS_DEMO") == "1"
_MOCK_FLAG = ("--mock-ai" in sys.argv) or os.environ.get("AVBUDS_MOCK_AI") == "1"
MOCK_AI = _MOCK_FLAG or (DEMO and os.environ.get("AVBUDS_REAL_AI") != "1")


def resource_path(rel):
    """Trả đường dẫn tới tài nguyên đi kèm, hoạt động cả khi chạy python lẫn khi đã
    đóng gói bằng PyInstaller (tài nguyên nằm trong sys._MEIPASS / thư mục _internal)."""
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base, rel)

# ================= I. THIẾT LẬP HỆ THỐNG LOGGING KÉP + CMD =================
ui_queue = queue.Queue()

class UILogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        if record.levelno >= logging.ERROR:
            ui_queue.put({"type": "error", "msg": msg})
        else:
            ui_queue.put({"type": "status", "msg": msg})

logger = logging.getLogger("AvbudsAI")
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler('system_debug.log', encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(file_handler)

ui_handler = UILogHandler()
ui_handler.setFormatter(logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(ui_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(console_handler)


# ================= II. THÔNG SỐ HỆ THỐNG & CẤU HÌNH =================
# Version chương trình — hiển thị trên tiêu đề cửa sổ + nhãn trên giao diện.
APP_NAME = "Avbuds AI Secretary"
APP_VERSION = "V23.1"

# Chế độ xử lý tiếng ĐỐI TÁC nghe vào (chống loop khi 2 máy cùng chạy app):
#  - INCOMING_OFF: bỏ hẳn luồng NGHE (chỉ dịch tiếng mình nói ra).
#  - INCOMING_TEXT: chỉ PHIÊN ÂM để HIỂN THỊ chữ đối tác, KHÔNG dịch lại, KHÔNG đọc-to.
#      Dùng khi đối tác CŨNG chạy app: âm nghe được đã là bản dịch sang tiếng tôi → không loop.
#  - INCOMING_TRANSLATE: dịch đầy đủ + hiện (khi đối tác KHÔNG chạy app).
INCOMING_OFF = "Off — only my voice"
INCOMING_TEXT = "Show text (partner also uses app)"
INCOMING_TRANSLATE = "Full translate (partner has no app)"

ctk.set_appearance_mode("light")

COLOR_BG = "#1E293B"
COLOR_PANEL = "#334155"
COLOR_BOX_BG = "#FFFFFF"
COLOR_TEXT_MAIN = "#F8FAFC"
COLOR_TEXT_COMMITTED = "#1E293B"
COLOR_TEXT_DRAFT = "#64748B"
COLOR_VERIFIED = "#2563EB"
COLOR_BTN = "#0D9488"
COLOR_STOP = "#BE123C"
SUB_KEY = "#010203"  # màu khóa cho nền trong suốt của phụ đề (gần như không xuất hiện thật)

client = OpenAI(api_key=config.OPENAI_API_KEY)

# ===== Client cho TÓM TẮT BIÊN BẢN (text) — Gemini free tier hoặc OpenAI =====
# Gemini có endpoint tương thích OpenAI SDK -> chỉ đổi base_url + tên model, giữ nguyên JSON mode.
_use_gemini = (getattr(config, "TEXT_PROVIDER", "openai") == "gemini"
               and getattr(config, "GEMINI_API_KEY", ""))
if _use_gemini:
    text_client = OpenAI(api_key=config.GEMINI_API_KEY,
                         base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    SUM_MODEL_FAST, SUM_MODEL_FINAL = "gemini-2.5-flash-lite", "gemini-2.5-flash"
    logger.info("🟢 [AI] Summary using Gemini (free tier).")
else:
    text_client = client
    SUM_MODEL_FAST, SUM_MODEL_FINAL = "gpt-4o-mini", "gpt-4o"
    if getattr(config, "TEXT_PROVIDER", "openai") == "gemini":
        logger.warning("⚠️ [AI] TEXT_PROVIDER=gemini but GEMINI_API_KEY is missing -> falling back to OpenAI.")

# Giọng Edge neural theo mã ngôn ngữ (TTS free). Đọc bằng giọng ĐÚNG ngôn ngữ của câu.
EDGE_VOICES = {
    "vi": "vi-VN-NamMinhNeural", "en": "en-US-AndrewNeural",
    "ko": "ko-KR-InJoonNeural", "ja": "ja-JP-KeitaNeural", "zh": "zh-CN-YunxiNeural",
}

# Tối đa 4 giọng Edge tiêu biểu mỗi ngôn ngữ (cân bằng nam–nữ) cho người dùng CHỌN.
# (nhãn_hiển_thị, tên_giọng_đầy_đủ). Giọng ĐẦU mỗi list là mặc định (khớp EDGE_VOICES).
EDGE_VOICE_CHOICES = {
    "vi": [("NamMinh (nam)", "vi-VN-NamMinhNeural"), ("HoaiMy (nữ)", "vi-VN-HoaiMyNeural")],
    "ko": [("InJoon (nam)", "ko-KR-InJoonNeural"), ("Hyunsu (nam)", "ko-KR-HyunsuMultilingualNeural"),
           ("SunHi (nữ)", "ko-KR-SunHiNeural")],
    "en": [("Andrew (nam)", "en-US-AndrewNeural"), ("Guy (nam)", "en-US-GuyNeural"),
           ("Ava (nữ)", "en-US-AvaNeural"), ("Jenny (nữ)", "en-US-JennyNeural")],
    "ja": [("Keita (nam)", "ja-JP-KeitaNeural"), ("Nanami (nữ)", "ja-JP-NanamiNeural")],
    "zh": [("Yunxi (nam)", "zh-CN-YunxiNeural"), ("Yunjian (nam)", "zh-CN-YunjianNeural"),
           ("Xiaoxiao (nữ)", "zh-CN-XiaoxiaoNeural"), ("Xiaoyi (nữ)", "zh-CN-XiaoyiNeural")],
}


def edge_voice_labels(lang_code):
    """Danh sách nhãn giọng cho 1 ngôn ngữ (cho dropdown). Rỗng → []."""
    return [lbl for lbl, _ in EDGE_VOICE_CHOICES.get(lang_code, [])]


def edge_voice_from_label(lang_code, label):
    """Nhãn → tên giọng Edge đầy đủ. Không khớp → giọng mặc định của ngôn ngữ."""
    for lbl, voice in EDGE_VOICE_CHOICES.get(lang_code, []):
        if lbl == label:
            return voice
    return EDGE_VOICES.get(lang_code, EDGE_VOICES["en"])


# Giọng Soniox TTS (tts-rt-v1). LƯU Ý: mọi giọng Soniox đều ĐA NGÔN NGỮ (nói được 60+
# thứ tiếng) — nên đây là 3 giọng CHỌN SẴN hợp mỗi ngôn ngữ (cân bằng nam/nữ), giọng nào
# cũng đọc được ngôn ngữ đó. (nhãn_hiển_thị, tên_giọng). Giọng ĐẦU = mặc định.
SONIOX_VOICE_CHOICES = {
    "en": [("Adrian (nam)", "Adrian"), ("Oliver (nam)", "Oliver"), ("Noah (nam)", "Noah"), ("Emma (nữ)", "Emma")],
    "ko": [("Noah (nam)", "Noah"), ("Daniel (nam)", "Daniel"), ("Owen (nam)", "Owen"), ("Mina (nữ)", "Mina")],
    "ja": [("Kenji (nam)", "Kenji"), ("Noah (nam)", "Noah"), ("Owen (nam)", "Owen"), ("Mina (nữ)", "Mina")],
    "zh": [("Owen (nam)", "Owen"), ("Noah (nam)", "Noah"), ("Mason (nam)", "Mason"), ("Grace (nữ)", "Grace")],
    "vi": [("Adrian (nam)", "Adrian"), ("Oliver (nam)", "Oliver"), ("Noah (nam)", "Noah"), ("Emma (nữ)", "Emma")],
}


def _is_soniox_tts():
    return getattr(config, "TTS_PROVIDER", "edge") == "soniox"


def tts_voice_labels(lang_code):
    """Danh sách nhãn giọng theo NHÀ CUNG CẤP đang chọn: Soniox → SONIOX_VOICE_CHOICES,
    còn lại (edge) → EDGE_VOICE_CHOICES."""
    if _is_soniox_tts():
        return [lbl for lbl, _ in SONIOX_VOICE_CHOICES.get(lang_code, SONIOX_VOICE_CHOICES.get("en", []))]
    return edge_voice_labels(lang_code)


def tts_voice_from_label(lang_code, label):
    """Nhãn → tên giọng theo nhà cung cấp đang chọn."""
    if _is_soniox_tts():
        choices = SONIOX_VOICE_CHOICES.get(lang_code) or SONIOX_VOICE_CHOICES.get("en", [])
        for lbl, voice in choices:
            if lbl == label:
                return voice
        return choices[0][1] if choices else (getattr(config, "SONIOX_TTS_VOICE", "") or "Adrian")
    return edge_voice_from_label(lang_code, label)


# Phát hiện đầu cable theo TÊN thiết bị Windows (để gán vai trò & lọc, chống nhầm):
#   • Cable RA (giọng tôi → cuộc họp) = VB-CABLE gốc → tên chứa "virtual cable".
#   • Cable VÀO (đối tác → tôi)        = VB-CABLE A   → tên chứa "cable a"/"cable-a".
def is_out_cable(name):
    n = (name or "").lower()
    return "cable" in n and "virtual cable" in n


def is_in_cable(name):
    n = (name or "").lower()
    return "cable" in n and ("cable a" in n or "cable-a" in n)
# GIỚI HẠN hàng đợi audio (chống TRÀN DỮ LIỆU/trễ dồn khi mạng/STT không theo kịp,
# nhất là khi 2 bên cùng bật app gây loop). Cap ~32 gói (vài giây) rồi BỎ gói CŨ NHẤT
# để giữ độ trễ thấp thay vì phình vô hạn.
AUDIO_Q_MAX = 32
# Kích thước gói thu mic: 1600 frame ≈ 0.1s (Soniox 16k) — nhỏ hơn 4000 cũ để audio tới
# STT sớm hơn, pipeline nhạy hơn (đổi nhẹ, không thêm thư viện).
CAPTURE_FRAMES = 1600
audio_queue = queue.Queue(maxsize=AUDIO_Q_MAX)     # luồng RA: tiếng tôi nói (mic)
audio_queue_in = queue.Queue(maxsize=AUDIO_Q_MAX)  # luồng VÀO: tiếng đối tác (system audio) — chỉ dùng khi 2 chiều


def enqueue_drop_oldest(q, item):
    """Đẩy gói vào hàng đợi có giới hạn; nếu đầy thì VỨT gói cũ nhất rồi đẩy gói mới.
    Dùng cho audio real-time: thà mất chút âm cũ còn hơn để trễ dồn vô hạn (rè/loop)."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try: q.get_nowait()
        except queue.Empty: pass
        try: q.put_nowait(item)
        except queue.Full: pass

is_recording = False
async_loop = None
async_loop_in = None               # event loop riêng cho engine luồng VÀO (2 chiều)


# ================= III. ĐỘNG CƠ REAL-TIME SINGLE-SOCKET (SONIOX UNIFIED) =================

async def soniox_engine(api_key, target_lang="vi", source_lang="auto", audio_q=None, lane="out",
                        translate=True, context=None):
    aq = audio_q if audio_q is not None else audio_queue
    url = "wss://stt-rt.soniox.com/transcribe-websocket"
    try:
        async with websockets.connect(url) as ws:

            cfg = {
                "api_key": api_key, "model": "stt-rt-v5", "audio_format": "pcm_s16le",
                "sample_rate": 16000, "num_channels": 1, "enable_endpoint_detection": True,
            }
            # translate=False → CHỈ phiên âm (STT), không dịch (dùng cho chế độ 'Show text':
            # tiếng nghe được đã là bản dịch sang tiếng tôi nên khỏi dịch lại → không loop).
            if translate:
                cfg["translation"] = {"type": "one_way", "target_language": target_lang}
            # Bộ từ chuyên ngành: terms (boost nhận dạng) + translation_terms (dịch nhất quán).
            if context:
                cfg["context"] = context
            # Gợi ý ngôn ngữ tôi nói -> nhận giọng chính xác hơn (auto = để Soniox tự dò)
            if source_lang and source_lang != "auto":
                cfg["language_hints"] = [source_lang]
            await ws.send(json.dumps(cfg))

            logger.info("🌐 [SONIOX SINGLE-SOCKET] Unified data channel established.")

            async def send_audio():
                while is_recording:
                    try:
                        pcm_data = await asyncio.to_thread(aq.get, timeout=0.5)
                        await ws.send(pcm_data)
                    except queue.Empty: continue
                    except websockets.ConnectionClosed:
                        logger.info("🔌 [SONIOX WS] Send stream: socket closed by server.")
                        break
                    except Exception as e:
                        logger.error(f"[SONIOX WS] Failed to send audio packet: {e}")
                        break

            async def receive_unified():
                try:
                    async for message in ws:
                        if not is_recording: break
                        data = json.loads(message)
                        if "tokens" in data and data["tokens"]:
                            orig_final, orig_prov = "", ""
                            trans_final, trans_prov = "", ""
                            orig_has_final, trans_has_final = False, False

                            for token in data["tokens"]:
                                chunk = clean_protocol_tags(token.get("text", ""))
                                is_trans = token.get("translation_status") == "translation"
                                is_final = token.get("is_final", False)

                                if is_trans:
                                    if is_final:
                                        trans_final += chunk
                                        trans_has_final = True
                                    else: trans_prov += chunk
                                else:
                                    if is_final:
                                        orig_final += chunk
                                        orig_has_final = True
                                    else: orig_prov += chunk

                            if orig_final or orig_prov or trans_final or trans_prov or orig_has_final or trans_has_final:
                                ui_queue.put({
                                    "type": "soniox_unified", "lane": lane,
                                    "orig_final": orig_final, "orig_prov": orig_prov,
                                    "trans_final": trans_final, "trans_prov": trans_prov,
                                    "orig_has_final": orig_has_final, "trans_has_final": trans_has_final
                                })
                except websockets.ConnectionClosed:
                    logger.info("🔌 [SONIOX WS] Receive stream: connection closed cleanly.")
                except Exception as e:
                    logger.error(f"[SONIOX WS] Receive stream error: {e}")

            await asyncio.gather(send_audio(), receive_unified(), return_exceptions=True)
    except Exception as e:
        logger.error(f"[SONIOX SYSTEM] Single-socket connection error: {e}")
    finally:
        if is_recording:
            ui_queue.put({"type": "status", "msg": "🔌 Session ended (session limit reached or timeout)."})


def _soniox_tokens_to_segments(tokens):
    """Chuyển token stream Soniox async (có translation_status) thành 2 list song song:
       segments=[{start, text(gốc)}], translations=[chuỗi dịch] — khớp định dạng process_file."""
    segments, translations = [], []
    cur_orig, cur_trans, cur_start = "", "", None

    def flush():
        nonlocal cur_orig, cur_trans, cur_start
        if cur_orig.strip() or cur_trans.strip():
            segments.append({"start": (cur_start or 0) / 1000.0, "text": cur_orig.strip()})
            translations.append(cur_trans.strip())
        cur_orig, cur_trans, cur_start = "", "", None

    for tk in tokens:
        raw = tk.get("text", "")
        if raw == "<end>":            # mốc ngắt câu (nếu Soniox phát)
            flush()
            continue
        chunk = clean_protocol_tags(raw)
        if tk.get("translation_status") == "translation":
            cur_trans += chunk
        else:
            if cur_trans:             # gốc xuất hiện sau khi đã có bản dịch -> câu mới
                flush()
            if cur_start is None:
                cur_start = tk.get("start_ms", 0)
            cur_orig += chunk
    flush()
    return segments, translations


def soniox_transcribe_file(path, target_code, source_code="auto"):
    """STT + dịch file thu sẵn bằng Soniox async (1 lần gọi, không cần OpenAI).
       Trả (segments, translations) khớp định dạng worker process_file."""
    import httpx
    api_key = config.SONIOX_API_KEY
    base = "https://api.soniox.com/v1"
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=300) as h:
        # 1) Upload file -> file_id
        with open(path, "rb") as f:
            r = h.post(f"{base}/files", headers=headers, files={"file": f})
        r.raise_for_status()
        file_id = r.json()["id"]
        tid = None
        try:
            # 2) Tạo transcription kèm dịch một chiều
            body = {
                "model": "stt-async-v5", "file_id": file_id,
                "translation": {"type": "one_way", "target_language": target_code},
            }
            if source_code and source_code != "auto":
                body["language_hints"] = [source_code]
            r = h.post(f"{base}/transcriptions", headers=headers, json=body)
            r.raise_for_status()
            tid = r.json()["id"]

            # 3) Poll đến khi xong
            while True:
                r = h.get(f"{base}/transcriptions/{tid}", headers=headers)
                r.raise_for_status()
                st = r.json().get("status")
                if st == "completed":
                    break
                if st == "error":
                    raise RuntimeError(r.json().get("error_message", "Soniox async processing error"))
                time.sleep(2)

            # 4) Lấy transcript
            r = h.get(f"{base}/transcriptions/{tid}/transcript", headers=headers)
            r.raise_for_status()
            tokens = r.json().get("tokens", [])
        finally:
            # 5) Dọn dẹp tài nguyên trên server (best-effort)
            try:
                if tid:
                    h.delete(f"{base}/transcriptions/{tid}", headers=headers)
            except Exception:
                pass
            try:
                h.delete(f"{base}/files/{file_id}", headers=headers)
            except Exception:
                pass
    return _soniox_tokens_to_segments(tokens)


async def openai_realtime_engine(api_key, target_lang="vi", target_name="Vietnamese"):
    # Endpoint + model chuẩn của OpenAI Realtime API
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {"Authorization": f"Bearer {api_key}", "OpenAI-Beta": "realtime=v1"}

    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            logger.info("💎 [OPENAI WS] Realtime API connected (Smart Brain: STT + GPT translation).")

            # Cấu hình phiên: bóc lời input (ô GỐC) + chỉ thị model dịch (ô DỊCH), tự ngắt câu bằng server VAD
            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                    "instructions": (
                        f"You are a simultaneous interpreter. Translate the user's speech into {target_name}. "
                        f"Output ONLY the {target_name} translation — no original text, no explanations, no quotes."
                    ),
                    "input_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "gpt-4o-transcribe"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                    },
                },
            }
            await ws.send(json.dumps(session_update))

            async def send_audio():
                while is_recording:
                    try:
                        pcm_data = await asyncio.to_thread(audio_queue.get, timeout=0.5)
                        b64_audio = base64.b64encode(pcm_data).decode("utf-8")
                        await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64_audio}))
                    except queue.Empty: continue
                    except websockets.ConnectionClosed: break
                    except Exception as e:
                        logger.error(f"[OPENAI WS] Failed to send audio packet: {e}")
                        break

            async def receive_text():
                try:
                    async for message in ws:
                        if not is_recording: break
                        data = json.loads(message)
                        et = data.get("type", "")

                        if et == "error":
                            logger.error(f"[OPENAI WS] Server error: {data.get('error', data)}")
                            continue

                        # --- Ô CHỮ GỐC: transcription của input ---
                        if et == "conversation.item.input_audio_transcription.delta":
                            delta = data.get("delta", "")
                            if delta:
                                ui_queue.put({"type": "openai_orig_delta", "text": delta})
                        elif et == "conversation.item.input_audio_transcription.completed":
                            ui_queue.put({"type": "openai_orig_done", "text": data.get("transcript") or ""})

                        # --- Ô CHỮ DỊCH: response text của model ---
                        elif et in ("response.text.delta", "response.output_text.delta", "response.audio_transcript.delta"):
                            delta = data.get("delta", "")
                            if delta:
                                ui_queue.put({"type": "openai_trans_delta", "text": delta})
                        elif et in ("response.text.done", "response.output_text.done", "response.audio_transcript.done"):
                            text = data.get("text") or data.get("transcript") or ""
                            ui_queue.put({"type": "openai_trans_done", "text": text})

                except websockets.ConnectionClosed: pass

            await asyncio.gather(send_audio(), receive_text(), return_exceptions=True)
    except Exception as e:
        logger.error(f"[OPENAI SYSTEM] WebSocket connection crashed: {e}")


# ================= IV. HÀM QUÉT THIẾT BỊ BẰNG SOUNDCARD =================
def get_clean_audio_devices():
    """Quét thiết bị thu, GIỮ NGUYÊN TÊN ĐẦY ĐỦ của Windows để đối chiếu device
    thật. TÁCH 2 NHÓM cho 2 picker riêng (tách biệt logic):
      mic_list  – 🎙️ [MIC] mic vật lý → nguồn LUỒNG NÓI (tiếng tôi → dịch cho đối tác)
      loop_list – 🔊 [LOA] loopback loa → nguồn LUỒNG NGHE (tiếng đối tác → dịch ra sub)
    Trả về (mic_list, loop_list, ui_to_obj_map)."""
    try:
        devices = sc.all_microphones(include_loopback=True)
    except Exception:
        return ["⚠️ No MIC"], ["⚠️ No SPEAKER"], {}
    mic_list, loop_list, ui_to_obj_map = [], [], {}
    for d in devices:
        if d.isloopback:
            ui_string = f"🔊 [SPEAKER] {d.name}"
            loop_list.append(ui_string)
        else:
            ui_string = f"🎙️ [MIC] {d.name}"
            mic_list.append(ui_string)
        ui_to_obj_map[ui_string] = d
    if not mic_list:
        mic_list = ["⚠️ No MIC"]
    if not loop_list:
        loop_list = ["⚠️ No loopback SPEAKER"]
    return mic_list, loop_list, ui_to_obj_map


def pick_default_device(ui_list, default_getter):
    """Chọn mục mặc định thông minh cho dropdown thiết bị.
    - Tránh CABLE/ảo làm mặc định.
    - Với MIC: tránh luôn endpoint Bluetooth 'Chat'/'Communication'/'Hands-Free'
      vì WASAPI thường KHÔNG mở thu được (ném AssertionError rỗng).
    - Ưu tiên thiết bị mặc định của Windows nếu nó vượt qua các bộ lọc trên.
    `default_getter` = 'default_microphone' hoặc 'default_speaker'."""
    if not ui_list:
        return ""
    valid = [u for u in ui_list if not u.startswith("⚠️")] or ui_list
    is_mic = default_getter == "default_microphone"
    bad_kw = ["cable", "virtual"]
    comm_kw = ["chat", "communication", "hands-free", "handsfree"]
    good = [u for u in valid if not any(k in u.lower() for k in bad_kw)]
    # Ưu tiên mặc định Windows nếu "đủ tốt"
    try:
        dname = getattr(sc, default_getter)().name
        cands = [u for u in good if dname and dname in u]
        if is_mic:
            cands = [u for u in cands if not any(k in u.lower() for k in comm_kw)]
        if cands:
            return cands[0]
    except Exception:
        pass
    pool = good or valid
    if is_mic:
        non_comm = [u for u in pool if not any(k in u.lower() for k in comm_kw)]
        pool = non_comm or pool
    return pool[0]


# ===== QUÉT THIẾT BỊ XUẤT (để đẩy giọng dịch vào MIC ảo VB-CABLE) =====
def get_output_devices():
    """Trả về (danh_sách_ui, map_ui->speaker_obj, lựa_chọn_mặc_định).
    GIỮ NGUYÊN TÊN ĐẦY ĐỦ để đối chiếu device thật.
    Ưu tiên chọn sẵn thiết bị có tên chứa 'CABLE' (VB-Audio Virtual Cable)."""
    try:
        speakers = sc.all_speakers()
    except Exception:
        return ["Default Speaker"], {}, "Default Speaker"
    clean_list, ui_map = [], {}
    for s in speakers:
        is_cable = "cable" in s.name.lower()
        ui_string = f"🎧 [VIRTUAL MIC] {s.name}" if is_cable else f"🔈 [SPEAKER] {s.name}"
        clean_list.append(ui_string)
        ui_map[ui_string] = s
    if not clean_list:
        return ["Default Speaker"], {}, "Default Speaker"
    default = next((u for u in clean_list if "[VIRTUAL MIC]" in u), clean_list[0])
    return clean_list, ui_map, default


# Kịch bản giả lập cho chế độ --demo (orig, trans)
DEMO_SCRIPT = [
    ("Hello everyone, let's start the weekly R&D sync.", "Xin chào mọi người, bắt đầu buổi sync R&D hàng tuần."),
    ("The new PCB layout for the ATH driver passed the first test.", "Layout PCB mới cho driver ATH đã qua bài test đầu tiên."),
    ("But the impedance at 2 kHz is still 8 percent too high.", "Nhưng trở kháng ở 2 kHz vẫn cao hơn 8 phần trăm."),
    ("Tu suggests we revise the trace width before the Sony review.", "Tú đề xuất chỉnh lại bề rộng đường mạch trước buổi review với Sony."),
    ("Action item: finalize the BOM by Friday and send the report.", "Việc cần làm: chốt BOM trước thứ Sáu và gửi báo cáo."),
]

# Kịch bản đối tác (luồng VÀO, 2 chiều): tiếng Hàn -> dịch sang tiếng Việt cho tôi nghe
DEMO_SCRIPT_IN = [
    ("네, 안녕하세요. 만나서 반갑습니다.", "Vâng, xin chào. Rất vui được gặp anh."),
    ("임피던스 문제는 우리 쪽에서 다시 확인하겠습니다.", "Vấn đề trở kháng bên chúng tôi sẽ kiểm tra lại."),
    ("금요일까지 보고서를 받을 수 있을까요?", "Liệu thứ Sáu chúng tôi nhận được báo cáo không?"),
]


# ================= V. GIAO DIỆN CHÍNH (APP CLOSURE) =================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        logger.info("=== R&D CRESYN ASSISTANT SYSTEM STARTING ===")
        title = f"{APP_NAME} {APP_VERSION} (Rolling Summary + Floating Sub)"
        if DEMO: title += "  [DEMO]"
        self.title(title)
        self.geometry("1500x950")
        self.configure(fg_color=COLOR_BG)

        self.last_summary_time = 0
        self.ui_to_obj_map = {}

        # Bộ từ vựng chuyên ngành (boost STT + dịch nhất quán + học từ chỗ sửa)
        self.glossary = load_glossary()            # nạp NHANH từ cache/bản đóng gói
        _gc = self.glossary.counts()
        logger.info(f"📚 [GLOSSARY] Nạp {_gc['terms']} thuật ngữ, {_gc['keep_original']} tên riêng, "
                    f"{_gc['corrections']} từ đã học.")
        # Kéo bản MỚI NHẤT từ GitHub ở luồng nền (không chặn UI); có mạng thì tự cập nhật.
        threading.Thread(target=self._refresh_glossary_from_cloud, daemon=True).start()

        # GỢI Ý từ mới trong buổi họp (thuật ngữ chưa có trong glossary)
        self.suggested_terms = {}       # term -> số lần xuất hiện
        self._sugg_seen_out = 0         # con trỏ block luồng NÓI đã quét
        self._sugg_seen_in = 0          # con trỏ block luồng NGHE đã quét
        self._sugg_dismissed = set()    # từ đã bỏ qua (không gợi lại)
        self.glossary_btn = None        # nút "📚..." để gắn badge số gợi ý

        # --- State cho TTS (đọc bản dịch → đẩy vào MIC ảo, hỗ trợ họp 2 chiều) ---
        self.tts_queue = queue.Queue()         # luồng RA: tiếng tôi → MIC ảo (người nghe)
        self.tts_queue_in = queue.Queue()      # luồng VÀO: tiếng đối tác → loa thật (tôi nghe)
        self.tts_spoken_count = 0              # con trỏ block đã đọc (luồng RA)
        self.tts_spoken_count_in = 0           # con trỏ block đã đọc (luồng VÀO)
        self.tts_out_map = {}                  # ui_string -> speaker object (VB-CABLE...)
        self.tts_worker_started = False
        self._tts_in_active = False            # cờ chống loop: True khi đang PHÁT IN-TTS → gate input VÀO
        self._tts_out_active = False           # cờ chống loop: True khi đang PHÁT OUT-TTS (monitor loa) → gate input VÀO

        # --- PRE-SYNTH (giảm trễ TTS): tổng hợp giọng SẴN cho bản dịch tạm khi nó "đứng yên"
        #     (Soniox không đổi provisional ~0.3s ⇒ câu coi như xong). Khi câu CHỐT → phát từ
        #     cache tức thì, giấu ~1–1.5s tổng hợp Edge vào lúc người nói đang nghỉ.
        #     KHÔNG VAD, KHÔNG thư viện mới — chỉ so sánh chuỗi. Vẫn chỉ PHÁT khi chốt → không đọc đôi.
        self._tts_cache = {}                   # norm_text -> audio (np float32 (N,1)) đã synth sẵn
        self._presynth_pending = set()         # norm_text đang synth dở (chống trùng)
        self._presynth_lock = threading.Lock()
        self._prov_last = ""                   # text dịch tạm lần trước (để đo độ ổn định)
        self._prov_since = 0.0                 # mốc thời gian text tạm bắt đầu "đứng yên"
        self._tts_warmed = False               # đã pre-warm kết nối Edge chưa (giảm trễ câu đầu)

        # Engine luồng VÀO (đối tác nói → dịch sang tiếng tôi), chỉ chạy khi bật 2 chiều
        self.engine_in = TranscriptEngine()

        # Lõi logic (đã được kiểm thử trong avbuds_core.py)
        self.engine = TranscriptEngine()
        self.summary = RollingSummary()
        self.is_summarizing = False
        # Transcript GỘP 2 luồng cho SUMMARY, luôn ở ngôn ngữ INPUT (tiếng tôi):
        #   tôi = lời GỐC tôi nói (INPUT) | đối tác = bản DỊCH sang tiếng tôi (INPUT)
        self.summary_feed = []
        self.sum_seen_out = 0          # con trỏ block luồng NÓI đã đưa vào feed
        self.sum_seen_in = 0           # con trỏ block luồng NGHE đã đưa vào feed

        # --- State cho cửa sổ PHỤ ĐỀ NỔI (floating subtitle) ---
        self.subtitle_window = None
        self.sub_orig_label = None
        self.sub_trans_label = None
        # Hàng đợi phụ đề (A+C): chỉ hiện câu ĐÃ CHỐT, giữ mỗi câu đủ lâu để đọc kịp
        self.sub_queue = []            # [(left, right, mine)] chờ hiển thị
        self.sub_seen_out = 0          # con trỏ block luồng NÓI đã đưa vào phụ đề
        self.sub_seen_in = 0           # con trỏ block luồng NGHE đã đưa vào phụ đề
        self.sub_cur = None            # câu đang hiển thị (left, right, mine)
        self.sub_cur_at = 0.0          # mốc thời gian bắt đầu hiển thị câu hiện tại
        self.sub_history = []          # tối đa 3 câu gần nhất (cuốn chiếu)
        self.sub_font_size = 30
        self.sub_text_color = "#FDE047"   # màu chữ dịch (đổi được qua nút 🎨)
        self.sub_mode = "transparent"     # "transparent" (chữ nổi) hoặc "black" (hộp đen)
        self._supports_transparent = True
        self.sub_text_area = None
        self.sub_bar = None
        self._sub_bar_visible = False
        self._sub_hover_after = None
        self._drag_x = 0
        self._drag_y = 0

        try: self.iconbitmap(resource_path("logo.ico"))
        except: pass

        self.build_ui()
        if DEMO:
            logger.info("🧪 DEMO MODE on — press START to feed sample transcript (no mic needed).")
            if MOCK_AI:
                logger.info("🧪 MOCK-AI on — Summary runs simulated, no API calls.")

    def build_ui(self):
        sidebar = ctk.CTkFrame(self, width=330, fg_color=COLOR_PANEL)
        sidebar.pack(side="left", fill="y", padx=10, pady=10)
        sidebar.pack_propagate(False)

        # Khu CÀI ĐẶT cuộn được (nhiều tuỳ chọn nên cho scroll để hiển thị cân đối, không tràn)
        top_side = ctk.CTkScrollableFrame(sidebar, fg_color="transparent", height=560)
        top_side.pack(side="top", fill="x", padx=0, pady=(4, 0))

        try:
            img = Image.open(resource_path("logo.png"))
            ctk.CTkLabel(top_side, image=ctk.CTkImage(img, size=(84, 84)), text="").pack(pady=(6, 2))
        except: pass

        # Nhãn tên + version chương trình (hiển thị rõ trên giao diện)
        ctk.CTkLabel(top_side, text=f"{APP_NAME}  {APP_VERSION}", text_color="#5EEAD4",
                     font=("Arial", 12, "bold")).pack(pady=(0, 6))

        self.mode_var = ctk.StringVar(value="Realtime Mic")
        ctk.CTkSegmentedButton(top_side, variable=self.mode_var, values=["File Processing", "Realtime Mic"], fg_color=COLOR_BTN).pack(pady=(6, 8), padx=20, fill="x")

        # HƯỚNG DỊCH: giữ biến nhưng ẨN UI 2 chiều cho tới khi có cable thứ 2 (chưa dùng được).
        # Mặc định "1 chiều" → app chạy y như cũ. Khi 2 chiều sẵn sàng, bật lại segmented:
        #   ctk.CTkSegmentedButton(top_side, variable=self.direction_var,
        #       values=["1 chiều","2 chiều"], command=self._on_direction_change, ...).pack(...)
        # (code 2 chiều: _build_two_way_frame / _on_direction_change / _pump_tts lane VÀO đã có sẵn.)
        self.direction_var = ctk.StringVar(value="1 chiều")

        mic_list, loop_list, self.ui_to_obj_map = get_clean_audio_devices()
        self._mic_list = mic_list
        def_mic = pick_default_device(mic_list, "default_microphone")
        def_loop = pick_default_device(loop_list, "default_speaker")

        # ── KHỐI 1: THIẾT BỊ ─────────────────────────────────────────────
        ctk.CTkLabel(top_side, text="① DEVICES", text_color="#5EEAD4",
                     font=("Arial", 11, "bold")).pack(pady=(8, 0), padx=20, anchor="w")

        # CHỌN NHANH: 1 thiết bị (tai nghe/loa) → tự điền cả MIC + LOA bên dưới
        self.combo_device_var = ctk.StringVar(value=def_loop)
        ctk.CTkLabel(top_side, text="🎧 Device (quick-pick mic + speaker):",
                     text_color="#FBBF24", font=("Arial", 11, "bold")).pack(pady=(4, 0), padx=20, anchor="w")
        self.combo_device_menu = ctk.CTkOptionMenu(top_side, variable=self.combo_device_var, values=loop_list,
                          command=self._on_combo_device, dynamic_resizing=False,
                          fg_color="#0E7490", button_color="#155E75")
        self.combo_device_menu.pack(pady=2, padx=20, fill="x")
        ctk.CTkLabel(top_side, text="↳ auto-fills the 2 fields below; edit manually if needed.",
                     text_color=COLOR_TEXT_DRAFT, font=("Arial", 9)).pack(padx=20, anchor="w")

        # MIC = nguồn LUỒNG NÓI (tiếng tôi → dịch → đọc cho đối tác)
        self.selected_mic = ctk.StringVar(value=def_mic)
        ctk.CTkLabel(top_side, text="🎤 MIC – MY voice (→ read to partner):",
                     text_color=COLOR_TEXT_DRAFT, font=("Arial", 11)).pack(pady=(6, 0), padx=20, anchor="w")
        self.mic_menu = ctk.CTkOptionMenu(top_side, variable=self.selected_mic, values=mic_list,
                          command=self._note_restart_needed,
                          dynamic_resizing=False, fg_color=COLOR_BTN)
        self.mic_menu.pack(pady=2, padx=20, fill="x")
        # LOA = nguồn LUỒNG NGHE (tiếng đối tác từ Teams → dịch → sub tôi đọc)
        self.selected_sys = ctk.StringVar(value=def_loop)
        ctk.CTkLabel(top_side, text="🔊 SPEAKER – PARTNER's voice (→ translate to sub):",
                     text_color=COLOR_TEXT_DRAFT, font=("Arial", 11)).pack(pady=(6, 0), padx=20, anchor="w")
        self.sys_menu = ctk.CTkOptionMenu(top_side, variable=self.selected_sys, values=loop_list,
                          command=self._note_restart_needed,
                          dynamic_resizing=False, fg_color=COLOR_BTN)
        self.sys_menu.pack(pady=2, padx=20, fill="x")
        # Nút LÀM MỚI: quét lại thiết bị (cắm/rút tai nghe sau khi mở app sẽ hiện ngay)
        ctk.CTkButton(top_side, text="🔄 Refresh devices", command=self.refresh_devices,
                      fg_color="#475569", hover_color="#334155",
                      font=("Arial", 10, "bold")).pack(pady=(4, 2), padx=20, fill="x")

        # ── KHỐI 2: NGÔN NGỮ (INPUT ⇄ OUTPUT, KHÔNG Auto detect) ─────────
        lang_options = ["English", "Korean", "Japanese", "Vietnamese", "Chinese"]
        ctk.CTkLabel(top_side, text="② LANGUAGE", text_color="#5EEAD4",
                     font=("Arial", 11, "bold")).pack(pady=(12, 0), padx=20, anchor="w")
        lang_row = ctk.CTkFrame(top_side, fg_color="transparent")
        lang_row.pack(pady=(4, 0), padx=20, fill="x")
        lang_row.grid_columnconfigure(0, weight=1)
        lang_row.grid_columnconfigure(2, weight=1)
        self.input_lang = ctk.StringVar(value="Vietnamese")
        self.output_lang = ctk.StringVar(value="English")
        # Đổi ngôn ngữ → cập nhật danh sách giọng đọc tương ứng (guard nếu picker chưa tạo)
        self.input_lang.trace_add("write", lambda *a: self._refresh_voice_pickers())
        self.output_lang.trace_add("write", lambda *a: self._refresh_voice_pickers())
        ctk.CTkOptionMenu(lang_row, variable=self.input_lang, values=lang_options,
                          command=self._note_restart_needed,
                          dynamic_resizing=False, width=110, fg_color=COLOR_BTN).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(lang_row, text="⇄", width=34, command=self._swap_langs,
                      fg_color="#475569", hover_color="#334155",
                      font=("Arial", 14, "bold")).grid(row=0, column=1, padx=6)
        ctk.CTkOptionMenu(lang_row, variable=self.output_lang, values=lang_options,
                          command=self._note_restart_needed,
                          dynamic_resizing=False, width=110, fg_color=COLOR_BTN).grid(row=0, column=2, sticky="ew")
        cap_row = ctk.CTkFrame(top_side, fg_color="transparent")
        cap_row.pack(padx=20, fill="x")
        cap_row.grid_columnconfigure(0, weight=1)
        cap_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cap_row, text="INPUT (I speak)", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 9)).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(cap_row, text="OUTPUT (partner)", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 9)).grid(row=0, column=1, sticky="e")

        self.ai_mode_var = ctk.StringVar(value="Default (Soniox)")
        self.ai_mode_menu = ctk.CTkOptionMenu(
            top_side, variable=self.ai_mode_var,
            values=["Default (Soniox)", "Smart Brain (Premium)"],
            command=self._note_restart_needed,
            fg_color="#3B82F6", button_color="#2563EB", button_hover_color="#1D4ED8"
        )
        self.ai_mode_menu.pack(pady=(15, 10), padx=20, fill="x")

        # XỬ LÝ TIẾNG ĐỐI TÁC (chống loop khi 2 máy). Xem hằng INCOMING_* ở đầu file.
        incoming_frame = ctk.CTkFrame(top_side, fg_color="#0F172A", corner_radius=8)
        incoming_frame.pack(pady=(0, 8), padx=20, fill="x")
        ctk.CTkLabel(incoming_frame, text="🎧 Partner's voice:", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 11, "bold")).pack(pady=(8, 0), padx=10, anchor="w")
        self.incoming_mode_var = ctk.StringVar(value=INCOMING_TEXT)
        ctk.CTkOptionMenu(incoming_frame, variable=self.incoming_mode_var,
                          values=[INCOMING_TEXT, INCOMING_TRANSLATE],
                          command=self._note_restart_needed,
                          fg_color=COLOR_BTN, font=("Arial", 10), dynamic_resizing=False).pack(pady=2, padx=10, fill="x")
        ctk.CTkLabel(incoming_frame,
                     text="ℹ️ Show text (default): partner ALSO runs this app → their voice is already\n"
                          "translated to your language, so just transcribe & SHOW it (no re-translate,\n"
                          "no read-aloud) → no loop. Full translate: partner does NOT run the app.",
                     text_color=COLOR_TEXT_DRAFT, font=("Arial", 9), justify="left").pack(pady=(0, 8), padx=10, anchor="w")

        ctk.CTkButton(top_side, text="💾 EXPORT DATA", command=self.export_data, fg_color=COLOR_BTN, font=("Arial", 11, "bold")).pack(pady=5, padx=20, fill="x")

        # NÚT BẬT/TẮT PHỤ ĐỀ NỔI
        ctk.CTkButton(top_side, text="🖥️ Floating Subtitle", command=self.toggle_subtitle,
                      fg_color="#7C3AED", hover_color="#6D28D9", font=("Arial", 11, "bold")).pack(pady=5, padx=20, fill="x")

        # TỪ CHUYÊN NGÀNH (glossary): xem/thêm từ + dạy từ nghe-sai + gợi ý từ mới trong họp
        self.glossary_btn = ctk.CTkButton(top_side, text="📚 Từ chuyên ngành", command=self.open_glossary_dialog,
                      fg_color="#0E7490", hover_color="#155E75", font=("Arial", 11, "bold"))
        self.glossary_btn.pack(pady=5, padx=20, fill="x")

        # ===== ĐỌC BẢN DỊCH CHO NGƯỜI NGHE (TTS → loa thật / MIC ảo) =====
        tts_frame = ctk.CTkFrame(top_side, fg_color="#0F172A", corner_radius=8)
        tts_frame.pack(pady=8, padx=20, fill="x")
        self.tts_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(tts_frame, text="🔊 Read translation to the listener", variable=self.tts_var,
                      command=self._on_tts_toggle,
                      font=("Arial", 11, "bold"), progress_color="#22C55E").pack(pady=(8, 2), padx=10, anchor="w")
        out_list, self.tts_out_map, out_default = get_output_devices()
        self.tts_device_var = ctk.StringVar(value=out_default)
        ctk.CTkLabel(tts_frame, text="🎙️ MIC sent into the meeting (VB-CABLE):", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 10, "bold")).pack(pady=(2, 0), padx=10, anchor="w")
        self.tts_device_menu = ctk.CTkOptionMenu(tts_frame, variable=self.tts_device_var, values=out_list,
                          fg_color=COLOR_BTN, font=("Arial", 10))
        self.tts_device_menu.pack(pady=2, padx=10, fill="x")
        if not any("[VIRTUAL MIC]" in u for u in out_list):
            ctk.CTkLabel(tts_frame,
                         text="⚠️ VB-CABLE not detected. Install the driver and reopen the app to send\nthe translated voice into the meeting (currently plays to real speaker for testing only).",
                         text_color="#F87171", font=("Arial", 9), justify="left").pack(pady=(2, 2), padx=10, anchor="w")
            ctk.CTkButton(tts_frame, text="📥 Install virtual MIC (VB-CABLE)", command=self.install_vbcable,
                          fg_color="#0E7490", hover_color="#155E75", font=("Arial", 10, "bold")).pack(pady=(0, 4), padx=10, fill="x")
        # NHÀ CUNG CẤP GIỌNG (TTS): chọn ngay trên UI → dropdown giọng bên dưới tự đổi theo.
        self._TTS_PROVIDER_LABELS = {"Microsoft Edge (miễn phí)": "edge", "Soniox (trả phí)": "soniox"}
        _cur_prov = getattr(config, "TTS_PROVIDER", "edge")
        _cur_lbl = next((l for l, v in self._TTS_PROVIDER_LABELS.items() if v == _cur_prov),
                        "Microsoft Edge (miễn phí)")
        self.tts_provider_var = ctk.StringVar(value=_cur_lbl)
        ctk.CTkLabel(tts_frame, text="🎛️ Nhà cung cấp giọng (TTS):", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 10, "bold")).pack(pady=(4, 0), padx=10, anchor="w")
        ctk.CTkOptionMenu(tts_frame, variable=self.tts_provider_var,
                          values=list(self._TTS_PROVIDER_LABELS.keys()),
                          command=self._on_tts_provider_change,
                          fg_color="#3B82F6", button_color="#2563EB", font=("Arial", 10)).pack(pady=(2, 4), padx=10, fill="x")

        # GIỌNG đọc cho ĐỐI TÁC (lane RA) — theo provider + ngôn ngữ OUTPUT (đối tác nghe).
        self.tts_voice_var = ctk.StringVar(value="onyx")   # giữ cho nhánh OpenAI dự phòng
        _out_code0 = LANG_CODE_MAP.get(self.output_lang.get(), "en")
        self.tts_voice_out_var = ctk.StringVar(value=(tts_voice_labels(_out_code0) or ["(default)"])[0])
        ctk.CTkLabel(tts_frame, text="🗣️ Voice for the partner:", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 10, "bold")).pack(pady=(2, 0), padx=10, anchor="w")
        self.voice_out_menu = ctk.CTkOptionMenu(tts_frame, variable=self.tts_voice_out_var,
                          values=tts_voice_labels(_out_code0) or ["(default)"],
                          fg_color=COLOR_BTN, font=("Arial", 10))
        self.voice_out_menu.pack(pady=(2, 4), padx=10, fill="x")
        self.tts_speed_var = ctk.StringVar(value="1.25x")
        ctk.CTkLabel(tts_frame, text="⏩ Reading speed:", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 10, "bold")).pack(pady=(2, 0), padx=10, anchor="w")
        ctk.CTkOptionMenu(tts_frame, variable=self.tts_speed_var,
                          values=["1.0x", "1.15x", "1.25x", "1.5x", "1.75x", "2.0x"],
                          fg_color=COLOR_BTN, font=("Arial", 10)).pack(pady=(2, 4), padx=10, fill="x")
        # Monitor: TỰ nghe đúng cái người nghe nhận (phát thêm ra loa thật), không đổi loa mặc định
        # Mặc định TẮT monitor: nếu mic của bạn trùng thiết bị với loa monitor
        # (vd INZONE Buds vừa là mic vừa là loa), tai nghe hở sẽ khiến mic thu
        # lại chính giọng TTS → dịch vòng lại (loop). Người nghe đã nhận qua CABLE
        # nên không cần monitor; bật lại thủ công nếu muốn nghe thử.
        self.tts_monitor_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(tts_frame, text="🎧 I also hear it (monitor real speaker)", variable=self.tts_monitor_var,
                      command=self._on_monitor_toggle,
                      font=("Arial", 10, "bold"), progress_color="#38BDF8").pack(pady=(2, 0), padx=10, anchor="w")
        ctk.CTkLabel(tts_frame,
                     text="ℹ️ Also plays the translation to your real speaker so you can hear it\n"
                          "(does not change the default speaker, does not affect the listener).\n"
                          "Turning it on plays a confirmation beep.\n"
                          "⚠️ If mic == speaker (e.g. INZONE Buds used for both) it can echo/loop —\n"
                          "keep it off, or use a separate speaker different from the mic.",
                     text_color=COLOR_TEXT_DRAFT, font=("Arial", 9), justify="left").pack(pady=(0, 8), padx=10, anchor="w")

        # ===== KHỐI 2 CHIỀU: NGHE LỜI ĐỐI TÁC BẰNG TIẾNG TÔI (đọc to) =====
        # Ẩn khi 1 chiều. Lọc thiết bị theo VAI TRÒ để không chọn nhầm đầu cable.
        self._build_two_way_frame(top_side, loop_list, out_list)

        self.vol_bar = ctk.CTkProgressBar(top_side, progress_color=COLOR_BTN)
        self.vol_bar.set(0); self.vol_bar.pack(pady=(8, 2), padx=20, fill="x")

        ctk.CTkLabel(top_side, text="🎚️ Noise threshold: 0.025", text_color=COLOR_TEXT_MAIN,
                     font=("Arial", 10)).pack(pady=(0, 2), padx=20, anchor="w")
        ctk.CTkButton(top_side, text="🎚️ Calibrate Noise", fg_color="#475569", hover_color="#334155",
                      font=("Arial", 11, "bold")).pack(pady=(0, 10), padx=20, fill="x")

        ctk.CTkLabel(sidebar, text="SYSTEM LOGS (DEBUG):", text_color="#94A3B8", font=("Arial", 11, "bold")).pack(pady=(15, 0))
        self.log_box = ctk.CTkTextbox(sidebar, fg_color="#1E293B", text_color="#2DD4BF", font=("Consolas", 10))
        self.log_box.pack(pady=5, padx=20, fill="both", expand=True)

        self.status_label = ctk.CTkLabel(sidebar, text="💤 Ready.", text_color=COLOR_TEXT_MAIN, font=("Arial", 11, "bold"))
        self.status_label.pack(side="bottom", pady=20)

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(side="right", fill="both", expand=True, padx=10, pady=10)
        main.grid_rowconfigure(0, weight=3); main.grid_rowconfigure(1, weight=1); main.grid_columnconfigure(0, weight=1)

        top_f = ctk.CTkFrame(main, fg_color="transparent")
        top_f.grid(row=0, column=0, sticky="nsew", pady=(0, 10))

        style = {"fg_color": COLOR_BOX_BG, "text_color": COLOR_TEXT_COMMITTED, "font": ("Segoe UI", 13), "wrap": "word"}

        l_box_f = ctk.CTkFrame(top_f, fg_color="transparent")
        l_box_f.pack(side="left", fill="both", expand=True, padx=(0, 5))
        ctk.CTkLabel(l_box_f, text="SOURCE – PARTNER's voice 🎤", text_color=COLOR_TEXT_MAIN, font=("Arial", 12, "bold")).pack(fill="x")
        self.left_box = ctk.CTkTextbox(l_box_f, **style); self.left_box.pack(fill="both", expand=True)

        r_box_f = ctk.CTkFrame(top_f, fg_color="transparent")
        r_box_f.pack(side="right", fill="both", expand=True, padx=(5, 0))
        ctk.CTkLabel(r_box_f, text="TRANSLATION – MY language (🗣️=I speak) ✅", text_color=COLOR_TEXT_MAIN, font=("Arial", 12, "bold")).pack(fill="x")
        self.right_box = ctk.CTkTextbox(r_box_f, **style); self.right_box.pack(fill="both", expand=True)

        self.left_box._textbox.configure(spacing1=2, spacing3=6, padx=14, pady=14)
        self.right_box._textbox.configure(spacing1=2, spacing3=6, padx=14, pady=14)

        bot = ctk.CTkFrame(main, fg_color="transparent")
        bot.grid(row=1, column=0, sticky="nsew")
        ctk.CTkLabel(bot, text="MASTER REPORT (Minutes/Summary/Analysis) 📝", text_color=COLOR_TEXT_MAIN, font=("Arial", 12, "bold")).pack(fill="x")
        self.analysis_box = ctk.CTkTextbox(bot, **style); self.analysis_box.pack(fill="both", expand=True)
        self.analysis_box._textbox.configure(spacing1=4, spacing3=10, padx=14, pady=14)

        acts = ctk.CTkFrame(main, height=60, fg_color="transparent")
        acts.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        # SUMMARY thủ công = bản đánh bóng chất lượng cao (gpt-4o)
        ctk.CTkButton(acts, text="📊 SUMMARY", command=lambda: self.generate_summary(final=True), fg_color="#F59E0B", font=("Arial", 14, "bold"), width=180).pack(side="left")
        ctk.CTkButton(acts, text="🚀 START", command=self.start_engine, fg_color=COLOR_BTN, font=("Arial", 14, "bold"), width=120).pack(side="right", padx=5)
        ctk.CTkButton(acts, text="⏹️ STOP", command=self.stop_engine, fg_color=COLOR_STOP, font=("Arial", 14, "bold"), width=120).pack(side="right", padx=5)

    @staticmethod
    def _probe_mic(dev):
        """Thử mở nhanh 1 thiết bị thu để biết có capture được không.
        Mic Bluetooth HFP sẽ ném AssertionError ngay khi mở (không bắt đầu stream
        nên không gây đổi profile)."""
        for r in (16000, 48000):
            try:
                with dev.recorder(samplerate=r, channels=1):
                    return True
            except Exception:
                continue
        return False

    def _resolve_working_mic(self):
        """Trả về (ui_string, device) cho MIC MỞ ĐƯỢC. Ưu tiên mic đang chọn;
        nếu lỗi thì tự dò mic khác (né CABLE/ảo và endpoint Bluetooth Chat/HFP trước)."""
        chosen_ui = self.selected_mic.get()
        chosen = self.ui_to_obj_map.get(chosen_ui)
        if chosen is not None and self._probe_mic(chosen):
            return chosen_ui, chosen
        mics = [(u, d) for u, d in self.ui_to_obj_map.items() if u.startswith("🎙️ [MIC]")]

        def rank(u):
            ul, s = u.lower(), 0
            if "cable" in ul or "virtual" in ul:
                s += 2
            if any(k in ul for k in ("chat", "communication", "hands-free", "handsfree")):
                s += 1
            return s

        for u, d in sorted(mics, key=lambda x: rank(x[0])):
            if u == chosen_ui:
                continue
            if self._probe_mic(d):
                return u, d
        return None, None

    @staticmethod
    def _resample_mono(mono, src, dst):
        """Resample mono float32 từ src→dst Hz (nội suy tuyến tính, đủ tốt cho STT)."""
        if src == dst or len(mono) == 0:
            return mono
        n_out = max(1, int(round(len(mono) * dst / src)))
        x_old = np.linspace(0.0, 1.0, num=len(mono), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, mono).astype(np.float32)

    def audio_capture_thread(self, device_obj, sample_rate, target_q=None):
        q = target_q if target_q is not None else audio_queue
        lane = "LISTEN/SPK" if target_q is not None else "SPEAK/MIC"
        dev_name = getattr(device_obj, "name", str(device_obj))
        # Thu THẲNG ở tần số engine cần; nếu driver không cho (hay gặp với loopback
        # của loa) thì thu ở 48000/44100 rồi RESAMPLE về cho đúng (tránh sai cao độ).
        attempts = [(sample_rate, sample_rate), (48000, sample_rate), (44100, sample_rate)]
        last_err = None
        for cap_rate, want_rate in attempts:
            try:
                with device_obj.recorder(samplerate=cap_rate, channels=1) as recorder:
                    note = f" → resample {want_rate}Hz" if cap_rate != want_rate else ""
                    logger.info(f"🎙️ [{lane}] Capture OK '{dev_name}' @ {cap_rate}Hz{note}.")
                    while is_recording:
                        data = recorder.record(numframes=CAPTURE_FRAMES)
                        if not is_recording:
                            break
                        # BUG2 — MUTE LUỒNG NÓI: khi TẮT "Đọc bản dịch cho người nghe"
                        # thì coi như tắt hẳn mic của tôi (không thu → không dịch → không
                        # in ra màn hình → không gửi đi). Bật lại là chạy ngay, không cần START lại.
                        if q is audio_queue and not self.tts_var.get():
                            continue
                        # GATE CHỐNG LOOP (đối xứng CẢ 2 luồng): khi đang PHÁT bất kỳ TTS nào
                        # (RA → cable cho người nghe, hoặc VÀO → loa thật cho tôi), tạm NGỪNG thu ở
                        # CẢ luồng NÓI (mic) LẪN NGHE (loopback). Lý do quan trọng khi ĐANG GỌI:
                        # giọng TTS phát đi có thể (a) rò qua tai nghe hở, hoặc (b) VỌNG NGƯỢC về từ
                        # đầu bên kia cuộc gọi (far-end echo) rồi được app họp phát ra LOA THẬT →
                        # nếu mic NÓI thu lại đúng lúc đó, nó tưởng là câu mới → dịch & đọc vòng lại
                        # → LOOP "tự nói lại". Trước đây chỉ gate luồng NGHE nên mic NÓI vẫn thu được
                        # echo và gây loop. Đuôi im lặng (tail) trong _tts_play_worker giữ cổng đóng
                        # thêm 1 nhịp để né phần đuôi + echo vọng về.
                        if self._tts_in_active or self._tts_out_active:
                            continue
                        mono = data[:, 0].astype(np.float32)
                        if cap_rate != want_rate:
                            mono = self._resample_mono(mono, cap_rate, want_rate)
                        enqueue_drop_oldest(q, (mono * 32767).astype(np.int16).tobytes())
                return  # thoát sạch khi dừng ghi
            except Exception as e:
                last_err = e
                logger.error(f"[AUDIO CAPTURE ERROR][{lane}] '{dev_name}' @ {cap_rate}Hz: {repr(e)}")
                if not is_recording:
                    return
        hint = ("👉 This device could NOT be captured (e.g. a Bluetooth headset in Chat/HFP "
                "mode not ready). Pick another MIC in the '🎤 MIC' list and START again."
                if lane.startswith("SPEAK") else
                "👉 This SPEAKER cannot capture loopback. Pick another SPEAKER (one currently playing) and START again.")
        logger.error(f"[AUDIO CAPTURE ERROR][{lane}] Giving up on '{dev_name}' — all rates failed "
                     f"({repr(last_err)}). {hint}")

    def demo_feeder(self):
        """Bơm transcript mẫu vào ui_queue để test toàn bộ pipeline mà không cần mic/API."""
        logger.info("🧪 [DEMO] Feeding sample transcript...")
        for orig, trans in DEMO_SCRIPT:
            if not is_recording: break
            wo, wt = orig.split(), trans.split()
            steps = max(len(wo), len(wt), 1)
            # Giả lập chữ chạy provisional (chưa chốt)
            for i in range(steps):
                if not is_recording: break
                ui_queue.put({
                    "type": "soniox_unified",
                    "orig_final": "", "orig_prov": " ".join(wo[:i + 1]),
                    "trans_final": "", "trans_prov": " ".join(wt[:i + 1]),
                    "orig_has_final": False, "trans_has_final": False,
                })
                time.sleep(0.12)
            if not is_recording: break
            # Chốt câu (final)
            ui_queue.put({
                "type": "soniox_unified",
                "orig_final": orig + " ", "orig_prov": "",
                "trans_final": trans + " ", "trans_prov": "",
                "orig_has_final": True, "trans_has_final": True,
            })
            time.sleep(0.7)
        ui_queue.put({"type": "status", "msg": "🧪 [DEMO] Sample script finished (press SUMMARY to test summarization)."})

    def demo_feeder_in(self):
        """Bơm transcript đối tác (luồng VÀO) để test chế độ 2 chiều — Hàn → Việt."""
        time.sleep(1.6)  # lệch pha với luồng RA cho giống hội thoại qua lại
        for orig, trans in DEMO_SCRIPT_IN:
            if not is_recording: break
            wo, wt = orig.split(), trans.split()
            for i in range(max(len(wo), len(wt), 1)):
                if not is_recording: break
                ui_queue.put({
                    "type": "soniox_unified", "lane": "in",
                    "orig_final": "", "orig_prov": " ".join(wo[:i + 1]),
                    "trans_final": "", "trans_prov": " ".join(wt[:i + 1]),
                    "orig_has_final": False, "trans_has_final": False,
                })
                time.sleep(0.13)
            if not is_recording: break
            ui_queue.put({
                "type": "soniox_unified", "lane": "in",
                "orig_final": orig + " ", "orig_prov": "",
                "trans_final": trans + " ", "trans_prov": "",
                "orig_has_final": True, "trans_has_final": True,
            })
            time.sleep(2.2)

    # ================= XỬ LÝ FILE (Whisper batch + GPT dịch) =================
    def process_file(self):
        target_name = self.output_lang.get()
        if target_name == "Auto detect":
            target_name = "English"

        path = None
        if not MOCK_AI:
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                title="Select audio file",
                filetypes=[("Audio", "*.mp3 *.wav *.m4a *.mp4 *.webm *.ogg *.flac"), ("All files", "*.*")],
            )
            if not path:
                self.status_label.configure(text="💤 Ready.", text_color=COLOR_TEXT_MAIN)
                return

        self.status_label.configure(text="📂 Processing file...", text_color="#FBBF24")
        self.log_box.insert("end", f"📂 Started processing file: {path or '(MOCK demo)'}\n")
        self.log_box.see("end")

        def worker():
            try:
                if MOCK_AI:
                    time.sleep(0.5)
                    segments = [{"start": i * 5.0, "text": o} for i, (o, t) in enumerate(DEMO_SCRIPT)]
                    translations = [t for (o, t) in DEMO_SCRIPT]
                elif getattr(config, "FILE_STT_PROVIDER", "openai") == "soniox":
                    # Soniox async: STT + dịch trong cùng 1 lần gọi (rẻ, không tốn OpenAI)
                    segments, translations = soniox_transcribe_file(
                        path, LANG_CODE_MAP.get(target_name, "en"))
                else:
                    with open(path, "rb") as f:
                        tr = client.audio.transcriptions.create(
                            model="whisper-1", file=f, response_format="verbose_json"
                        )
                    segments = self._extract_segments(tr)
                    translations = self._translate_segments([s["text"] for s in segments], target_name)

                for i, seg in enumerate(segments):
                    orig = (seg.get("text") or "").strip()
                    trans = (translations[i] if i < len(translations) else "").strip()
                    if not orig and not trans:
                        continue
                    self.engine.committed_blocks.append({
                        "time": seconds_to_mmss(seg.get("start", 0.0)),
                        "orig": orig, "trans": trans,
                    })
                    if trans:
                        self.engine.full_transcript.append(trans)

                self.after(0, self._render_static_done, path)
            except Exception as e:
                logger.error(f"[FILE] File processing error: {e}")
                self.after(0, lambda: self.status_label.configure(text="FILE ERROR", text_color=COLOR_STOP))

        threading.Thread(target=worker, daemon=True).start()

    def _extract_segments(self, tr):
        """Lấy danh sách {start, text} từ kết quả Whisper verbose_json (object hoặc dict)."""
        segs = getattr(tr, "segments", None)
        if segs is None and isinstance(tr, dict):
            segs = tr.get("segments")
        out = []
        for s in (segs or []):
            start = getattr(s, "start", None)
            text = getattr(s, "text", None)
            if isinstance(s, dict):
                start = s.get("start", start)
                text = s.get("text", text)
            out.append({"start": start or 0.0, "text": text or ""})
        if not out:  # fallback: không có segment -> dùng toàn văn
            full = getattr(tr, "text", None) or (tr.get("text") if isinstance(tr, dict) else "")
            out = [{"start": 0.0, "text": full or ""}]
        return out

    def _translate_segments(self, texts, target_name):
        if not texts:
            return []
        sys_prompt = (
            f"Translate each string in the JSON 'segments' array into {target_name}. "
            f"Return JSON with key 'translations' as an array of the SAME length and order. "
            f"Only the translations, no extra commentary."
        )
        resp = client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps({"segments": texts}, ensure_ascii=False)},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("translations", [])

    def _render_static_done(self, path):
        left, _ = self.engine.left_lines()
        self.left_box.delete("1.0", "end"); self.left_box.insert("end", left)
        right, _ = self.engine.right_lines("")
        self.right_box.delete("1.0", "end"); self.right_box.insert("end", right)
        n = len(self.engine.committed_blocks)
        self.status_label.configure(text="✅ File processing done", text_color="#A7F3D0")
        self.log_box.insert("end", f"✅ Done: {n} segments. Press 📊 SUMMARY to summarize, 💾 EXPORT to save.\n")
        self.log_box.see("end")

    # ================= EXPORT DATA (Markdown) =================
    def export_data(self):
        if not self.engine.committed_blocks and not self.summary.report:
            self.log_box.insert("end", "⚠️ No data to export yet.\n")
            self.log_box.see("end")
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="Save Markdown minutes",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
            initialfile=time.strftime("Avbuds_Report_%Y%m%d_%H%M.md"),
        )
        if not path:
            return
        try:
            md = build_markdown(
                self.engine.committed_blocks, self.summary.report,
                meta={"output_lang": self.output_lang.get(),
                      "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(md)
            self.log_box.insert("end", f"💾 Exported: {path}\n")
            self.log_box.see("end")
            self.status_label.configure(text="💾 Exported", text_color="#A7F3D0")
        except Exception as e:
            logger.error(f"[EXPORT] File write error: {e}")

    def start_engine(self):
        global is_recording, async_loop
        if is_recording: return

        selected_mode = self.ai_mode_var.get()
        logger.info("==================================================================")
        logger.info(f"🚀 STARTING MULTI-RATE SYNC SYSTEM {APP_VERSION}")
        logger.info("==================================================================")

        self.left_box.delete("1.0", "end")
        self.right_box.delete("1.0", "end")
        self.engine.reset()
        self.engine_in.reset()
        self.summary.reset()
        self.tts_spoken_count = 0      # bắt đầu phiên mới: không đọc lại block cũ
        self.tts_spoken_count_in = 0
        # Reset hàng đợi phụ đề (A+C)
        self.sub_queue.clear()
        self.sub_seen_out = 0
        self.sub_seen_in = 0
        self.sub_cur = None
        self.sub_cur_at = 0.0
        self.sub_history = []
        # Reset transcript gộp cho summary
        self.summary_feed = []
        self.sum_seen_out = 0
        self.sum_seen_in = 0
        # Reset gợi ý từ mới cho phiên mới (giữ danh sách đã bỏ qua để không gợi lại)
        self.suggested_terms = {}
        self._sugg_seen_out = 0
        self._sugg_seen_in = 0
        self._update_glossary_badge()
        # Reset cache/độ-ổn-định cho pre-synth TTS (không giữ giọng synth của phiên cũ)
        with self._presynth_lock:
            self._tts_cache.clear()
            self._presynth_pending.clear()
        self._prov_last = ""
        self._prov_since = 0.0

        # Drain mọi hàng đợi tồn (audio + TTS) để phiên mới KHÔNG đọc lại đồ phiên cũ
        tts_qs = [self.tts_queue, self.tts_queue_in,
                  getattr(self, "tts_play_q", None), getattr(self, "tts_play_q_in", None)]
        for q in (audio_queue, audio_queue_in, *[x for x in tts_qs if x is not None]):
            while not q.empty():
                try: q.get_nowait()
                except: break

        # CHẾ ĐỘ XỬ LÝ FILE (batch, không dùng mic/websocket realtime)
        if self.mode_var.get() == "File Processing":
            self.process_file()
            return

        is_recording = True
        self.last_summary_time = time.time()

        if DEMO:
            logger.info("🧪 DEMO MODE — skipping hardware & websocket.")
            threading.Thread(target=self.demo_feeder, daemon=True).start()       # LUỒNG NÓI
            threading.Thread(target=self.demo_feeder_in, daemon=True).start()    # LUỒNG NGHE
        else:
            # === LUỒNG NÓI: MIC (tiếng tôi) → dịch sang OUTPUT → TTS đọc cho đối tác ===
            # Kiểm tra MIC mở được không; nếu không (vd Bluetooth HFP) → tự chuyển mic chạy được
            mic_ui, mic_obj = self._resolve_working_mic()
            if mic_obj is None:
                logger.error("[SPEAK] Could not open any MIC for capture — check your audio devices.")
                is_recording = False
                return
            if mic_ui != self.selected_mic.get():
                logger.error(f"[SPEAK] MIC '{self.selected_mic.get()}' could NOT capture (Bluetooth "
                             f"headset in Chat/HFP mode). → Auto-switching to '{mic_ui}'. "
                             f"💡 Tip: use the laptop MIC for your voice and keep the Bluetooth headset "
                             f"for LISTENING only — better quality and no lock-up.")
                self.selected_mic.set(mic_ui)
            # TỰ ĐỘNG ĐỔI TẦN SỐ MẪU: Soniox = 16000Hz, OpenAI Custom = 24000Hz
            current_rate = 16000 if "Default" in selected_mode else 24000
            threading.Thread(target=self.audio_capture_thread, args=(mic_obj, current_rate), daemon=True).start()
            threading.Thread(target=self.run_async_loop, args=(selected_mode,), daemon=True).start()

            # === LUỒNG NGHE: xử lý tiếng đối tác theo 3 chế độ (chống loop khi 2 máy) ===
            # self._incoming_translate: None=không chạy, False=STT-only (show text), True=dịch đầy đủ.
            mode_in = self.incoming_mode_var.get()
            self._incoming_translate = None
            sys_obj = None
            if mode_in == INCOMING_OFF:
                logger.info("🚫 [LISTEN] Off — only translating my own voice (loop-safe for two machines).")
            elif mode_in == INCOMING_TEXT:
                # ĐỐI TÁC cũng chạy app → âm nghe được ĐÃ là tiếng tôi. Chỉ STT để HIỆN chữ,
                # KHÔNG dịch lại, KHÔNG đọc-to, KHÔNG bơm cable → không loop.
                sys_obj = self.ui_to_obj_map.get(self.selected_sys.get(), None)
                if sys_obj is None:
                    logger.info("ℹ️ [LISTEN] Show-text: no valid SPEAKER selected — skipping partner display.")
                else:
                    self._incoming_translate = False
                    logger.info("👂 [LISTEN] Show-text mode: transcribe partner (already in my language) "
                                "for DISPLAY only — no re-translate, no read-aloud → loop-safe.")
            else:  # INCOMING_TRANSLATE — đối tác KHÔNG chạy app
                # 2 chiều: nguồn = Cable VÀO (Cable A) để KHÔNG nghe giọng gốc + chống loop.
                # 1 chiều: giữ nguyên như cũ (loopback loa đã chọn).
                two_way = self.direction_var.get() == "2 chiều"
                if two_way:
                    self._ensure_tts_worker()
                    sys_ui = self.cable_in_var.get()
                    sys_obj = self.ui_to_obj_map.get(sys_ui, None)
                    if sys_obj is None:
                        logger.error("[LISTEN-2way] No 'Inbound Cable' (Cable A) yet. Install the 2nd cable "
                                     "and select it in the two-way section — the listen lane will NOT run.")
                    else:
                        self._incoming_translate = True
                        logger.info(f"👂 [LISTEN-2way] Capturing partner from '{sys_ui}' → reading aloud in my language.")
                else:
                    sys_obj = self.ui_to_obj_map.get(self.selected_sys.get(), None)
                    if sys_obj is None:
                        logger.info("ℹ️ [LISTEN] No valid SPEAKER selected — running the SPEAK lane only.")
                    else:
                        self._incoming_translate = True
                        logger.info("👂 [LISTEN] Full-translate partner from SPEAKER → sub.")
            if sys_obj is not None:
                threading.Thread(target=self.audio_capture_thread, args=(sys_obj, 16000, audio_queue_in), daemon=True).start()
                threading.Thread(target=self.run_async_loop_in, daemon=True).start()

        self.status_label.configure(text="🎙️ Listening...", text_color="#FBBF24")
        self.update_ui_loop()

    def run_async_loop(self, mode):
        global async_loop
        async_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(async_loop)

        # LUỒNG NÓI: nguồn = INPUT (tôi nói), đích = OUTPUT (đối tác nghe)
        in_code = LANG_CODE_MAP.get(self.input_lang.get(), "en")
        out_code = LANG_CODE_MAP.get(self.output_lang.get(), "en")

        if "Default" in mode:
            soniox_key = getattr(config, 'SONIOX_API_KEY', config.OPENAI_API_KEY)
            ctx = self.glossary.build_soniox_context(source_code=in_code, target_code=out_code, translate=True)
            async_loop.run_until_complete(
                soniox_engine(soniox_key, target_lang=out_code, source_lang=in_code, lane="out", context=ctx)
            )
        else:
            async_loop.run_until_complete(
                openai_realtime_engine(config.OPENAI_API_KEY, target_lang=out_code,
                                       target_name=self.output_lang.get())
            )

    def run_async_loop_in(self):
        """LUỒNG NGHE: tiếng đối tác từ LOA (loopback) → dịch sang tiếng TÔI → hiện sub.
        Nguồn = OUTPUT (ngôn ngữ đối tác), đích = INPUT (ngôn ngữ tôi). Luôn dùng Soniox,
        đọc từ audio_queue_in, gắn nhãn lane='in'. KHÔNG đọc TTS (chỉ hiển thị)."""
        global async_loop_in
        async_loop_in = asyncio.new_event_loop()
        asyncio.set_event_loop(async_loop_in)

        in_code = LANG_CODE_MAP.get(self.input_lang.get(), "en")    # tôi (đích)
        out_code = LANG_CODE_MAP.get(self.output_lang.get(), "en")  # đối tác (nguồn)
        soniox_key = getattr(config, 'SONIOX_API_KEY', config.OPENAI_API_KEY)
        if getattr(self, "_incoming_translate", True) is False:
            # SHOW-TEXT: âm nghe được ĐÃ là tiếng tôi (đối tác đã dịch) → chỉ phiên âm để hiện chữ.
            ctx = self.glossary.build_soniox_context(source_code=in_code, target_code=in_code, translate=False)
            async_loop_in.run_until_complete(
                soniox_engine(soniox_key, source_lang=in_code, audio_q=audio_queue_in,
                              lane="in", translate=False, context=ctx)
            )
        else:
            ctx = self.glossary.build_soniox_context(source_code=out_code, target_code=in_code, translate=True)
            async_loop_in.run_until_complete(
                soniox_engine(soniox_key, target_lang=in_code, source_lang=out_code,
                              audio_q=audio_queue_in, lane="in", context=ctx)
            )

    @staticmethod
    def _dev_family(ui_string):
        """Lấy 'họ thiết bị' = phần trong ngoặc cuối, vd 'INZONE Buds - Chat'."""
        m = re.findall(r"\(([^)]*)\)", ui_string or "")
        return m[-1].strip().lower() if m else ""

    def _match_mic_for_speaker(self, speaker_ui):
        """Tìm MIC cùng 'họ thiết bị' với loa đã chọn (vd loa INZONE → mic INZONE)."""
        target = self._dev_family(speaker_ui)
        if not target:
            return None
        mics = [u for u in getattr(self, "_mic_list", []) if u.startswith("🎙️ [MIC]")]
        # khớp chính xác họ thiết bị
        for u in mics:
            if self._dev_family(u) == target:
                return u
        # khớp nới lỏng: ≥2 token chung trong ngoặc (vd 'inzone' + 'buds')
        tgt = set(target.replace("-", " ").split())
        for u in mics:
            if len(tgt & set(self._dev_family(u).replace("-", " ").split())) >= 2:
                return u
        return None

    def _note_restart_needed(self, *_):
        """Cài đặt ở cấp ENGINE (mic/loa/ngôn ngữ/AI mode) chỉ nạp lúc START. Nếu đổi
        khi ĐANG ghi → báo rõ phải STOP rồi START để áp dụng (tránh tưởng app treo cài đặt).
        Khi CHƯA ghi → không cần làm gì, lựa chọn sẽ tự được dùng ở lần START kế."""
        if is_recording:
            logger.info("⚠️ Settings changed (MIC/SPEAKER/Language/Engine). "
                        "Press ⏹️ STOP then 🚀 START again to apply to the running session.")

    def refresh_devices(self):
        """Quét LẠI toàn bộ thiết bị âm thanh và cập nhật mọi dropdown (mic/loa/TTS/cable).
        Khắc phục: cắm/rút tai nghe hoặc đổi thiết bị Windows sau khi mở app mà danh sách cũ."""
        try:
            mic_list, loop_list, self.ui_to_obj_map = get_clean_audio_devices()
            self._mic_list = mic_list
            out_list, self.tts_out_map, out_default = get_output_devices()
        except Exception as e:
            logger.error(f"[DEVICE] Could not rescan devices: {e}")
            return

        def _set(menu, var, values, default=None):
            """Cập nhật values cho menu; nếu lựa chọn hiện KHÔNG còn → đổi về default hợp lệ."""
            if menu is None:
                return
            try: menu.configure(values=values)
            except Exception: pass
            if var.get() not in values:
                var.set(default if default in values else (values[0] if values else ""))

        _set(getattr(self, "combo_device_menu", None), self.combo_device_var, loop_list,
             pick_default_device(loop_list, "default_speaker"))
        _set(getattr(self, "mic_menu", None), self.selected_mic, mic_list,
             pick_default_device(mic_list, "default_microphone"))
        _set(getattr(self, "sys_menu", None), self.selected_sys, loop_list,
             pick_default_device(loop_list, "default_speaker"))
        _set(getattr(self, "tts_device_menu", None), self.tts_device_var, out_list, out_default)
        # Khối 2 chiều (nếu đã dựng): cable VÀO + loa thật
        if getattr(self, "cable_in_var", None) is not None:
            in_cables = [u for u in loop_list if is_in_cable(u)] or ["⚠️ No inbound cable (Cable A)"]
            _set(getattr(self, "cable_in_menu", None), self.cable_in_var, in_cables, in_cables[0])
        if getattr(self, "speaker_real_var", None) is not None:
            real_spk = [u for u in out_list if "cable" not in u.lower()] or ["Default Speaker"]
            _set(getattr(self, "speaker_real_menu", None), self.speaker_real_var, real_spk, real_spk[0])

        logger.info(f"🔄 Devices refreshed: {len(mic_list)} mic, {len(loop_list)} speaker, {len(out_list)} output.")
        if is_recording:
            logger.info("⚠️ Recording — device changes apply after STOP then START again.")

    def _on_combo_device(self, choice):
        """Chọn nhanh 1 thiết bị → tự điền ô LOA (loopback) + ô MIC (mic cùng họ).
        Nếu mic cùng họ không mở được (vd Bluetooth HFP), khi START sẽ tự nhảy
        sang mic laptop (cơ chế _resolve_working_mic)."""
        self.selected_sys.set(choice)
        mic = self._match_mic_for_speaker(choice)
        if mic:
            self.selected_mic.set(mic)
            logger.info(f"🎧 Quick-pick: SPEAKER + MIC by '{self._dev_family(choice) or choice}'.")
        else:
            logger.info(f"🎧 Quick-pick SPEAKER '{choice}'. No matching mic found — keeping current MIC.")
        self._note_restart_needed()

    def _swap_langs(self):
        """Đảo nhanh INPUT ⇄ OUTPUT (ngôn ngữ tôi ⇄ ngôn ngữ đối tác)."""
        a, b = self.input_lang.get(), self.output_lang.get()
        self.input_lang.set(b)
        self.output_lang.set(a)

    # ================= V.d 2 CHIỀU: NGHE LỜI ĐỐI TÁC BẰNG TIẾNG TÔI =================
    def _build_two_way_frame(self, parent, loop_list, out_list):
        """Khối UI chỉ hiện ở 2 chiều. Lọc thiết bị theo VAI TRÒ (chỉ hiện đúng cable)
        để không chọn nhầm đầu cable. Mặc định 1 chiều → KHÔNG pack (ẩn)."""
        f = ctk.CTkFrame(parent, fg_color="#16213A", corner_radius=8)
        self.two_way_frame = f
        ctk.CTkLabel(f, text="🎧 TWO-WAY LISTEN (partner → my language, read aloud)",
                     text_color="#C4B5FD", font=("Arial", 11, "bold")).pack(pady=(8, 2), padx=10, anchor="w")

        self.listen_tts_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(f, text="🔊 Read the partner's words to me", variable=self.listen_tts_var,
                      font=("Arial", 10, "bold"), progress_color="#22C55E").pack(pady=(2, 4), padx=10, anchor="w")

        # ② Cable VÀO (loopback Cable A) — CHỈ hiện cable VÀO
        in_cables = [u for u in loop_list if is_in_cable(u)]
        self.cable_in_var = ctk.StringVar(value=(in_cables or ["⚠️ No inbound cable (Cable A)"])[0])
        ctk.CTkLabel(f, text="② Inbound Cable — meeting → me:", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 10, "bold")).pack(pady=(2, 0), padx=10, anchor="w")
        self.cable_in_menu = ctk.CTkOptionMenu(f, variable=self.cable_in_var,
                      values=in_cables or ["⚠️ No inbound cable (Cable A)"], dynamic_resizing=False,
                      fg_color=COLOR_BTN, font=("Arial", 10))
        self.cable_in_menu.pack(pady=2, padx=10, fill="x")

        # Loa THẬT để TÔI nghe bản dịch (loại trừ cable)
        real_spk = [u for u in out_list if "cable" not in u.lower()]
        self.speaker_real_var = ctk.StringVar(value=(real_spk or ["Default Speaker"])[0])
        ctk.CTkLabel(f, text="🔈 Speaker I hear the translation on:", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 10, "bold")).pack(pady=(2, 0), padx=10, anchor="w")
        self.speaker_real_menu = ctk.CTkOptionMenu(f, variable=self.speaker_real_var,
                      values=real_spk or ["Default Speaker"], dynamic_resizing=False,
                      fg_color=COLOR_BTN, font=("Arial", 10))
        self.speaker_real_menu.pack(pady=2, padx=10, fill="x")

        # Giọng đọc cho TÔI (theo input_lang)
        _in_code0 = LANG_CODE_MAP.get(self.input_lang.get(), "vi")
        self.tts_voice_in_var = ctk.StringVar(value=(tts_voice_labels(_in_code0) or ["(default)"])[0])
        ctk.CTkLabel(f, text="🗣️ Voice for me:", text_color=COLOR_TEXT_DRAFT,
                     font=("Arial", 10, "bold")).pack(pady=(2, 0), padx=10, anchor="w")
        self.voice_in_menu = ctk.CTkOptionMenu(f, variable=self.tts_voice_in_var,
                      values=tts_voice_labels(_in_code0) or ["(default)"],
                      fg_color=COLOR_BTN, font=("Arial", 10))
        self.voice_in_menu.pack(pady=2, padx=10, fill="x")

        # Cảnh báo thiếu Cable VÀO + nút cài cable thứ 2 (pack động trong _refresh_cable_warn)
        self._twoway_warn = ctk.CTkLabel(f, text="", text_color="#F87171",
                                         font=("Arial", 9), justify="left")
        self.install_cable2_btn = ctk.CTkButton(f, text="📥 Install 2nd cable (for two-way)",
                     command=self.install_cable2, fg_color="#0E7490", hover_color="#155E75",
                     font=("Arial", 10, "bold"))

        ctk.CTkLabel(f, text=("➤ In the meeting app (Zoom/Teams):\n"
                              "   • Speaker = CABLE-A Input (Cable A)\n"
                              "   • Mic = CABLE Output (Virtual Cable)\n"
                              "→ Hear only the translation, not the original voice, no echo."),
                     text_color="#7DD3FC", font=("Arial", 9), justify="left").pack(pady=(4, 8), padx=10, anchor="w")

    def _on_direction_change(self, _value=None):
        """Đổi 1 chiều ⇄ 2 chiều: hiện/ẩn khối 2 chiều + cập nhật giọng + cảnh báo cable."""
        two = self.direction_var.get() == "2 chiều"
        self._refresh_voice_pickers()
        if two:
            self._refresh_cable_warn()
            try:
                self.two_way_frame.pack(before=self.vol_bar, fill="x", padx=20, pady=(4, 8))
            except Exception:
                self.two_way_frame.pack(fill="x", padx=20, pady=(4, 8))
            self._ensure_tts_worker()
            logger.info("🔁 TWO-WAY mode: read the partner's words aloud in your language (no need to read the sub).")
        else:
            self.two_way_frame.pack_forget()
            logger.info("➡️ ONE-WAY mode (unchanged behavior).")

    def _on_tts_provider_change(self, _label=None):
        """Đổi nhà cung cấp TTS ngay trên UI (Edge/Soniox). Cập nhật config runtime → engine dùng
        liền (không cần START lại), và làm mới dropdown giọng cho đúng bộ giọng của provider."""
        prov = self._TTS_PROVIDER_LABELS.get(self.tts_provider_var.get(), "edge")
        config.TTS_PROVIDER = prov
        self._refresh_voice_pickers()
        if prov == "soniox":
            logger.info("🎛️ TTS = Soniox (tts-rt-v1, trả phí ~$0.70/giờ, dùng key Soniox sẵn có).")
        else:
            logger.info("🎛️ TTS = Microsoft Edge (miễn phí).")

    def _refresh_voice_pickers(self, *_):
        """Cập nhật danh sách giọng theo ngôn ngữ: RA=output_lang, VÀO=input_lang."""
        if getattr(self, "voice_out_menu", None) is None:
            return
        out_code = LANG_CODE_MAP.get(self.output_lang.get(), "en")
        out_labels = tts_voice_labels(out_code) or ["(default)"]
        self.voice_out_menu.configure(values=out_labels)
        if self.tts_voice_out_var.get() not in out_labels:
            self.tts_voice_out_var.set(out_labels[0])
        if getattr(self, "voice_in_menu", None) is not None:
            in_code = LANG_CODE_MAP.get(self.input_lang.get(), "vi")
            in_labels = tts_voice_labels(in_code) or ["(default)"]
            self.voice_in_menu.configure(values=in_labels)
            if self.tts_voice_in_var.get() not in in_labels:
                self.tts_voice_in_var.set(in_labels[0])

    def _refresh_cable_warn(self):
        """Hiện cảnh báo + nút cài nếu CHƯA có Cable VÀO (Cable A); ẩn nếu đã có."""
        has_in = any(is_in_cable(u) for u in self.ui_to_obj_map)
        if has_in:
            self._twoway_warn.pack_forget()
            self.install_cable2_btn.pack_forget()
        else:
            self._twoway_warn.configure(
                text="⚠️ No 2nd cable (Cable A) for the INBOUND lane.\n"
                     "Install it, then RESTART the PC & reopen the app.")
            self._twoway_warn.pack(pady=(2, 2), padx=10, anchor="w")
            self.install_cable2_btn.pack(pady=(0, 6), padx=10, fill="x")

    def install_cable2(self):
        """Cài cable thứ 2 (VB-CABLE A+B) cho 2 chiều. Ưu tiên installer đóng gói trong
        drivers/VBCABLE_A_B_Setup_x64.exe; không có → mở trang tải VB-Audio."""
        import webbrowser
        installer = resource_path(os.path.join("drivers", "VBCABLE_A_B_Setup_x64.exe"))
        if os.path.exists(installer):
            logger.info("📥 Opening the 2nd cable installer (VB-CABLE A+B) — click YES at UAC then 'Install Driver'.")
            try:
                rc = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", installer, None, os.path.dirname(installer), 1)
                if rc <= 32:
                    logger.error(f"[Cable2] Could not launch the installer (code {rc}) — you may have clicked No at UAC.")
                    return
                logger.info("➡️ After installing, RESTART the PC & reopen the app — 'Inbound Cable' (Cable A) will appear.")
                self.status_label.configure(text="🔄 Installed: reboot & reopen the app", text_color="#FBBF24")
            except Exception as e:
                logger.error(f"[Cable2] Error running the installer: {e}")
        else:
            logger.info("ℹ️ No installer bundled in drivers/ — opening the VB-CABLE A+B download page.")
            try:
                webbrowser.open("https://vb-audio.com/Cable/")
            except Exception as e:
                logger.error(f"[Cable2] Could not open the browser ({e}). Download at: https://vb-audio.com/Cable/")

    def _on_tts_toggle(self):
        """Bật 'Đọc bản dịch' → khởi động worker TTS đẩy giọng dịch vào MIC ảo (CABLE).
        KHÔNG tự bật monitor (tránh loop khi mic trùng loa monitor)."""
        if self.tts_var.get():
            # KHÔNG tự ép bật monitor: nếu mic ≡ loa monitor (vd INZONE Buds), monitor
            # gây loop (mic thu lại giọng TTS). Để người dùng tự bật nếu thật sự cần.
            self._ensure_tts_worker()
            logger.info("🔊 Reading translation to the listener (pushed into the virtual CABLE MIC). "
                        "Enable 'I also hear it' to preview — may echo if mic == speaker.")
        else:
            logger.info("🔇 Translation reading turned off.")

    def _on_monitor_toggle(self):
        """Bật/tắt 'Tôi cũng nghe'. Vì monitor chỉ phát khi có CÂU DỊCH MỚI, lúc bật
        ta phát 1 tiếng bíp thử NGAY ra loa monitor để xác nhận nghe được (không phải
        chờ tới câu sau mới biết nút có ăn hay không)."""
        if self.tts_monitor_var.get():
            self._ensure_tts_worker()
            spk = self._get_monitor_speaker()
            if spk is None:
                logger.error("[Monitor] No real speaker found to preview — check your audio devices.")
                self.tts_monitor_var.set(False)
                return
            logger.info(f"🎧 Monitor ON — you will hear the translation on speaker: '{spk.name}' (playing test beep).")
            threading.Thread(target=self._play_monitor_test, args=(spk,), daemon=True).start()
        else:
            logger.info("🔇 Monitor OFF — only pushing the translated voice into the virtual MIC for the listener.")

    def _play_monitor_test(self, speaker):
        """Bíp ngắn 440Hz ~0.25s @24kHz để xác nhận loa monitor thực sự kêu."""
        try:
            n = int(24000 * 0.25)
            t = np.arange(n, dtype=np.float32) / 24000.0
            tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32).reshape(-1, 1)
            self._play_buffer(speaker, tone)
        except Exception as e:
            logger.error(f"[Monitor] Error playing test beep: {e}")

    def stop_engine(self):
        global is_recording
        if not is_recording: return
        is_recording = False
        self.status_label.configure(text="💤 Stopped", text_color="#A7F3D0")
        # Khi kết thúc phiên: chạy bản tổng hợp hoàn chỉnh chất lượng cao (gpt-4o)
        self.generate_summary(final=True)

    # ================= V.c VOICE OUT → MIC ẢO (TTS, họp 2 chiều) =================
    def _ensure_tts_worker(self):
        if not self.tts_worker_started:
            self.tts_worker_started = True
            # Pipeline: tách TỔNG HỢP và PHÁT để câu N+1 được synth sẵn trong khi câu N đang phát
            # → giấu độ trễ synth (~vài giây) khỏi luồng nghe, chỉ câu đầu tiên còn trễ.
            self.tts_play_q = queue.Queue()        # (audio, primary, monitor) cho lane out
            self.tts_play_q_in = queue.Queue()     # ... cho lane in
            threading.Thread(target=self._tts_synth_worker, args=(self.tts_queue, "out", self.tts_play_q), daemon=True).start()
            threading.Thread(target=self._tts_synth_worker, args=(self.tts_queue_in, "in", self.tts_play_q_in), daemon=True).start()
            threading.Thread(target=self._tts_play_worker, args=(self.tts_play_q, "out"), daemon=True).start()
            threading.Thread(target=self._tts_play_worker, args=(self.tts_play_q_in, "in"), daemon=True).start()
            logger.info("🔊 TTS worker started (synth↔play pipeline; OUT → virtual MIC, IN → real speaker).")
        # Pre-warm kết nối TTS (DNS/TLS) ngay để câu ĐẦU TIÊN không chịu trễ cold-start.
        if not self._tts_warmed:
            self._tts_warmed = True
            threading.Thread(target=self._tts_prewarm, daemon=True).start()

    @staticmethod
    def _norm_tts_key(text):
        """Khóa cache TTS: thường hóa text để bản dịch TẠM khớp bản CHỐT (dù lệch dấu câu/hoa thường)."""
        t = re.sub(r"\s+", " ", (text or "").lower()).strip()
        return t.strip(" .,!?;:…\"'·、。，！？")

    def _tts_prewarm(self):
        """Gọi 1 lần tổng hợp ngắn để 'làm nóng' kết nối Edge → giảm trễ câu đầu tiên."""
        try:
            targets = self._resolve_targets("out")
            if targets is None:
                return
            _p, _m, lang_code, voice = targets
            self._synth_audio("xin chào", lang_code, voice)  # kết quả bỏ đi, chỉ để mở kết nối
            logger.info("🔥 [TTS] Warmed up the voice-synthesis connection (lower first-sentence latency).")
        except Exception:
            pass

    def _maybe_presynth_out(self):
        """Khi bản dịch TẠM (provisional) của luồng RA 'đứng yên' ~0.3s → coi như câu đã xong,
        TỔNG HỢP SẴN giọng vào cache NGAY (lúc người nói đang nghỉ) để khi câu chốt thì phát liền.
        Chỉ chạy với Edge (free) để không tốn phí synth thừa khi trật cache."""
        if not self.tts_var.get():
            return
        if getattr(config, "TTS_PROVIDER", "openai") != "edge":
            return
        text = (self.engine.active_trans + self.engine.prov_trans).strip()
        now = time.time()
        if text != self._prov_last:          # text vừa đổi → reset mốc ổn định
            self._prov_last = text
            self._prov_since = now
            return
        if not text or len(text) < 6:
            return
        if now - self._prov_since < 0.3:     # chưa "đứng yên" đủ lâu
            return
        key = self._norm_tts_key(text)
        with self._presynth_lock:
            if key in self._tts_cache or key in self._presynth_pending:
                return
            self._presynth_pending.add(key)
        threading.Thread(target=self._presynth_worker, args=(text, key), daemon=True).start()

    def _presynth_worker(self, text, key):
        """Tổng hợp trước 1 câu (lane RA) rồi cất vào cache. Trật thì thôi (vô hại)."""
        try:
            targets = self._resolve_targets("out")
            if targets is None:
                return
            _p, _m, lang_code, voice = targets
            audio = self._synth_audio(text, lang_code, voice)
            if audio is not None:
                with self._presynth_lock:
                    self._tts_cache[key] = audio
                    while len(self._tts_cache) > 24:     # giữ cache nhỏ gọn
                        self._tts_cache.pop(next(iter(self._tts_cache)))
        except Exception as e:
            logger.warning(f"[TTS-presynth] skipped: {e}")
        finally:
            with self._presynth_lock:
                self._presynth_pending.discard(key)

    def _tts_synth_worker(self, text_q, lane, play_q):
        """STAGE 1: lấy text → chọn thiết bị + tổng hợp giọng → đẩy buffer sang play_q.
        Chạy TRƯỚC luồng phát nên synth câu kế tiếp diễn ra trong lúc câu hiện tại đang phát."""
        while True:
            text = text_q.get()
            if not text:
                continue
            try:
                targets = self._resolve_targets(lane)
                if targets is None:
                    continue
                primary, monitor, lang_code, voice = targets
                # Ưu tiên audio đã PRE-SYNTH sẵn (lane RA): bản dịch tạm đã được synth lúc
                # người nói nghỉ → phát ngay, khỏi chờ tổng hợp. Trật cache → synth như cũ.
                audio = None
                if lane == "out":
                    key = self._norm_tts_key(text)
                    # Nếu đang pre-synth dở đúng câu này → ĐỢI nó xong (tối đa 2s) để xài lại,
                    # tránh tổng hợp 2 lần. Xong/không-pending/hết-giờ thì thôi.
                    deadline = time.time() + 2.0
                    while True:
                        with self._presynth_lock:
                            audio = self._tts_cache.pop(key, None)
                            pending = key in self._presynth_pending
                        if audio is not None or not pending or time.time() > deadline:
                            break
                        time.sleep(0.05)
                hit = audio is not None
                logger.info(f"🔊 [TTS-{lane}]{' (cache)' if hit else ''} Speaking: {text}")
                if audio is None:
                    audio = self._synth_audio(text, lang_code, voice)
                if audio is not None:
                    play_q.put((audio, primary, monitor))
            except Exception as e:
                logger.error(f"[TTS-{lane}] Voice synthesis error: {e}")

    def _tts_play_worker(self, play_q, lane="out"):
        """STAGE 2: phát từng buffer nối tiếp (block tới khi xong) để không chồng tiếng.
        Lane 'in': bật cờ _tts_in_active quanh lúc phát → audio_capture_thread GATE input VÀO,
        chống loop (loopback thu lại chính giọng TTS). Đuôi 0.3s chờ âm tắt hẳn."""
        while True:
            audio, primary, monitor = play_q.get()
            try:
                # Bật cờ chống loop tương ứng lane: khi đang phát TTS, luồng VÀO (loopback)
                # bị GATE để không thu lại chính giọng TTS (xem audio_capture_thread).
                if lane == "in":
                    self._tts_in_active = True
                else:
                    self._tts_out_active = True
                self._play_audio(audio, primary, monitor)
            except Exception as e:
                logger.error(f"[TTS] Voice playback error: {e}")
            finally:
                # đuôi 0.3s chờ âm tắt hẳn trước khi mở lại cổng thu (tránh bắt phần đuôi)
                time.sleep(0.3)
                if lane == "in":
                    self._tts_in_active = False
                else:
                    self._tts_out_active = False

    def _get_monitor_speaker(self, exclude_name=None):
        """Chọn LOA THẬT để nghe thử (bỏ qua CABLE/ảo và thiết bị đang gửi đi).
        Ưu tiên endpoint MEDIA, tránh kênh thoại 'Chat'/'Communications' (thường nhỏ tiếng).
        KHÔNG đổi loa mặc định Windows — chỉ đọc để phát thêm."""
        def is_real(s):
            n = s.name.lower()
            return "cable" not in n and "virtual" not in n and s.name != exclude_name
        speakers = [s for s in sc.all_speakers() if is_real(s)]
        if not speakers:
            return None
        # Loại endpoint thoại (chat/communications/hands-free) nếu còn lựa chọn khác
        comm_kw = ("chat", "communication", "hands-free", "handsfree")
        media = [s for s in speakers if not any(k in s.name.lower() for k in comm_kw)]
        pool = media or speakers
        # Trong nhóm media, ưu tiên loa mặc định Windows nếu nó nằm trong nhóm
        try:
            d = sc.default_speaker()
            for s in pool:
                if d is not None and s.name == d.name:
                    return s
        except Exception:
            pass
        return pool[0]

    def _play_buffer(self, speaker, audio):
        """Phát NGUYÊN câu (đã tổng hợp xong) trong 1 lần → liền mạch, không underrun
        nên không còn rè/click do jitter mạng. `audio` = float32 mono shape (N,1)."""
        try:
            with speaker.player(samplerate=24000, channels=1) as p:
                p.play(audio)
        except Exception as e:
            logger.error(f"[TTS] Playback error on '{getattr(speaker, 'name', '?')}': {e}")

    def _resolve_targets(self, lane="out"):
        """Chọn thiết bị + ngôn ngữ + GIỌNG theo lane. Trả (primary, monitor, lang_code, voice) hoặc None.
        - out: tiếng tôi đã dịch → MIC ảo (cho người nghe) + monitor loa thật nếu bật. Giọng = output_lang.
        - in : tiếng đối tác đã dịch sang tiếng tôi → đọc ra LOA THẬT đã chọn. Giọng = input_lang."""
        if lane == "out":
            primary = self.tts_out_map.get(self.tts_device_var.get())
            if primary is None:
                logger.error("[TTS] No valid output device selected (install VB-CABLE then choose the 'VIRTUAL MIC').")
                return None
            monitor = None
            if self.tts_monitor_var.get():
                monitor = self._get_monitor_speaker(exclude_name=primary.name)
                if monitor is None:
                    logger.error("[TTS] 'I also hear it' is on but no real speaker was found for playback.")
            lang_name = self.output_lang.get()
            voice_label = self.tts_voice_out_var.get()
        else:
            # Loa thật đã chọn (2 chiều); fallback tự dò loa thật nếu chưa chọn hợp lệ.
            primary = self.tts_out_map.get(self.speaker_real_var.get()) if getattr(self, "speaker_real_var", None) else None
            if primary is None:
                primary = self._get_monitor_speaker()
            if primary is None:
                logger.error("[TTS-in] No real speaker found to read the partner's voice.")
                return None
            monitor = None
            lang_name = self.input_lang.get()
            voice_label = getattr(self, "tts_voice_in_var", None).get() if getattr(self, "tts_voice_in_var", None) else ""
        lang_code = LANG_CODE_MAP.get(lang_name, "en")
        if lang_code == "auto":
            lang_code = "en"
        voice = tts_voice_from_label(lang_code, voice_label)
        return primary, monitor, lang_code, voice

    def _synth_audio(self, text, lang_code="en", voice=None):
        """Tổng hợp giọng → float32 mono (N,1) @24kHz. KHÔNG phát (để pipeline tách stage)."""
        try:
            speed = float(self.tts_speed_var.get().rstrip("x"))
        except Exception:
            speed = 1.0
        provider = getattr(config, "TTS_PROVIDER", "openai")
        if provider == "edge":
            pcm = self._synth_edge(text, lang_code, speed, voice)
        elif provider == "soniox":
            pcm = self._synth_soniox(text, lang_code, voice)
        else:
            pcm = self._synth_openai(text, speed)
        n = len(pcm) - (len(pcm) % 2)
        if n <= 0:
            return None
        return (np.frombuffer(pcm[:n], dtype=np.int16).astype(np.float32) / 32768.0).reshape(-1, 1)

    def _play_audio(self, audio, primary, monitor=None):
        """Phát NGUYÊN câu ra primary (+ monitor) song song, mỗi thiết bị 1 lần play."""
        threads = []
        for spk in [primary] + ([monitor] if monitor is not None else []):
            t = threading.Thread(target=self._play_buffer, args=(spk, audio), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:      # chờ phát xong để giữ thứ tự đọc nối tiếp giữa các câu
            t.join()

    def _synth_soniox(self, text, lang_code="en", voice=None):
        """TTS Soniox tts-rt-v1 -> PCM 24kHz 16-bit mono (trả phí ~$0.70/giờ, dùng KEY SONIOX sẵn có).
        Trả thẳng PCM s16le 24kHz nên khớp pipeline (không cần decode/resample như Edge).
        `voice` = giọng người dùng chọn ở dropdown; rỗng → SONIOX_TTS_VOICE/Adrian."""
        import httpx
        api_key = getattr(config, "SONIOX_API_KEY", "") or ""
        voice = voice or (getattr(config, "SONIOX_TTS_VOICE", "") or "Adrian")
        lang = lang_code if (lang_code and lang_code != "auto") else "en"
        body = {"model": "tts-rt-v1", "language": lang, "voice": voice,
                "audio_format": "pcm_s16le", "sample_rate": 24000, "text": text}
        with httpx.Client(timeout=30) as h:
            r = h.post("https://tts-rt.soniox.com/tts",
                       headers={"Authorization": f"Bearer {api_key}"}, json=body)
            r.raise_for_status()
            return r.content

    def _synth_openai(self, text, speed=1.0):
        """TTS OpenAI gpt-4o-mini-tts -> PCM 24kHz 16-bit mono (trả phí)."""
        kwargs = dict(
            model="gpt-4o-mini-tts",
            voice=self.tts_voice_var.get(),
            input=text,
            response_format="pcm",   # 24000 Hz, 16-bit signed, mono, little-endian
            instructions="Speak clearly at a brisk, natural conversational pace, like a live meeting interpreter. Do not drawl.",
        )

        def synth(create_kwargs):
            buf = bytearray()
            with client.audio.speech.with_streaming_response.create(**create_kwargs) as response:
                for chunk in response.iter_bytes():
                    buf.extend(chunk)
            return bytes(buf)
        try:
            return synth(dict(speed=speed, **kwargs))
        except Exception:
            return synth(kwargs)  # một số model không nhận tham số speed

    def _synth_edge(self, text, lang_code="en", speed=1.0, voice=None):
        """TTS Microsoft Edge neural (FREE, không cần key) -> PCM 24kHz 16-bit mono.
        Edge trả MP3 streaming → decode + resample về 24kHz bằng miniaudio.
        `voice` = giọng người dùng chọn (đầy đủ); rỗng → giọng mặc định của ngôn ngữ."""
        import edge_tts, miniaudio
        voice = voice or EDGE_VOICES.get(lang_code, EDGE_VOICES["en"])
        rate = f"{int(round((speed - 1.0) * 100)):+d}%"  # vd 1.2x -> '+20%'

        async def run():
            buf = bytearray()
            async for ch in edge_tts.Communicate(text, voice, rate=rate).stream():
                if ch["type"] == "audio":
                    buf.extend(ch["data"])
            return bytes(buf)
        # Edge thi thoảng ném NoAudioReceived khi bị throttle -> thử lại vài lần
        mp3 = b""
        for attempt in range(3):
            try:
                mp3 = asyncio.run(run())
                if mp3:
                    break
            except Exception as e:
                if attempt == 2:
                    raise
                logger.warning(f"[TTS-edge] Retrying ({attempt + 1}/3): {type(e).__name__}")
                time.sleep(1.0)
        if not mp3:
            return b""
        dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=24000)
        return dec.samples.tobytes()

    def _render_dual(self):
        """Gộp 2 luồng vào 2 ô GỐC | DỊCH theo quy luật:
             • cột TRÁI (Gốc)  = LUÔN ngôn ngữ ĐỐI TÁC
             • cột PHẢI (Dịch) = LUÔN ngôn ngữ TÔI (để dễ đọc)
             • dòng của LUỒNG NÓI (tôi) gắn icon 🗣️
        - LUỒNG NGHE (đối tác): trái = orig(đối tác)      , phải = trans(→ tôi)
        - LUỒNG NÓI  (tôi)    : trái = trans(→ đối tác nghe), phải = orig(tôi nói)
        Trả về (left_text, right_text, sub_left, sub_right)."""
        rows = []
        for b in self.engine_in.committed_blocks:           # NGHE (đối tác)
            rows.append((b.get("time", ""), b.get("orig", ""), b.get("trans", ""), False))
        for b in self.engine.committed_blocks:              # NÓI (tôi)
            rows.append((b.get("time", ""), b.get("trans", ""), b.get("orig", ""), True))
        rows.sort(key=lambda r: r[0])

        left, right = [], []
        for t, l, r, mine in rows:
            ic = "🗣️ " if mine else ""
            if l.strip(): left.append(f"[{t}] {ic}{l}")
            if r.strip(): right.append(f"[{t}] {ic}{r}")

        sub_left = sub_right = ""
        # Dòng đang nói (provisional) — LUỒNG NÓI: trái=trans, phải=orig
        b_l = (self.engine.active_trans + self.engine.prov_trans).strip()
        b_r = (self.engine.active_orig + self.engine.prov_orig).strip()
        if b_l or b_r:
            bt = self.engine.current_block_time or time.strftime("%H:%M:%S")
            if b_l: left.append(f"[{bt}] 🗣️ {b_l}")
            if b_r: right.append(f"[{bt}] 🗣️ {b_r}")
            sub_left, sub_right = b_l, b_r
        # Dòng đang nói — LUỒNG NGHE: trái=orig, phải=trans
        a_l = (self.engine_in.active_orig + self.engine_in.prov_orig).strip()
        a_r = (self.engine_in.active_trans + self.engine_in.prov_trans).strip()
        if a_l or a_r:
            at = self.engine_in.current_block_time or time.strftime("%H:%M:%S")
            if a_l: left.append(f"[{at}] {a_l}")
            if a_r: right.append(f"[{at}] {a_r}")
            sub_left, sub_right = a_l, a_r
        # Không có dòng live → lấy block cuối cho phụ đề
        if not (sub_left or sub_right) and rows:
            sub_left, sub_right = rows[-1][1], rows[-1][2]
        return "\n".join(left).strip(), "\n".join(right).strip(), sub_left, sub_right

    def _pump_tts(self):
        """Phát hiện block dịch vừa chốt và đẩy vào hàng đợi TTS. Gọi trong update_ui_loop."""
        on_out = self.tts_var.get()
        two_way = self.direction_var.get() == "2 chiều"
        # Chỉ đọc-to tiếng đối tác khi đang DỊCH đầy đủ (Full translate). Ở chế độ Show-text
        # (_incoming_translate is False) tuyệt đối KHÔNG đọc-to → tránh loop.
        on_in = (two_way and getattr(self, "_incoming_translate", None)
                 and getattr(self, "listen_tts_var", None) is not None and self.listen_tts_var.get())
        if on_out or on_in:
            self._ensure_tts_worker()

        # LUỒNG RA: tiếng tôi đã dịch → đọc ra MIC ảo (người nghe)
        out_blocks = self.engine.committed_blocks
        if not on_out:
            self.tts_spoken_count = len(out_blocks)   # giữ con trỏ đồng bộ, không đọc dồn lịch sử
        else:
            # Đọc ĐỦ + ĐÚNG thứ tự, mỗi câu đúng MỘT lần (con trỏ chỉ tiến, không lùi → không đọc lại).
            # Nói nhanh thì đọc trễ hơn nhưng không bỏ ý; backlog tự rút khi có khoảng lặng.
            while self.tts_spoken_count < len(out_blocks):
                t = (out_blocks[self.tts_spoken_count].get("trans") or "").strip()
                if t:
                    self.tts_queue.put(t)
                self.tts_spoken_count += 1

        # LUỒNG VÀO (2 chiều): tiếng đối tác đã dịch sang TIẾNG TÔI → đọc to ra loa thật.
        # Đồng bộ với sub vì cùng kích hoạt lúc block chốt. Con trỏ chỉ tiến (không đọc lại).
        in_blocks = self.engine_in.committed_blocks
        if not on_in:
            self.tts_spoken_count_in = len(in_blocks)
        else:
            while self.tts_spoken_count_in < len(in_blocks):
                t = (in_blocks[self.tts_spoken_count_in].get("trans") or "").strip()
                if t:
                    self.tts_queue_in.put(t)
                self.tts_spoken_count_in += 1

    def install_vbcable(self):
        """Chạy installer VB-CABLE đóng gói kèm app với quyền admin (UAC bật lên).
        File installer đặt tại: drivers/VBCABLE_Setup_x64.exe (đóng gói qua PyInstaller datas)."""
        # Đã cài rồi thì khỏi cài lại
        try:
            if any("cable" in s.name.lower() for s in sc.all_speakers()):
                logger.info("✅ VB-CABLE is already installed — no need to reinstall.")
                self.status_label.configure(text="✅ Virtual MIC ready", text_color="#22C55E")
                return
        except Exception:
            pass

        installer = resource_path(os.path.join("drivers", "VBCABLE_Setup_x64.exe"))
        if not os.path.exists(installer):
            logger.error(f"[VB-CABLE] Installer not found: {installer} — "
                         f"copy VBCABLE_Setup_x64.exe into the 'drivers/' folder next to the app.")
            return

        logger.info("📥 Opening the VB-CABLE installer — click YES at the UAC window, then 'Install Driver'.")
        try:
            # Verb 'runas' -> bật UAC xin quyền admin (bắt buộc khi cài driver nhân)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", installer, None, os.path.dirname(installer), 1
            )
            if rc <= 32:
                logger.error(f"[VB-CABLE] Could not launch the installer (code {rc}) — "
                             f"you may have clicked No at UAC.")
                return
            logger.info("➡️ After installing, RESTART the PC then reopen the app — "
                        "the 'MIC sent into the meeting' menu will show CABLE Input.")
            self.status_label.configure(text="🔄 Installed: reboot & reopen the app", text_color="#FBBF24")
        except Exception as e:
            logger.error(f"[VB-CABLE] Error running the installer: {e}")

    # ================= TỪ CHUYÊN NGÀNH (GLOSSARY) =================
    def _refresh_glossary_from_cloud(self):
        """Luồng nền: tải glossary mới nhất từ GitHub → thay self.glossary + ghi cache offline."""
        data, _sha = github_glossary_fetch()
        if not (data and isinstance(data.get("terms"), list)):
            return
        try:
            self.glossary = Glossary(terms=data.get("terms", []),
                                     keep_original=data.get("keep_original", []),
                                     corrections=data.get("corrections", {}),
                                     meta=data.get("meta", {}))
            with open(glossary_cache_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            c = self.glossary.counts()
            logger.info(f"☁️ [GLOSSARY] Đồng bộ từ cloud: {c['terms']} thuật ngữ, "
                        f"{c['corrections']} từ đã học (áp dụng ở lần START kế).")
            cb = getattr(self, "_glossary_refresh_cb", None)
            if cb is not None:
                try: self.after(0, cb)
                except Exception: pass
        except Exception as e:
            logger.warning(f"[GLOSSARY] Lỗi áp dữ liệu cloud: {e}")

    def _cloud_push_glossary(self, message):
        """Đẩy self.glossary lên GitHub (luồng nền) nếu đã cấu hình token; nếu chưa thì chỉ báo."""
        if not _github_token():
            logger.info("ℹ️ [GLOSSARY] Đã lưu cục bộ. Chưa có token (AVBUDS_GH_TOKEN / github_token.txt / "
                        "config.GLOSSARY_GITHUB_TOKEN) → KHÔNG đẩy lên cloud.")
            return

        def work():
            ok, info = github_glossary_push(self.glossary.to_dict(), message)
            if ok:
                logger.info("☁️ [GLOSSARY] Đã đẩy lên GitHub — mọi máy nhận ở lần mở kế.")
            else:
                logger.error(f"[GLOSSARY] Đẩy lên GitHub thất bại: {info}")
        threading.Thread(target=work, daemon=True).start()

    # ---- GỢI Ý từ mới trong buổi họp ----
    def _note_suggestion(self, term):
        """Ghi nhận 1 ứng viên; True nếu là gợi ý MỚI (chưa biết, chưa bỏ qua)."""
        if not term:
            return False
        if self.glossary.is_known(term) or term.lower() in self._sugg_dismissed:
            return False
        n = self.suggested_terms.get(term, 0) + 1
        self.suggested_terms[term] = n
        return n == 1

    def _scan_suggestions(self):
        """Quét block MỚI chốt (2 luồng) → gom thuật ngữ chưa có trong glossary. Rẻ, không API."""
        changed = False
        for eng, attr in ((self.engine, "_sugg_seen_out"), (self.engine_in, "_sugg_seen_in")):
            blocks = eng.committed_blocks
            i = getattr(self, attr)
            while i < len(blocks):
                b = blocks[i]
                for txt in (b.get("orig", ""), b.get("trans", "")):
                    for cand in extract_candidate_terms(txt):
                        if self._note_suggestion(cand):
                            changed = True
                i += 1
            setattr(self, attr, i)
        if changed:
            self._update_glossary_badge()

    def _harvest_summary_terms(self, data):
        """Gom essential_keywords do LLM chọn trong báo cáo summary → gợi ý nếu chưa có."""
        try:
            changed = False
            for item in (data.get("detailed_discussion_ledger") or []):
                for kw in (item.get("essential_keywords_and_terms") or []):
                    kw = (kw or "").strip()
                    if kw and 2 <= len(kw) <= 40 and self._note_suggestion(kw):
                        changed = True
            if changed:
                self._update_glossary_badge()
        except Exception:
            pass

    def _update_glossary_badge(self):
        """Cập nhật số gợi ý trên nút 📚 + làm mới danh sách trong dialog nếu đang mở."""
        n = len(getattr(self, "suggested_terms", {}))
        if getattr(self, "glossary_btn", None) is not None:
            try:
                self.glossary_btn.configure(text=(f"📚 Từ chuyên ngành 💡{n}" if n else "📚 Từ chuyên ngành"))
            except Exception:
                pass
        cb = getattr(self, "_glossary_refresh_cb", None)
        if cb is not None:
            try: cb()
            except Exception: pass

    @staticmethod
    def _lang_of(word):
        """Đoán ngôn ngữ của 1 từ để điền đúng ô: ko nếu có Hangul, vi nếu có dấu tiếng Việt, else en."""
        if re.search(r"[가-힣]", word):
            return "ko"
        if re.search(r"[àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđ]", word.lower()):
            return "vi"
        return "en"

    def open_glossary_dialog(self):
        """Cửa sổ xem/thêm thuật ngữ + DẠY từ nghe-sai→đúng. Lưu ngay vào glossary.json
        cạnh app (nhớ qua các phiên). Thuật ngữ mới áp dụng ở lần START kế; từ đã dạy
        (correction) áp dụng NGAY cho text mới chốt."""
        if getattr(self, "_glossary_win", None) is not None and self._glossary_win.winfo_exists():
            self._glossary_win.focus(); return
        win = ctk.CTkToplevel(self)
        self._glossary_win = win
        win.title("📚 Từ chuyên ngành")
        win.geometry("460x760")
        win.configure(fg_color=COLOR_BG)
        win.after(200, lambda: win.attributes("-topmost", True))

        count_lbl = ctk.CTkLabel(win, text="", text_color="#5EEAD4", font=("Arial", 12, "bold"))
        count_lbl.pack(pady=(12, 4), padx=16, anchor="w")

        def refresh_counts():
            c = self.glossary.counts()
            count_lbl.configure(text=f"📚 {c['terms']} thuật ngữ · {c['keep_original']} tên riêng · "
                                     f"{c['corrections']} từ đã học")

        # (self._glossary_refresh_cb được gán refresh_all ở cuối, sau khi render_suggestions có mặt)

        def _on_close():
            self._glossary_refresh_cb = None
            win.destroy()
            self._glossary_win = None
        win.protocol("WM_DELETE_WINDOW", _on_close)

        # --- Đồng bộ cloud ---
        def do_sync():
            logger.info("🔄 [GLOSSARY] Đang kéo bản mới nhất từ GitHub...")
            threading.Thread(target=self._refresh_glossary_from_cloud, daemon=True).start()

        ctk.CTkButton(win, text="🔄 Đồng bộ từ cloud (GitHub)", command=do_sync,
                      fg_color="#0E7490", hover_color="#155E75", font=("Arial", 11, "bold")).pack(pady=(0, 4), padx=16, fill="x")

        # --- GỢI Ý từ mới phát hiện trong buổi họp (chưa có trong glossary) ---
        fsg = ctk.CTkFrame(win, fg_color="#0F172A", corner_radius=8)
        fsg.pack(pady=6, padx=16, fill="x")
        ctk.CTkLabel(fsg, text="💡 Gợi ý từ mới (xuất hiện trong họp):", text_color="#FBBF24",
                     font=("Arial", 11, "bold")).pack(pady=(8, 2), padx=10, anchor="w")
        sugg_list = ctk.CTkScrollableFrame(fsg, fg_color="#0B1220", height=130)
        sugg_list.pack(pady=(0, 8), padx=10, fill="x")

        def add_suggestion(term):
            lang = self._lang_of(term)
            if self.glossary.add_term(**{lang: term}):
                self.glossary.save(glossary_cache_path())
                self.suggested_terms.pop(term, None)
                self._sugg_dismissed.add(term.lower())
                logger.info(f"📚 Thêm từ gợi ý: {term} ({lang}).")
                self._cloud_push_glossary(f"add suggested term: {term}")
                refresh_counts(); render_suggestions(); self._update_glossary_badge()

        def dismiss_suggestion(term):
            self.suggested_terms.pop(term, None)
            self._sugg_dismissed.add(term.lower())
            render_suggestions(); self._update_glossary_badge()

        def render_suggestions():
            for w in sugg_list.winfo_children():
                w.destroy()
            items = sorted(self.suggested_terms.items(), key=lambda kv: -kv[1])
            if not items:
                ctk.CTkLabel(sugg_list, text="(chưa có — sẽ hiện khi có thuật ngữ lạ lúc họp)",
                             text_color=COLOR_TEXT_DRAFT, font=("Arial", 9)).pack(anchor="w", padx=4, pady=4)
                return
            for term, freq in items[:40]:
                row = ctk.CTkFrame(sugg_list, fg_color="transparent")
                row.pack(fill="x", pady=1)
                ctk.CTkButton(row, text="✕", width=26, height=24, fg_color="#7F1D1D", hover_color="#9F1239",
                              font=("Arial", 10), command=lambda t=term: dismiss_suggestion(t)).pack(side="right", padx=(2, 0))
                ctk.CTkButton(row, text="➕", width=32, height=24, fg_color=COLOR_BTN,
                              font=("Arial", 11, "bold"), command=lambda t=term: add_suggestion(t)).pack(side="right", padx=2)
                ctk.CTkLabel(row, text=f"{term}   ×{freq}", text_color=COLOR_TEXT_MAIN,
                             font=("Arial", 11), anchor="w").pack(side="left", padx=4)

        def _entry(parent, ph):
            e = ctk.CTkEntry(parent, placeholder_text=ph, fg_color="#0F172A", font=("Arial", 11))
            e.pack(pady=3, padx=10, fill="x")
            return e

        # --- Thêm thuật ngữ mới ---
        f1 = ctk.CTkFrame(win, fg_color="#0F172A", corner_radius=8)
        f1.pack(pady=6, padx=16, fill="x")
        ctk.CTkLabel(f1, text="➕ Thêm thuật ngữ (điền ít nhất 1 ô):", text_color=COLOR_TEXT_MAIN,
                     font=("Arial", 11, "bold")).pack(pady=(8, 2), padx=10, anchor="w")
        e_en = _entry(f1, "English (vd: impedance)")
        e_ko = _entry(f1, "한국어 (vd: 임피던스)")
        e_vi = _entry(f1, "Tiếng Việt (vd: trở kháng)")

        def do_add_term():
            label = e_en.get() or e_ko.get() or e_vi.get()
            if self.glossary.add_term(en=e_en.get(), ko=e_ko.get(), vi=e_vi.get()):
                self.glossary.save(glossary_cache_path())
                logger.info(f"📚 Đã thêm thuật ngữ: {label} (áp dụng ở lần START kế).")
                for e in (e_en, e_ko, e_vi): e.delete(0, "end")
                refresh_counts()
                self._cloud_push_glossary(f"add term: {label}")
            else:
                logger.error("[GLOSSARY] Cần điền ít nhất 1 ô để thêm thuật ngữ.")

        ctk.CTkButton(f1, text="➕ Thêm thuật ngữ", command=do_add_term,
                      fg_color=COLOR_BTN, font=("Arial", 11, "bold")).pack(pady=(4, 10), padx=10, fill="x")

        # --- Dạy từ nghe-sai → đúng (học ngay) ---
        f2 = ctk.CTkFrame(win, fg_color="#0F172A", corner_radius=8)
        f2.pack(pady=6, padx=16, fill="x")
        ctk.CTkLabel(f2, text="🎯 Dạy từ nghe-SAI → ĐÚNG (áp dụng ngay):", text_color=COLOR_TEXT_MAIN,
                     font=("Arial", 11, "bold")).pack(pady=(8, 2), padx=10, anchor="w")
        e_wrong = _entry(f2, "Nghe sai thành (vd: impedin)")
        e_right = _entry(f2, "Phải là (vd: impedance)")

        def do_add_corr():
            w, r = e_wrong.get(), e_right.get()
            if self.glossary.add_correction(w, r):
                self.glossary.save(glossary_cache_path())
                logger.info(f"🎯 Đã học: '{w}' → '{r}' (sửa ngay cho câu mới).")
                for e in (e_wrong, e_right): e.delete(0, "end")
                refresh_counts()
                self._cloud_push_glossary(f"learn correction: {w} -> {r}")
            else:
                logger.error("[GLOSSARY] Cần điền cả 2 ô (nghe-sai và đúng).")

        ctk.CTkButton(f2, text="🎯 Dạy từ này", command=do_add_corr,
                      fg_color="#7C3AED", hover_color="#6D28D9", font=("Arial", 11, "bold")).pack(pady=(4, 10), padx=10, fill="x")

        ctk.CTkLabel(win,
                     text="ℹ️ App tải glossary từ GitHub mỗi lần mở (cache offline). Thuật ngữ mới →\n"
                          "dùng ở lần START kế; từ đã 'dạy' → sửa ngay câu vừa chốt.\n"
                          "☁️ Có token (file github_token.txt cạnh app / env AVBUDS_GH_TOKEN /\n"
                          "config.py) → thêm từ TỰ ĐẨY lên cloud cho mọi máy. Chưa có → chỉ lưu cục bộ.",
                     text_color=COLOR_TEXT_DRAFT, font=("Arial", 9), justify="left").pack(pady=(6, 8), padx=16, anchor="w")

        def refresh_all():
            refresh_counts(); render_suggestions()
        self._glossary_refresh_cb = refresh_all   # luồng nền/scan cập nhật cả nhãn lẫn danh sách gợi ý
        refresh_all()

    def update_ui_loop(self):
        while not ui_queue.empty():
            msg = ui_queue.get()

            if msg["type"] == "status":
                self.log_box.insert("end", f"{msg['msg']}\n")
                self.log_box.see("end")
                continue
            elif msg["type"] == "error":
                self.status_label.configure(text="SYSTEM ERROR", text_color=COLOR_STOP)
                self.log_box.insert("end", f"❌ {msg['msg']}\n")
                continue

            # --- Đẩy token vào lõi logic (định tuyến theo lane RA/VÀO) ---
            elif msg["type"] == "soniox_unified":
                # Lớp 2 — sửa từ 'nghe sai' đã HỌC trên text ĐÃ CHỐT (provisional bỏ qua vì đổi liên tục).
                if self.glossary.corrections:
                    if msg.get("orig_final"):
                        msg["orig_final"] = self.glossary.correct_text(msg["orig_final"])
                    if msg.get("trans_final"):
                        msg["trans_final"] = self.glossary.correct_text(msg["trans_final"])
                if msg.get("lane") == "in":
                    self.engine_in.feed_soniox(msg)
                else:
                    self.engine.feed_soniox(msg)
            elif msg["type"] == "openai_orig_delta":
                self.engine.feed_openai_orig_delta(msg["text"])
            elif msg["type"] == "openai_orig_done":
                self.engine.feed_openai_orig_done(msg["text"])
            elif msg["type"] == "openai_trans_delta":
                self.engine.feed_openai_trans_delta(msg["text"])
            elif msg["type"] == "openai_trans_done":
                self.engine.feed_openai_trans_done(msg["text"])

        if is_recording:
            current_time = time.time()

            # Khóa dòng Fallback an toàn cho CẢ 2 luồng nếu im lặng quá 5.5 giây
            if self.engine.fallback_commit(5.5):
                logger.info("⏱ Force-committed line (SPEAK lane).")
            self.engine_in.fallback_commit(5.5)

            # --- RENDER 2 LUỒNG VÀO GỐC | DỊCH (trái=đối tác, phải=tôi, 🗣️=tôi nói) ---
            left_text, right_text, _sl, _sr = self._render_dual()

            self.left_box.delete("1.0", "end")
            self.left_box.insert("end", left_text)
            self.left_box.see("end")

            self.right_box.delete("1.0", "end")
            self.right_box.insert("end", right_text)
            self.right_box.see("end")

            # --- PHỤ ĐỀ NỔI: hàng đợi dwell-time, chỉ câu đã chốt (đọc kịp, hết nhấp nháy) ---
            self._pump_subtitle()

            # --- PRE-SYNTH: tổng hợp sẵn giọng cho câu dịch tạm khi nó "đứng yên" (giảm trễ) ---
            self._maybe_presynth_out()

            # --- ĐỌC BẢN DỊCH RA MIC ẢO (TTS) khi có block mới chốt ---
            self._pump_tts()

            # --- GỢI Ý TỪ MỚI: quét thuật ngữ chưa có trong glossary (rẻ, không gọi API) ---
            self._scan_suggestions()

            # KHÔNG tự tóm tắt theo chu kỳ nữa (tốn token). Summary chỉ chạy khi bấm nút
            # 📊 SUMMARY hoặc khi ⏹️ STOP (bản final). Xem generate_summary().

            self.after(50, self.update_ui_loop)

    # ================= V.b PHỤ ĐỀ NỔI (FLOATING SUBTITLE OVERLAY) =================
    def toggle_subtitle(self):
        # Đang mở -> đóng
        if self.subtitle_window is not None and self.subtitle_window.winfo_exists():
            if self._sub_hover_after is not None:
                try: self.after_cancel(self._sub_hover_after)
                except Exception: pass
                self._sub_hover_after = None
            self.subtitle_window.destroy()
            self.subtitle_window = None
            self.sub_bar = None
            self._sub_bar_visible = False
            return

        win = ctk.CTkToplevel(self)
        win.title("Avbuds Sub")
        win.geometry("920x160+320+720")
        try: win.overrideredirect(True)            # bỏ khung viền OS
        except: pass

        # --- VÙNG CHỮ đặt TRÊN, chiếm phần lớn ---
        text_area = ctk.CTkFrame(win, fg_color="#0B1220")
        text_area.pack(side="top", fill="both", expand=True, padx=6, pady=(6, 0))
        self.sub_text_area = text_area

        # Dòng CÂU TRƯỚC (mờ, nhỏ, căn giữa) — giữ ngữ cảnh liền mạch, không gây "nhảy"
        self.sub_prev_label = ctk.CTkLabel(text_area, text="", text_color="#64748B",
                                           font=("Segoe UI", self._sub_prev_size(), "bold"),
                                           wraplength=880, justify="center", anchor="center")
        self.sub_prev_label.pack(fill="x", padx=10, pady=(6, 0))
        # Dòng CÂU HIỆN TẠI (sáng, to, CĂN GIỮA) — như phụ đề YouTube
        self.sub_trans_label = ctk.CTkLabel(text_area, text="🎙️ Waiting for content...", text_color=self.sub_text_color,
                                            font=("Segoe UI", self.sub_font_size, "bold"),
                                            wraplength=880, justify="center", anchor="center")
        self.sub_trans_label.pack(fill="both", expand=True, padx=10, pady=(2, 6))

        # --- THANH ĐIỀU KHIỂN đặt DƯỚI: ẩn mặc định, hiện khi rê chuột vào ---
        # height cố định + pack_propagate(False) -> nút không bị ép nhỏ khi chữ sub dài
        bar = ctk.CTkFrame(win, fg_color="#0B1220", corner_radius=6, height=34)
        bar.pack_propagate(False)
        ctk.CTkLabel(bar, text="⠿ drag", text_color="#64748B", font=("Arial", 9)).pack(side="left", padx=6)
        ctk.CTkButton(bar, text="✕", width=24, height=20, fg_color="#7F1D1D", hover_color="#9F1239",
                      font=("Arial", 10), command=self.toggle_subtitle).pack(side="right", padx=2, pady=2)
        ctk.CTkButton(bar, text="🌓", width=28, height=20, fg_color="#1E293B", hover_color="#334155",
                      font=("Arial", 11), command=self.toggle_sub_mode).pack(side="right", padx=2, pady=2)
        ctk.CTkButton(bar, text="🎨", width=28, height=20, fg_color="#1E293B", hover_color="#334155",
                      font=("Arial", 11), command=self.pick_sub_color).pack(side="right", padx=2, pady=2)
        ctk.CTkButton(bar, text="A+", width=26, height=20, fg_color="#1E293B", hover_color="#334155",
                      font=("Arial", 10), command=lambda: self.adjust_sub_font(2)).pack(side="right", padx=2, pady=2)
        ctk.CTkButton(bar, text="A−", width=26, height=20, fg_color="#1E293B", hover_color="#334155",
                      font=("Arial", 10), command=lambda: self.adjust_sub_font(-2)).pack(side="right", padx=2, pady=2)
        bar.bind("<Button-1>", self._start_move)
        bar.bind("<B1-Motion>", self._on_move)
        self.sub_bar = bar
        self._sub_bar_visible = False

        self.subtitle_window = win
        self._apply_sub_mode(self.sub_mode)
        win.after(120, lambda: win.attributes("-topmost", True))  # luôn nổi trên cùng
        self._sub_hover_after = self.after(180, self._poll_sub_hover)

    def _apply_sub_mode(self, mode):
        """Đổi chế độ nền: 'transparent' (chữ nổi, xuyên thấu) hoặc 'black' (hộp đen)."""
        self.sub_mode = mode
        win = self.subtitle_window
        if win is None or not win.winfo_exists():
            return
        if mode == "transparent" and self._supports_transparent:
            bg = SUB_KEY
            try:
                win.attributes("-transparentcolor", SUB_KEY)
                win.attributes("-alpha", 1.0)
            except Exception:
                self._supports_transparent = False
                bg = "#0B1220"
        else:
            bg = "#0B1220"
            try: win.attributes("-transparentcolor", "")   # tắt color-key
            except Exception: pass
            try: win.attributes("-alpha", 1.0)
            except Exception: pass
        try: win.configure(fg_color=bg)
        except Exception: pass
        for wdg in (self.sub_text_area, self.sub_prev_label, self.sub_trans_label):
            try: wdg.configure(fg_color=bg)
            except Exception: pass

    def toggle_sub_mode(self):
        self._apply_sub_mode("black" if self.sub_mode == "transparent" else "transparent")

    def _show_sub_bar(self):
        if self.sub_bar is not None and not self._sub_bar_visible:
            # before=text_area -> bar được cấp chỗ TRƯỚC, không bị chữ dài ép cụt
            self.sub_bar.pack(side="bottom", fill="x", padx=6, pady=(0, 4),
                              before=self.sub_text_area)
            self._sub_bar_visible = True

    def _hide_sub_bar(self):
        if self.sub_bar is not None and self._sub_bar_visible:
            self.sub_bar.pack_forget()
            self._sub_bar_visible = False

    def _poll_sub_hover(self):
        """Hiện thanh điều khiển khi con trỏ ở trong vùng phụ đề, ẩn khi ra ngoài.
        Dùng vị trí con trỏ toàn cục nên hoạt động cả khi nền trong suốt (click-through)."""
        win = self.subtitle_window
        if win is None or not win.winfo_exists():
            self._sub_hover_after = None
            return
        try:
            px, py = win.winfo_pointerxy()
            x, y = win.winfo_rootx(), win.winfo_rooty()
            w, h = win.winfo_width(), win.winfo_height()
            inside = (x <= px <= x + w) and (y <= py <= y + h)
        except Exception:
            inside = False
        if inside:
            self._show_sub_bar()
        else:
            self._hide_sub_bar()
        self._sub_hover_after = self.after(180, self._poll_sub_hover)

    def pick_sub_color(self):
        from tkinter import colorchooser
        win = self.subtitle_window
        # Tạm bỏ "luôn trên cùng" để hộp chọn màu không bị phụ đề che
        try:
            if win is not None and win.winfo_exists():
                win.attributes("-topmost", False)
        except Exception: pass
        chosen = colorchooser.askcolor(color=self.sub_text_color, title="Choose subtitle text color", parent=win)
        try:
            if win is not None and win.winfo_exists():
                win.attributes("-topmost", True)
        except Exception: pass
        if chosen and chosen[1]:
            self.sub_text_color = chosen[1]
            if self.sub_trans_label is not None and self.sub_trans_label.winfo_exists():
                self.sub_trans_label.configure(text_color=self.sub_text_color)

    def _start_move(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_move(self, event):
        if self.subtitle_window is None or not self.subtitle_window.winfo_exists():
            return
        x = self.subtitle_window.winfo_x() + (event.x - self._drag_x)
        y = self.subtitle_window.winfo_y() + (event.y - self._drag_y)
        self.subtitle_window.geometry(f"+{x}+{y}")

    def _sub_prev_size(self):
        """Cỡ chữ dòng 'câu trước' (mờ) = ~60% cỡ câu hiện tại, tối thiểu 12."""
        return max(12, int(self.sub_font_size * 0.6))

    def adjust_sub_font(self, delta):
        self.sub_font_size = max(12, min(60, self.sub_font_size + delta))
        if self.sub_trans_label is not None and self.sub_trans_label.winfo_exists():
            self.sub_trans_label.configure(font=("Segoe UI", self.sub_font_size, "bold"))
        if getattr(self, "sub_prev_label", None) is not None and self.sub_prev_label.winfo_exists():
            self.sub_prev_label.configure(font=("Segoe UI", self._sub_prev_size(), "bold"))

    def _sub_dwell(self, pair):
        """Thời gian giữ tối thiểu 1 câu phụ đề để đọc kịp.
        ~1.2s + theo độ dài câu (tính trên cột PHẢI = tiếng tôi đọc)."""
        txt = (pair[1] or pair[0])
        return max(1.8, min(6.0, 1.2 + 0.045 * len(txt)))

    def _pump_subtitle(self):
        """A+C: chỉ đưa câu ĐÃ CHỐT (final) vào hàng đợi, hiển thị từng câu và
        GIỮ đủ lâu để đọc; nói nhanh dồn câu thì rút ngắn dwell để bắt kịp.
        KHÔNG hiển thị text tạm (provisional) nên hết nhấp nháy."""
        now = time.time()

        # 1) Nạp câu MỚI chốt — CHỈ luồng NGHE (lời đối tác đã dịch sang TIẾNG TÔI).
        #    KHÔNG hiện lời TÔI nói (luồng NÓI) cho phụ đề gọn, dễ theo dõi.
        self.sub_seen_out = len(self.engine.committed_blocks)   # giữ con trỏ đồng bộ, không dùng
        new = []
        in_blocks = self.engine_in.committed_blocks       # NGHE: trái=orig(đối tác), phải=trans(→tôi)
        while self.sub_seen_in < len(in_blocks):
            b = in_blocks[self.sub_seen_in]
            new.append((b.get("time", ""), b.get("orig", "").strip(), b.get("trans", "").strip(), False))
            self.sub_seen_in += 1
        new.sort(key=lambda r: r[0])
        for _t, left, right, mine in new:
            if left or right:
                self.sub_queue.append((left, right, mine))

        # 2) Điều tiết chuyển câu theo dwell-time (mỗi lần chuyển → đẩy vào history)
        def _advance():
            self.sub_cur = self.sub_queue.pop(0)
            self.sub_cur_at = now
            self.sub_history.append(self.sub_cur)
            if len(self.sub_history) > 3:          # giữ tối đa 3 câu (cuốn chiếu)
                self.sub_history.pop(0)

        if self.sub_cur is None:
            if self.sub_queue:
                _advance()
        elif self.sub_queue:
            dwell = self._sub_dwell(self.sub_cur)
            if len(self.sub_queue) >= 3:      # tồn đọng nhiều → đọc nhanh để bắt kịp
                dwell = min(dwell, 1.1)
            if now - self.sub_cur_at >= dwell:
                _advance()

        # 3) Vẽ ra cửa sổ phụ đề (nếu đang mở) — 2 DÒNG: câu TRƯỚC (mờ) + câu HIỆN TẠI (sáng).
        #    Luôn hiện BẢN DỊCH (cột phải = tiếng TÔI); đánh dấu 🗣️ nếu là lời tôi nói.
        if self.subtitle_window is None or not self.subtitle_window.winfo_exists():
            return
        if not self.sub_history:
            return

        def _disp(item):
            left, right, mine = item
            txt = (right or left).strip()
            return (("🗣️ " if mine else "") + txt) if txt else ""

        cur = _disp(self.sub_history[-1])
        prev = _disp(self.sub_history[-2]) if len(self.sub_history) >= 2 else ""
        self.sub_prev_label.configure(text=prev)
        self.sub_trans_label.configure(text=cur if cur else "🎙️ ...")

    # ================= V.c TỔNG HỢP BIÊN BẢN (ROLLING / INCREMENTAL) =================
    def _collect_summary_feed(self):
        """Gộp lời 2 bên thành transcript NGÔN NGỮ TÔI (INPUT) cho summary:
           - Tôi (luồng NÓI):  dùng lời GỐC tôi nói (đã là INPUT).
           - Đối tác (NGHE):    dùng bản DỊCH sang tiếng tôi (INPUT).
        Append-only theo thứ tự chốt → con trỏ rolling-summary ổn định."""
        out_blocks = self.engine.committed_blocks
        while self.sum_seen_out < len(out_blocks):
            t = (out_blocks[self.sum_seen_out].get("orig") or "").strip()
            if t:
                self.summary_feed.append(f"[Me] {t}")
            self.sum_seen_out += 1
        in_blocks = self.engine_in.committed_blocks
        while self.sum_seen_in < len(in_blocks):
            t = (in_blocks[self.sum_seen_in].get("trans") or "").strip()
            if t:
                self.summary_feed.append(f"[Partner] {t}")
            self.sum_seen_in += 1

    def generate_summary(self, final=False):
        if self.is_summarizing:
            return

        self._collect_summary_feed()
        transcript = self.summary_feed
        if not self.summary.should_run(transcript, final):
            return

        # Biên bản LUÔN viết bằng ngôn ngữ TÔI nói (INPUT)
        target_lang = self.input_lang.get()
        model, messages, snapshot, new_text, _prev_json = self.summary.build_request(
            transcript, target_lang, final,
            model_fast=SUM_MODEL_FAST, model_final=SUM_MODEL_FINAL)
        prev_report_obj = self.summary.report  # dùng cho mock

        # Thông báo trạng thái theo ngôn ngữ đầu ra
        status_messages = {
            "English": "Analyzing R&D data, please wait...\n",
            "Korean": "R&D 데이터를 분석 중입니다. 잠시만 기다려 주세요...\n",
            "Japanese": "R&Dデータを分析しています。少々お待ちください...\n",
            "Chinese": "正在分析R&D数据，请稍候...\n",
            "Vietnamese": "Đang phân tích dữ liệu R&D, vui lòng đợi...\n",
            "Auto detect": "Analyzing R&D data, please wait...\n"
        }
        current_status = status_messages.get(target_lang, "Analyzing R&D data, please wait...\n")
        self.analysis_box.delete("1.0", "end")
        self.analysis_box.insert("end", ("🧪 [MOCK] " if MOCK_AI else "") + current_status)
        self.update()

        self.is_summarizing = True

        def fetch_summary():
            try:
                if MOCK_AI:
                    time.sleep(0.4)  # giả lập độ trễ mạng
                    data = mock_summary_data(prev_report_obj, new_text, target_lang, final)
                else:
                    response = text_client.chat.completions.create(
                        model=model,
                        response_format={"type": "json_object"},
                        max_tokens=4000,
                        messages=messages,
                    )
                    data = json.loads(response.choices[0].message.content)

                # Chỉ tiến con trỏ khi gộp thành công -> lần sau không gửi lại đoạn đã xử lý
                self.summary.apply(data, snapshot)
                logger.info(f"📝 [SUMMARY] Minutes updated via {'MOCK' if MOCK_AI else model} (final={final}).")
                self.after(0, self.render_report, data)

            except Exception as e:
                logger.error(f"[SUMMARY] Summarization error ({model}): {e}")
                self.after(0, self._show_summary_error, str(e))
            finally:
                self.is_summarizing = False

        threading.Thread(target=fetch_summary, daemon=True).start()

    def _show_summary_error(self, msg):
        # Nếu đã có báo cáo cũ thì giữ lại, chỉ ghi chú lỗi ở cuối
        if not self.summary.report:
            self.analysis_box.delete("1.0", "end")
        self.analysis_box.insert("end", f"\n⚠️ Summary error: {msg}\n")

    def render_report(self, data):
        """Đổ dữ liệu JSON biên bản lên analysis_box (luôn chạy trên main thread qua self.after)."""
        self._harvest_summary_terms(data)   # gom từ khóa LLM chọn → gợi ý từ mới
        self.analysis_box.delete("1.0", "end")
        try:
            txt = self.analysis_box._textbox
            txt.tag_config("main_title", font=("Segoe UI", 25, "bold"), foreground="#1E293B")
            txt.tag_config("section_header", font=("Segoe UI", 20, "bold"), foreground="#2563EB")
            txt.tag_config("topic_header", font=("Segoe UI", 18, "bold"), foreground="#0D9488")
            txt.tag_config("bullet_bold", font=("Segoe UI", 18, "bold"), foreground="#1E293B")
            txt.tag_config("keyword_tag", font=("Consolas", 17, "bold"), foreground="#EA580C")
            txt.tag_config("body_text", font=("Segoe UI", 18), foreground="#475569")
            txt.tag_config("divider", font=("Segoe UI", 12), foreground="#CBD5E1")

            def insert_format(text, tag="body_text", newline=True):
                txt.insert("end", str(text) + ("\n" if newline else ""), tag)

            insert_format("📊 MEETING SUMMARY REPORT", "main_title")
            insert_format("—" * 65, "divider")

            if "meeting_brief_overview" in data:
                insert_format("🔍 Overview:", "section_header")
                insert_format(data['meeting_brief_overview'], "body_text")
                insert_format("", "body_text")

            if "detailed_discussion_ledger" in data:
                insert_format("Direct Discussion Ledger:\n", "section_header")
                for item in data["detailed_discussion_ledger"]:
                    insert_format(f"📌 Topic: {item.get('topic', 'N/A')}", "topic_header")

                    insert_format("• Context: ", "bullet_bold", newline=False)
                    insert_format(item.get('context_or_issues_raised', 'N/A'), "body_text")

                    if "individual_arguments_and_debates" in item:
                        insert_format("💬 Arguments & Debates:", "bullet_bold")
                        for arg in item["individual_arguments_and_debates"]:
                            insert_format(f"  > {arg}", "body_text")
                        insert_format("", "body_text")

                    if "essential_keywords_and_terms" in item:
                        insert_format("🔑 Keywords: ", "bullet_bold", newline=False)
                        keywords = item["essential_keywords_and_terms"]
                        for i, kw in enumerate(keywords):
                            insert_format(f" {kw} ", "keyword_tag", newline=False)
                            if i < len(keywords) - 1:
                                insert_format("|", "divider", newline=False)
                        insert_format("\n", "body_text")

                    if "decisions_or_next_steps" in item:
                        insert_format("✔ Decisions & Next Steps:", "bullet_bold")
                        for step in item["decisions_or_next_steps"]:
                            insert_format(f"  - {step}", "body_text")

                    insert_format("—" * 45, "divider")

            if data.get("unresolved_issues_pool"):
                insert_format("❓ Unresolved Issues Pool:", "section_header")
                for issue in data["unresolved_issues_pool"]:
                    insert_format(f"• {issue}", "body_text")
                insert_format("—" * 65, "divider")

            if data.get("action_items_tracking"):
                insert_format("📋 Action Items Tracking:", "section_header")
                for action in data["action_items_tracking"]:
                    insert_format(f"☐ {action}", "body_text")

        except Exception as parse_err:
            self.analysis_box.insert("end", json.dumps(data, ensure_ascii=False, indent=2))

def on_closing():
    global is_recording
    is_recording = False
    app.destroy()

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", on_closing)
    app.mainloop()
