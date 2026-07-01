# Avbuds AI Secretary (Avbuds AI Translate)

> Trợ lý phiên dịch & ghi biên bản hội thoại **thời gian thực** dành cho R&D CRESYN.
> Phiên bản hiện tại: **V22.0 (Strict Auth Selector)** — file nguồn chính: `Avbuds_AI_Translate.py`.

---

## 1. Tổng quan

Avbuds AI Secretary là ứng dụng desktop (GUI) chạy trên Windows, cho phép:

- **Thu âm thanh thời gian thực** từ micro hoặc từ âm thanh hệ thống (loopback / "nghe lén" cuộc họp online).
- **Nhận dạng giọng nói (STT)** và **dịch song song** ra ngôn ngữ đích, hiển thị đồng thời 2 cột: *Văn bản gốc* và *Bản dịch*.
- **Tổng hợp biên bản cuộc họp (Master Report)** tự động bằng GPT-4o, định dạng JSON có cấu trúc, hiển thị màu sắc trực quan.
- Hỗ trợ **2 động cơ AI** lựa chọn được: Soniox (mặc định, tốc độ cao) và OpenAI Realtime (Smart Brain / Premium).

Ứng dụng được đóng gói thành file thực thi (`.exe`) qua PyInstaller (xem `Avbuds_AI_Translate.spec`, thư mục `dist/` và `build/`).

---

## 2. Tính năng chính

| Nhóm | Mô tả |
|------|-------|
| **Real-time STT + dịch** | Chuyển giọng nói thành văn bản và dịch ngay lập tức theo từng token (final / provisional). |
| **2 động cơ AI** | `Default (Soniox)` dùng WebSocket Soniox `stt-rt-v4`; `Smart Brain (Premium)` dùng OpenAI Realtime `gpt-realtime-translate`. |
| **Chọn nguồn âm thanh** | Liệt kê mọi micro và thiết bị loopback (System Audio) qua thư viện `soundcard`. |
| **Đa ngôn ngữ** | Input/Output: Auto detect, English, Korean, Japanese, Vietnamese, Chinese. |
| **Hiển thị 2 cột song ngữ** | Cột trái: `ORIGINAL STT`; cột phải: `TRANSLATION`. Mỗi block gắn timestamp `[HH:MM:SS]`. |
| **Master Report (tóm tắt)** | Tự động mỗi 5 phút hoặc bấm nút `SUMMARY`; gọi GPT-4o sinh biên bản JSON, render màu sắc trên `analysis_box`. |
| **Logging kép** | Ghi log chi tiết ra file `system_debug.log` + console, đồng thời hiển thị log rút gọn lên panel UI. |
| **Cơ chế chốt dòng (commit)** | Tự khóa dòng khi có token "final" hoặc khi im lặng quá 5.5 giây (fallback). |

---

## 3. Kiến trúc & luồng xử lý

### 3.1. Sơ đồ luồng

```
[Micro / System Audio]
        │  (soundcard recorder, PCM int16)
        ▼
   audio_queue  ◄── audio_capture_thread (threading)
        │
        ▼
 Async Engine (asyncio + websockets)         ──►  ui_queue  ──►  update_ui_loop()  ──►  GUI
   ├─ soniox_engine()        (16000 Hz)                              (after 50ms)        (left/right box)
   └─ openai_realtime_engine() (24000 Hz)
        │
        ▼
 full_transcript  ──►  generate_summary()  ──►  GPT-4o  ──►  analysis_box (Master Report)
```

### 3.2. Đa luồng (threading + asyncio)

- **Thread thu âm** (`audio_capture_thread`): đọc PCM 4000 frame mỗi vòng, chuyển sang `int16` và đẩy vào `audio_queue`.
- **Thread async loop** (`run_async_loop`): chạy event loop riêng, gọi engine STT/dịch tương ứng với chế độ đã chọn.
  - Trong engine: 2 coroutine `send_audio()` (gửi PCM) và `receive_*()` (nhận kết quả) chạy đồng thời qua `asyncio.gather`.
- **Vòng cập nhật UI** (`update_ui_loop`): chạy trên main thread Tkinter, lặp mỗi 50ms, tiêu thụ `ui_queue` và vẽ lại 2 ô văn bản.
- Giao tiếp giữa các luồng hoàn toàn qua **Queue** (`audio_queue`, `ui_queue`) — không luồng nào block luồng khác.

### 3.3. Cơ chế quản lý văn bản (state)

| Biến | Ý nghĩa |
|------|---------|
| `committed_blocks` | Danh sách các block đã chốt (time / orig / trans). |
| `active_orig`, `active_trans` | Văn bản final đang tích lũy cho block hiện tại. |
| `prov_orig`, `prov_trans` | Văn bản tạm (provisional/draft) chạy realtime. |
| `current_block_time` | Timestamp khởi đầu block. |
| `full_transcript` | Toàn bộ bản dịch đã chốt — dùng làm input cho summary. |
| `last_token_time` | Mốc thời gian token cuối, dùng cho fallback khóa dòng 5.5s. |

---

## 4. Cấu hình

File `config.py`:

```python
OPENAI_API_KEY = "sk-proj-..."        # Key OpenAI (STT realtime + GPT-4o summary)
SONIOX_API_KEY = "..."                # Key Soniox (động cơ dịch mặc định)
MODEL_PATH     = ".../base"           # Đường dẫn model Whisper local (faster-whisper / CTranslate2)
DEFAULT_TARGET_LANG = "en"
```

> ⚠️ **Cảnh báo bảo mật:** `config.py` hiện đang chứa API key thật ở dạng plain-text. Không commit file này lên Git công khai; nên chuyển sang biến môi trường (`.env`) hoặc nhập runtime.

Bản đồ mã ngôn ngữ (`LANG_CODE_MAP`):
`Auto detect→auto, English→en, Korean→ko, Japanese→ja, Vietnamese→vi, Chinese→zh`.

---

## 5. Giao diện người dùng

- **Sidebar (trái):** logo, chọn chế độ (File Processing / Realtime Mic), chọn thiết bị âm thanh, ngôn ngữ vào/ra, chọn động cơ AI, nút `EXPORT DATA`, thanh âm lượng (`vol_bar`), hiệu chỉnh nhiễu (`Calibrate Noise`), panel `SYSTEM LOGS`, nhãn trạng thái.
- **Khu chính (phải):**
  - Hàng trên: 2 ô `ORIGINAL STT 🎤` và `TRANSLATION ✅`.
  - Hàng giữa: ô `MASTER REPORT` (biên bản/tóm tắt/phân tích).
  - Hàng dưới: nút `SUMMARY`, `START`, `STOP`.

Bảng màu chủ đạo (dark theme nền `#1E293B`, hộp văn bản nền trắng, chữ committed `#1E293B`, nhấn mạnh xanh `#2563EB`).

---

## 6. Master Report — biên bản tự động

Hàm `generate_summary()` gọi **GPT-4o** với system prompt yêu cầu trả về JSON theo schema:

```json
{
  "meeting_brief_overview": "...",
  "detailed_discussion_ledger": [
    {
      "topic": "...",
      "context_or_issues_raised": "...",
      "individual_arguments_and_debates": ["Speaker X ...", "Speaker Y ..."],
      "essential_keywords_and_terms": ["..."],
      "decisions_or_next_steps": ["..."]
    }
  ],
  "unresolved_issues_pool": ["..."],
  "action_items_tracking": ["Assignee - Task - Deadline/Status"]
}
```

Nguyên tắc prompt (rút gọn):
1. **Universal adaptability** — tự thích nghi ngữ cảnh cuộc họp, không bó hẹp lĩnh vực.
2. **Debate & perspective tracking** — bắt đủ quan điểm/tranh luận từng người.
3. **Full detail preservation** — giữ nguyên chi tiết, không dùng "etc.", không rút gọn.
4. **Rolling update** — gộp dữ liệu mới vào báo cáo cũ theo trình tự thời gian.

Output language tự khớp với *Output Language* đang chọn. JSON được làm sạch (`gỡ ```json ... ```), parse và render màu trên `analysis_box`; nếu parse lỗi sẽ fallback hiển thị text thô.

---

## 7. Yêu cầu hệ thống & phụ thuộc

**Thư viện Python:**

```
customtkinter      # GUI
websockets         # kết nối WebSocket Soniox / OpenAI Realtime
soundcard          # thu micro + loopback system audio
numpy              # xử lý PCM
soundfile / sounddevice  (đóng gói portaudio)
openai             # GPT-4o summary + OpenAI realtime
Pillow (PIL)       # hiển thị logo
```

> `requirements.txt` hiện đang trống — cần bổ sung danh sách phụ thuộc ở trên trước khi cài đặt môi trường mới.

**Tài nguyên đi kèm:** `logo.png`, `logo.ico`, thư mục `base/` (model Whisper-base CTranslate2 cho STT offline — `model.bin`, `tokenizer.json`, `vocabulary.txt`, `config.json`).

---

## 8. Cách chạy

### Chạy từ mã nguồn
```bash
pip install customtkinter websockets soundcard numpy openai pillow
python Avbuds_AI_Translate.py
```

### Build file thực thi
```bash
pyinstaller Avbuds_AI_Translate.spec
# Kết quả: dist/Avbuds_AI_Translate/Avbuds_AI_Translate.exe
```

Quy trình sử dụng:
1. Chọn **thiết bị âm thanh** (Mic hoặc System Audio).
2. Chọn **Input / Output Language** và **động cơ AI**.
3. Bấm **START** → nói/phát âm thanh → xem song ngữ realtime.
4. Bấm **SUMMARY** (hoặc tự động mỗi 5 phút) để sinh biên bản.
5. Bấm **STOP** để dừng phiên.

---

## 9. Cấu trúc thư mục (rút gọn)

```
translate tool/
├─ Avbuds_AI_Translate.py      # Mã nguồn chính (GUI + engine)
├─ Avbuds_AI_Translate.spec    # Cấu hình PyInstaller
├─ config.py                   # API keys & cấu hình
├─ rule.txt                    # Đặc tả kiến trúc protocol (V17.4 — tham chiếu lịch sử)
├─ knowledge.txt               # Từ khóa domain (Sony, ATH, PCB, CRESYN, R&D...)
├─ logo.png / logo.ico         # Tài nguyên giao diện
├─ system_debug.log            # Log chi tiết runtime
├─ meeting_history.log         # Lịch sử cuộc họp
├─ base/                       # Model Whisper-base (CTranslate2) cho STT offline
├─ backup/                     # Các phiên bản cũ (Realtime.py, main_final.py)
├─ build/ , dist/              # Sản phẩm đóng gói PyInstaller
└─ *.c / *.pyd                 # Mã đã biên dịch Cython
```

---

## 10. Ghi chú & hạn chế đã biết

- **Chế độ `File Processing`** xuất hiện trên UI nhưng phần xử lý file (offline) chưa được nối logic trong `Avbuds_AI_Translate.py` — hiện chỉ `Realtime Mic` hoạt động đầy đủ.
- Nút **EXPORT DATA**, **Calibrate Noise** và **vol_bar** đã có trên giao diện nhưng chưa gắn lệnh xử lý.
- `rule.txt` mô tả kiến trúc Protocol **V17.4** (4 luồng VAD/STT/GPT/AUDIT, fuzzy-match overlap...) — đây là đặc tả thiết kế gốc; bản hiện hành V22.0 đã chuyển sang dùng động cơ WebSocket hợp nhất (Soniox/OpenAI) đơn giản hơn.
- API key đang để plaintext trong `config.py` — nên di chuyển khỏi mã nguồn.
- `requirements.txt` trống — cần điền phụ thuộc.

---

*Tài liệu này được sinh tự động dựa trên việc đọc mã nguồn `Avbuds_AI_Translate.py`, `config.py`, `Avbuds_AI_Translate.spec`, `rule.txt` và các tài nguyên đi kèm.*
