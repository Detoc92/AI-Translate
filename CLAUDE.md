# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này. Đọc kỹ trước khi sửa code.

## Chương trình là gì

**Avbuds AI Secretary V22.0** — app desktop GUI (Windows) phiên dịch hội thoại **real-time** + tự động ghi biên bản cuộc họp (R&D CRESYN).

- `Avbuds_AI_Translate.py` — lớp GUI + 2 engine WebSocket (Soniox/OpenAI) + nối dây. Sửa ở đây cho UI, engine, demo/mock.
- `avbuds_core.py` — **lõi logic thuần** (TranscriptEngine, RollingSummary, mock, schema, self-test). Sửa ở đây cho logic transcript/summary; có test chạy được không cần GUI.
- `config.py` chứa API key + đường dẫn model.
- Mọi thứ khác (`*.c`, `*.pyd`, `build/`, `dist/`, `backup/`) là sản phẩm sinh ra hoặc bản cũ — **không sửa bằng tay**.

## Lệnh thường dùng

```bash
# Chạy từ nguồn (chưa có requirements.txt — phải cài tay)
pip install customtkinter websockets soundcard numpy openai pillow
python Avbuds_AI_Translate.py

# === TEST NHANH (không cần mic / API key / customtkinter) ===
python avbuds_core.py                       # self-test logic lõi (30 assert, stdlib thuần)

# === CHẠY GUI Ở CHẾ ĐỘ DEMO (cần customtkinter) ===
python Avbuds_AI_Translate.py --demo --mock-ai   # bơm transcript mẫu + summary giả lập, offline hoàn toàn
python Avbuds_AI_Translate.py --demo             # transcript mẫu nhưng summary gọi API thật
# (cũng bật được qua env: AVBUDS_DEMO=1, AVBUDS_MOCK_AI=1, AVBUDS_REAL_AI=1)

# Đóng gói exe
pyinstaller Avbuds_AI_Translate.spec   # -> dist/Avbuds_AI_Translate/Avbuds_AI_Translate.exe
```

**Testability**: logic dễ sai (máy trạng thái transcript + rolling summary) nằm ở `avbuds_core.py` — **module thuần stdlib, không import GUI/SDK**, có `_selftest()` chạy bằng bất kỳ Python nào. Khi sửa logic transcript/summary, sửa ở `avbuds_core.py` rồi chạy `python avbuds_core.py` để verify ngay (không cần dựng GUI). GUI chỉ là lớp glue mỏng gọi vào lõi.

## Kiến trúc (cách nó hoạt động)

App là một `ctk.CTk` (class `App`). Luồng dữ liệu:

```
Mic/System Audio ─(soundcard)─► audio_queue ─► async engine (websockets)
                                                   │ trả kết quả qua
                                                   ▼
                                                ui_queue ─► update_ui_loop() (mỗi 50ms) ─► 2 ô textbox
full_transcript ─► generate_summary() ─► Gemini/GPT-4o (text_client) ─► analysis_box
```

**3 ngữ cảnh chạy song song, giao tiếp CHỈ qua Queue (không block nhau):**

1. `audio_capture_thread` (threading) — đọc PCM int16, đẩy vào `audio_queue`.
2. `run_async_loop` (threading + asyncio riêng) — chạy 1 trong 2 engine:
   - `soniox_engine()` — WebSocket Soniox `stt-rt-v5`, **16000 Hz**, chế độ `Default (Soniox)`.
   - `openai_realtime_engine()` — OpenAI `gpt-realtime-translate`, **24000 Hz**, chế độ `Smart Brain (Premium)`.
   - Mỗi engine gồm 2 coroutine `send_audio()` + `receive_*()` chạy qua `asyncio.gather`.
3. `update_ui_loop()` (main Tkinter thread, `self.after(50, ...)`) — tiêu thụ `ui_queue`, vẽ lại UI.

**Sample rate đổi tự động theo engine** (`start_engine`): Soniox 16000, OpenAI 24000. Nếu thêm engine mới phải xử lý rate ở đây.

### Quản lý state văn bản (quan trọng khi sửa logic hiển thị)

- `committed_blocks[]` — block đã chốt `{time, orig, trans}`.
- `active_orig/active_trans` — text "final" đang gom cho block hiện tại.
- `prov_orig/prov_trans` — text tạm (provisional/draft) chạy live.
- `full_transcript[]` — bản dịch đã chốt, là input cho summary.
- Chốt dòng (commit) khi: nhận token `is_final`/`*_done`, **hoặc** im lặng > 5.5s (fallback trong `update_ui_loop`).

Soniox và OpenAI gửi message type khác nhau vào `ui_queue` (`soniox_unified` vs `openai_orig_delta/done`, `openai_trans_delta/done`) — `update_ui_loop` xử lý từng loại riêng. Khi đổi engine, giữ đúng hợp đồng message type này.

### Summary / Master Report (ROLLING / INCREMENTAL)

`generate_summary(final=False)` dùng cơ chế **rolling tăng dần** để chống đốt tiền + chống rơi ý:
- Giữ state `self.summary_report` (báo cáo cấu trúc gần nhất) + `self.summarized_count` (con trỏ số dòng `full_transcript` đã gộp).
- Mỗi lần chỉ gửi **delta** (`full_transcript[summarized_count:]`) + báo cáo cũ (`[PREVIOUS_REPORT]`) → model **MERGE** thay vì làm lại từ đầu. Con trỏ chỉ tiến khi gộp thành công.
- Bật `response_format={"type":"json_object"}` (JSON mode) + `max_tokens=4000` → JSON luôn hợp lệ, không còn gỡ ```json fence, hết lỗi parse.
- **Hybrid model**: auto mỗi 300s → model "fast" (`final=False`, rẻ); nút **SUMMARY** và khi **STOP** → model "final" (`final=True`, đánh bóng bản cuối). Auto bỏ qua nếu không có dòng mới.
- **Nhà cung cấp text đổi được** (tiết kiệm chi phí): `config.TEXT_PROVIDER` = `gemini` (free tier, `gemini-2.5-flash-lite`/`gemini-2.5-flash` qua endpoint OpenAI-compat) hoặc `openai` (`gpt-4o-mini`/`gpt-4o`). GUI tạo `text_client` + hằng `SUM_MODEL_FAST/FINAL`, bơm vào `RollingSummary.build_request(model_fast=, model_final=)`. Thiếu `GEMINI_API_KEY` → tự fallback về OpenAI.
- Render tách thành `render_report(data)`, gọi qua `self.after(0, ...)` để chạy trên main thread (an toàn Tkinter). Lỗi → `_show_summary_error` (giữ báo cáo cũ). Schema chung ở hằng `REPORT_SCHEMA`.
- Khóa `self.is_summarizing` chống chạy chồng.

### Phụ đề nổi (floating subtitle overlay)

`toggle_subtitle()` tạo `ctk.CTkToplevel` frameless (`overrideredirect`), `-topmost`, `-alpha 0.88`, **kéo thả** (`_start_move`/`_on_move`), chỉnh cỡ chữ (`adjust_sub_font`). Hiển thị **song ngữ**: `sub_orig_label` (gốc, nhỏ) + `sub_trans_label` (dịch, lớn). Cập nhật từ `update_subtitle()` gọi trong `update_ui_loop` — **không tốn chi phí AI**. Nút bật/tắt "🖥️ Phụ đề nổi" trên sidebar.

## Quy ước & lưu ý khi sửa

- **Tiếng Việt**: log, comment, status message đều bằng tiếng Việt — giữ nguyên phong cách này.
- **Logging kép**: dùng `logger` (đã cấu hình) — tự ghi ra `system_debug.log` + console + UI panel. `logging.ERROR` trở lên hiện thành lỗi đỏ trên UI. Đừng dùng `print`.
- Truy cập màu/tag textbox qua `widget._textbox` (lõi tkinter của customtkinter).
- `is_recording` là cờ global điều khiển vòng đời mọi luồng — set `False` để dừng sạch.
- API hiện tại: Soniox real-time `stt-rt-v5` + async `stt-async-v5`, OpenAI Realtime `gpt-4o-realtime-preview` (Smart Brain), summary `gemini-2.5-flash`/`gpt-4o` tuỳ `TEXT_PROVIDER`. Nếu user hỏi/đổi model AI, kiểm tra lại tên model hợp lệ trước (model Soniox/Gemini bị deprecate theo lịch).

## Phần CHƯA hoàn thiện (đừng tưởng là bug)

- Nút **Calibrate Noise** và thanh **vol_bar** vẫn chưa gắn command (UI trang trí).
- (Đã hoạt động: **File Processing** = mặc định Soniox async (`stt-async-v5`, STT+dịch 1 lần gọi qua `soniox_transcribe_file()`); đặt `config.FILE_STT_PROVIDER="openai"` để quay lại Whisper batch + GPT-4o → `process_file()`; **EXPORT DATA** = Markdown qua `build_markdown` (core); **Smart Brain** = OpenAI Realtime WS đã viết lại endpoint/model chuẩn; **Phụ đề nổi**, **SUMMARY**.)
- ⚠️ **Smart Brain (OpenAI Realtime)** đã sửa theo API chuẩn (`wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview`, header `OpenAI-Beta: realtime=v1`, transcription `gpt-4o-transcribe`, server_vad, instructions dịch) nhưng **chưa test được với key thật** — cần key có quyền Realtime để xác nhận. Sửa ở `openai_realtime_engine()`.
- `requirements.txt` đang **trống**.
- `config.py` chứa **API key plaintext** — không commit/đẩy ra ngoài; nếu refactor, ưu tiên chuyển sang biến môi trường.
- `rule.txt` mô tả Protocol **V17.4** (kiến trúc 4 luồng VAD/STT/GPT/AUDIT, fuzzy-match overlap) — là đặc tả thiết kế **cũ**, KHÔNG khớp code V22.0 hiện hành. Dùng làm tham chiếu ý tưởng, không phải mô tả code đang chạy.

## Tài nguyên đi kèm

`logo.png`, `logo.ico` (UI); `base/` (model Whisper-base CTranslate2 cho STT offline — hiện code online không nạp trực tiếp); `knowledge.txt` (từ khóa domain: Sony, ATH, PCB, CRESYN, R&D).
