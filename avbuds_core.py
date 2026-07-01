"""
avbuds_core.py — Lõi logic THUẦN (không phụ thuộc GUI / SDK) của Avbuds AI Secretary.

Mục tiêu: tách máy trạng thái transcript + cơ chế rolling-summary ra khỏi GUI để có
thể KIỂM THỬ NHANH bằng Python thuần (chỉ cần stdlib), không cần micro / API key /
customtkinter. Chạy self-test:

    python avbuds_core.py

GUI (Avbuds_AI_Translate.py) chỉ việc gọi vào các lớp ở đây nên hành vi test được
chính là hành vi chạy thật.
"""

import time
import json
import re

LANG_CODE_MAP = {
    "Auto detect": "auto", "English": "en", "Korean": "ko",
    "Japanese": "ja", "Vietnamese": "vi", "Chinese": "zh"
}


def clean_protocol_tags(text):
    if not text:
        return ""
    return text.replace("<end>", "").replace("激活", "").replace("<pad>", "")


# Schema biên bản dùng chung cho cơ chế rolling summary (giữ đúng key để GUI render màu)
REPORT_SCHEMA = """{
  "meeting_brief_overview": "A clear, readable technical summary of the current meeting progress.",
  "detailed_discussion_ledger": [
    {
      "topic": "General topic or theme of this specific discussion block.",
      "context_or_issues_raised": "Detailed description of the situation, problem, or idea being discussed.",
      "individual_arguments_and_debates": [
        "Speaker X stated/proposed: [Detailed statement with exact keywords used]",
        "Speaker Y countered/added: [Detailed counter-argument, feedback, or complementary idea]"
      ],
      "essential_keywords_and_terms": [
        "Extract every raw keyword, numeric metric, proper name, or specific code mentioned in this topic block"
      ],
      "decisions_or_next_steps": [
        "Agreed decisions, solutions to test, or next validation actions."
      ]
    }
  ],
  "unresolved_issues_pool": [
    "List of conflicts, ideas, or open questions that remain unresolved with no team consensus yet."
  ],
  "action_items_tracking": [
    "Assignee Name - Specific Actionable Task Description - Deadline/Status"
  ]
}"""


# =====================================================================
#  MÁY TRẠNG THÁI TRANSCRIPT (gom token -> chốt block -> dựng dòng UI)
# =====================================================================
class TranscriptEngine:
    def __init__(self, clock=None, timestr=None):
        # clock/timestr cho phép inject để test xác định (deterministic)
        self._clock = clock or time.time
        self._timestr = timestr or (lambda: time.strftime("%H:%M:%S"))
        self.reset()

    def reset(self):
        self.committed_blocks = []
        self.active_orig = ""
        self.active_trans = ""
        self.prov_orig = ""
        self.prov_trans = ""
        self.current_block_time = ""
        self.last_token_time = self._clock()
        self.full_transcript = []

    def _stamp(self):
        if not self.current_block_time:
            self.current_block_time = self._timestr()

    # --- SONIOX (unified) ---
    def feed_soniox(self, msg):
        self.last_token_time = self._clock()
        self._stamp()
        if msg["orig_final"]:
            self.active_orig += msg["orig_final"]
        if msg["trans_final"]:
            self.active_trans += msg["trans_final"]
        self.prov_orig = msg["orig_prov"]
        self.prov_trans = msg["trans_prov"]
        if msg["trans_has_final"]:
            self._commit(self.active_orig, self.active_trans)

    # --- OPENAI realtime ---
    def feed_openai_orig_delta(self, text):
        self.last_token_time = self._clock()
        self._stamp()
        self.prov_orig += text

    def feed_openai_orig_done(self, text):
        self.last_token_time = self._clock()
        final_text = text.strip() if text else self.prov_orig.strip()
        if final_text:
            self.active_orig = final_text
        self.prov_orig = ""

    def feed_openai_trans_delta(self, text):
        self.last_token_time = self._clock()
        self._stamp()
        self.prov_trans += text

    def feed_openai_trans_done(self, text):
        self.last_token_time = self._clock()
        final_trans = text.strip() if text else self.prov_trans.strip()
        final_orig = self.active_orig.strip() if self.active_orig.strip() else self.prov_orig.strip()
        self._commit(final_orig, final_trans, reset_prov=True)

    # --- Commit & fallback ---
    def _commit(self, orig, trans, reset_prov=False):
        orig, trans = orig.strip(), trans.strip()
        if orig or trans:
            self.committed_blocks.append({
                "time": self.current_block_time or self._timestr(),
                "orig": orig,
                "trans": trans,
            })
            if trans:
                self.full_transcript.append(trans)
        self.active_orig = ""
        self.active_trans = ""
        if reset_prov:
            self.prov_orig = ""
            self.prov_trans = ""
        self.current_block_time = ""

    def fallback_commit(self, silence=5.5):
        """Ép khóa dòng nếu im lặng quá `silence` giây. Trả True nếu có chốt."""
        now = self._clock()
        if (self.active_orig.strip() or self.active_trans.strip()) and (now - self.last_token_time > silence):
            self._commit(self.active_orig, self.active_trans)
            return True
        return False

    # --- Dựng nội dung hiển thị ---
    def left_lines(self):
        lines = [f"[{b['time']}] {b['orig']}" for b in self.committed_blocks]
        live = self.active_orig + self.prov_orig
        if live.strip():
            t = f"[{self.current_block_time}] " if self.current_block_time else f"[{self._timestr()}] "
            lines.append(t + live)
        return "\n".join(lines).strip(), live

    def right_lines(self, live_left):
        lines = [f"[{b['time']}] {b['trans']}" for b in self.committed_blocks]
        live = self.active_trans + self.prov_trans
        if live.strip() or live_left.strip():
            t = f"[{self.current_block_time}] " if self.current_block_time else f"[{self._timestr()}] "
            lines.append(t + live)
        return "\n".join(lines).strip(), live

    def subtitle_pair(self, live_left="", live_right=""):
        cur_orig = (self.active_orig + self.prov_orig).strip() or live_left.strip()
        cur_trans = (self.active_trans + self.prov_trans).strip() or live_right.strip()
        if not cur_orig and self.committed_blocks:
            cur_orig = self.committed_blocks[-1]["orig"]
        if not cur_trans and self.committed_blocks:
            cur_trans = self.committed_blocks[-1]["trans"]
        return cur_orig[-220:], (cur_trans[-220:] if cur_trans else "")


# =====================================================================
#  ROLLING SUMMARY (gộp tăng dần: chỉ gửi delta + báo cáo cũ)
# =====================================================================
class RollingSummary:
    def __init__(self):
        self.reset()

    def reset(self):
        self.report = None     # dict báo cáo cấu trúc gần nhất
        self.count = 0         # số dòng full_transcript đã gộp

    def pending(self, transcript):
        return transcript[self.count:]

    def should_run(self, transcript, final):
        new_text = " ".join(self.pending(transcript)).strip()
        if not new_text and not self.report:
            return False
        if not new_text and not final:
            return False
        return True

    def build_request(self, transcript, target_lang, final,
                      model_fast="gpt-4o-mini", model_final="gpt-4o"):
        """Trả (model, messages, snapshot_count, new_text, prev_report).
        model_fast/model_final cho phép GUI bơm model nhà cung cấp khác (vd Gemini)."""
        new_lines = self.pending(transcript)
        new_text = " ".join(new_lines).strip()
        snapshot = len(transcript)
        prev = json.dumps(self.report, ensure_ascii=False) if self.report else "null"

        system_prompt = f"""Role: Universal Professional Meeting Secretary maintaining a LIVE rolling report.
You are given [PREVIOUS_REPORT] (a JSON object, may be null on the first run) and [NEW_TRANSCRIPT] (the latest speech since the last update).
Task: MERGE [NEW_TRANSCRIPT] into [PREVIOUS_REPORT] and RETURN THE COMPLETE, UPDATED report as a single JSON object.
Output Language: ALL content MUST be written in {target_lang}.

STRICT RULES:
1. PRESERVE everything already in PREVIOUS_REPORT. Never delete, drop or shorten earlier topics, keywords, decisions or action items.
2. Append new dialogue under the most relevant existing topic, or create a new topic block only when it is genuinely a new theme.
3. Keep raw keywords, numeric metrics, proper names and codes verbatim. Do NOT use vague phrases like "etc." or "and other issues".
4. Capture individual viewpoints, disagreements and debates per speaker. Never smooth over personal arguments.
5. Keep "meeting_brief_overview" clear, readable and concise, even though the discussion ledger stays fully detailed.

Return ONLY a valid JSON object matching EXACTLY this schema (no markdown, no commentary):
{REPORT_SCHEMA}"""

        if final:
            system_prompt += ("\n\nFINAL PASS: This is the closing version of the meeting. "
                              "Merge duplicate topics, fix inconsistencies and make the overview polished and easy to read, "
                              "but DO NOT remove any factual point, keyword, decision or action item.")

        user_content = (f"[PREVIOUS_REPORT]\n{prev}\n\n"
                        f"[NEW_TRANSCRIPT]\n{new_text if new_text else '(no new speech, just refine and polish the existing report)'}")

        model = model_final if final else model_fast
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        return model, messages, snapshot, new_text, prev

    def apply(self, data, snapshot):
        self.report = data
        self.count = snapshot


def mock_summary_data(prev_report, new_text, target_lang, final):
    """Sinh báo cáo GIẢ (không gọi API) để test chế độ --mock-ai. Vẫn tuân thủ rolling
    (mỗi lần append thêm 1 topic, giữ nguyên topic cũ)."""
    if prev_report:
        report = json.loads(json.dumps(prev_report))  # deep copy
    else:
        report = {
            "meeting_brief_overview": "",
            "detailed_discussion_ledger": [],
            "unresolved_issues_pool": [],
            "action_items_tracking": [],
        }
    idx = len(report["detailed_discussion_ledger"]) + 1
    tag = "FINAL" if final else "LIVE"
    report["meeting_brief_overview"] = (
        f"[MOCK {tag} | lang={target_lang}] Bản tổng hợp giả lập, hiện có {idx} khối thảo luận."
    )
    report["detailed_discussion_ledger"].append({
        "topic": f"Demo topic #{idx}",
        "context_or_issues_raised": (new_text[:200] or "(refine pass — không có dòng mới)"),
        "individual_arguments_and_debates": [
            "Speaker A: " + (new_text[:60] or "..."),
            "Speaker B: phản hồi giả lập cho khối #%d" % idx,
        ],
        "essential_keywords_and_terms": ["DEMO", "MOCK", "R&D", f"BLOCK-{idx}"],
        "decisions_or_next_steps": ["Kiểm thử pipeline rolling end-to-end"],
    })
    report["action_items_tracking"].append(f"danhtu - Test rolling summary khối #{idx} - In progress")
    return report


# =====================================================================
#  XUẤT BÁO CÁO MARKDOWN (Export Data)
# =====================================================================
def seconds_to_mmss(sec):
    try:
        sec = int(sec or 0)
    except Exception:
        sec = 0
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _md_line(text):
    return (text or "").replace("\n", " ").strip()


def build_markdown(committed_blocks, summary_report, meta=None):
    """Dựng chuỗi Markdown: phần Tổng hợp + 2 phần tách biệt (Bản gốc / Bản dịch),
    mỗi câu một dòng kèm dấu thời gian mờ để dễ đọc liền mạch."""
    L = ["# Avbuds AI — Biên bản & Bản dịch", ""]
    if meta:
        if meta.get("exported_at"):
            L.append(f"- **Xuất lúc:** {meta['exported_at']}")
        if meta.get("output_lang"):
            L.append(f"- **Ngôn ngữ đích:** {meta['output_lang']}")
        if meta.get("source"):
            L.append(f"- **Nguồn:** {meta['source']}")
        L.append("")

    r = summary_report or {}
    if r:
        L.append("## 📊 Tổng hợp")
        L.append("")
        if r.get("meeting_brief_overview"):
            L.append("### Tổng quan")
            L.append(str(r["meeting_brief_overview"]))
            L.append("")
        for item in r.get("detailed_discussion_ledger", []):
            L.append(f"### 📌 {item.get('topic', '(không tên)')}")
            if item.get("context_or_issues_raised"):
                L.append(f"- **Bối cảnh:** {item['context_or_issues_raised']}")
            for arg in item.get("individual_arguments_and_debates", []):
                L.append(f"  - 💬 {arg}")
            kws = item.get("essential_keywords_and_terms", [])
            if kws:
                L.append(f"- **Từ khóa:** {', '.join(str(k) for k in kws)}")
            for step in item.get("decisions_or_next_steps", []):
                L.append(f"  - ✔ {step}")
            L.append("")
        if r.get("unresolved_issues_pool"):
            L.append("### ❓ Vấn đề chưa giải quyết")
            for x in r["unresolved_issues_pool"]:
                L.append(f"- {x}")
            L.append("")
        if r.get("action_items_tracking"):
            L.append("### 📋 Việc cần làm")
            for x in r["action_items_tracking"]:
                L.append(f"- [ ] {x}")
            L.append("")

    blocks = committed_blocks or []

    # --- Phần GỐC (liền mạch) ---
    L.append("## 🎤 Bản gốc (Original)")
    L.append("")
    for b in blocks:
        orig = _md_line(b.get("orig"))
        if orig:
            L.append(f"`[{b.get('time', '')}]` {orig}")
            L.append("")

    # --- Phần DỊCH (liền mạch) ---
    L.append("## ✅ Bản dịch (Translation)")
    L.append("")
    for b in blocks:
        trans = _md_line(b.get("trans"))
        if trans:
            L.append(f"`[{b.get('time', '')}]` {trans}")
            L.append("")

    return "\n".join(L)


# =====================================================================
#  GLOSSARY — bộ từ vựng chuyên ngành (cải thiện STT + dịch nhất quán)
# =====================================================================
# map mã ngôn ngữ -> field trong glossary (chỉ en/ko/vi có sẵn dữ liệu)
_GLOSS_LANG_FIELD = {"en": "en", "ko": "ko", "vi": "vi"}


def _term_forms(s):
    """Tách 1 chuỗi thuật ngữ thành các DẠNG riêng: phần chính (trước ngoặc) + nội dung
    trong ngoặc. vd 'surface mount technology (SMT)' -> ['surface mount technology','SMT'];
    '콘덴서 (커패시터)' -> ['콘덴서','커패시터']."""
    if not s:
        return []
    forms = []
    main = re.sub(r"\([^)]*\)", "", s).strip()
    if main:
        forms.append(main)
    for inside in re.findall(r"\(([^)]*)\)", s):
        inside = inside.strip()
        if inside:
            forms.append(inside)
    return forms


class Glossary:
    """Bộ từ vựng chuyên ngành nạp từ glossary.json (thuần stdlib, test được không cần GUI).
    - build_soniox_context(): dựng object 'context' cho Soniox (terms + translation_terms + general).
    - correct_text(): sửa cụm 'nghe sai' theo 'corrections' đã HỌC (cặp wrong->right).
    - add_term / add_correction / save: học & ghi nhớ qua các phiên."""

    def __init__(self, terms=None, keep_original=None, corrections=None, meta=None):
        self.terms = list(terms or [])            # list dict {en,ko,vi,cat}
        self.keep_original = list(keep_original or [])
        self.corrections = dict(corrections or {})  # {wrong_lower: right}
        self.meta = dict(meta or {})
        self._corr_compiled = []
        self._compile_corrections()

    @classmethod
    def load(cls, path):
        """Nạp từ file JSON; lỗi/thiếu file -> Glossary rỗng (an toàn, không crash app)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return cls()
        return cls(terms=data.get("terms", []),
                   keep_original=data.get("keep_original", []),
                   corrections=data.get("corrections", {}),
                   meta=data.get("meta", {}))

    def to_dict(self):
        return {"meta": self.meta, "keep_original": self.keep_original,
                "terms": self.terms, "corrections": self.corrections}

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    # ---- Context cho Soniox (Lớp 1 nhận dạng + Lớp 3 dịch nhất quán) ----
    def all_term_strings(self, langs=("en", "ko", "vi")):
        """Danh sách phẳng mọi DẠNG thuật ngữ (dedup theo lower, giữ thứ tự) + keep_original."""
        out, seen = [], set()

        def add(w):
            w = (w or "").strip()
            if w and w.lower() not in seen:
                seen.add(w.lower()); out.append(w)

        for w in self.keep_original:
            add(w)
        for t in self.terms:
            for lang in langs:
                for form in _term_forms(t.get(lang, "")):
                    add(form)
        return out

    def build_soniox_context(self, source_code=None, target_code=None,
                             translate=True, max_terms=400, max_chars=9000):
        """Dựng object 'context' cho Soniox. general/translation_terms (giá trị cao) dựng trước,
        rồi 'terms' (boost nhận dạng) lấp phần CÒN LẠI của ngân sách ký tự (Soniox giới hạn
        ~8000 token ≈ 10000 ký tự — ta chừa mép an toàn). translate=False -> chỉ terms.
        Trả None nếu rỗng."""
        ctx = {}
        dom = self.meta.get("domain")
        if dom:
            ctx["general"] = [{"key": "domain", "value": dom}]
        sf = _GLOSS_LANG_FIELD.get(source_code)
        tf = _GLOSS_LANG_FIELD.get(target_code)
        if translate and sf and tf and sf != tf:
            pairs, seen = [], set()
            for t in self.terms:
                s = _term_forms(t.get(sf, ""))
                d = _term_forms(t.get(tf, ""))
                if s and d:
                    key = (s[0].lower(), d[0].lower())
                    if key not in seen:
                        seen.add(key)
                        pairs.append({"source": s[0], "target": d[0]})
            if pairs:
                ctx["translation_terms"] = pairs
        # 'terms' lấp phần còn lại của ngân sách (cắt bớt nếu tràn để không vượt giới hạn Soniox)
        base = len(json.dumps(ctx, ensure_ascii=False)) if ctx else 2
        budget = max(0, max_chars - base)
        terms, used = [], 2
        for w in self.all_term_strings()[:max_terms]:
            need = len(w) + 4  # ~ dấu ngoặc kép + phẩy
            if used + need > budget:
                break
            terms.append(w); used += need
        if terms:
            ctx["terms"] = terms
        return ctx or None

    # ---- Correction (Lớp 2 — HỌC từ chỗ người dùng sửa) ----
    def _compile_corrections(self):
        # cụm dài khớp trước (tránh khớp một phần)
        items = sorted(self.corrections.items(), key=lambda kv: -len(kv[0]))
        self._corr_compiled = [
            (re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)", re.IGNORECASE), r)
            for w, r in items if w
        ]

    def correct_text(self, text):
        """Thay các cụm 'nghe sai' đã học bằng cụm đúng (khớp nguyên từ, không phân biệt hoa/thường)."""
        if not text or not self._corr_compiled:
            return text
        for pat, right in self._corr_compiled:
            text = pat.sub(right, text)
        return text

    def add_correction(self, wrong, right):
        wrong = (wrong or "").strip()
        right = (right or "").strip()
        if not wrong or not right:
            return False
        self.corrections[wrong.lower()] = right
        self._compile_corrections()
        return True

    def add_term(self, en="", ko="", vi="", cat="custom"):
        en, ko, vi = (en or "").strip(), (ko or "").strip(), (vi or "").strip()
        if not (en or ko or vi):
            return False
        self.terms.append({"en": en, "ko": ko, "vi": vi, "cat": cat})
        return True

    def counts(self):
        return {"terms": len(self.terms), "keep_original": len(self.keep_original),
                "corrections": len(self.corrections)}


# =====================================================================
#  SELF-TEST (python avbuds_core.py)
# =====================================================================
def _selftest():
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}")

    # Clock có thể điều khiển để test fallback
    state = {"t": 1000.0}
    eng = TranscriptEngine(clock=lambda: state["t"], timestr=lambda: "00:00:00")

    print("== TranscriptEngine: Soniox provisional -> final commit ==")
    eng.feed_soniox({"orig_final": "", "orig_prov": "hello", "trans_final": "", "trans_prov": "xin",
                     "orig_has_final": False, "trans_has_final": False})
    left, live_left = eng.left_lines()
    check("provisional hiển thị ở ô trái", "hello" in left and len(eng.committed_blocks) == 0)
    check("subtitle hiện provisional dịch", eng.subtitle_pair()[1] == "xin")

    eng.feed_soniox({"orig_final": "hello world ", "orig_prov": "", "trans_final": "xin chào ", "trans_prov": "",
                     "orig_has_final": True, "trans_has_final": True})
    check("commit tạo 1 block", len(eng.committed_blocks) == 1)
    check("full_transcript có bản dịch", eng.full_transcript == ["xin chào"])
    check("active được giải phóng sau commit", eng.active_orig == "" and eng.active_trans == "")
    left, live_left = eng.left_lines()
    check("ô trái có timestamp + text gốc", left == "[00:00:00] hello world")

    print("== TranscriptEngine: fallback commit khi im lặng ==")
    eng.feed_soniox({"orig_final": "dangling ", "orig_prov": "", "trans_final": "treo ", "trans_prov": "",
                     "orig_has_final": True, "trans_has_final": False})  # chưa có trans final -> chưa commit
    check("chưa commit khi thiếu trans_final", len(eng.committed_blocks) == 1)
    check("fallback không kích hoạt sớm", eng.fallback_commit(5.5) is False)
    state["t"] += 6.0  # trôi 6 giây
    check("fallback kích hoạt sau 6s im lặng", eng.fallback_commit(5.5) is True)
    check("fallback tạo block thứ 2", len(eng.committed_blocks) == 2)

    print("== TranscriptEngine: luồng OpenAI delta/done ==")
    eng2 = TranscriptEngine(clock=lambda: 0.0, timestr=lambda: "12:00:00")
    eng2.feed_openai_orig_delta("Hel")
    eng2.feed_openai_orig_delta("lo")
    eng2.feed_openai_orig_done("")             # fallback lấy từ prov_orig
    check("orig_done gom prov thành active", eng2.active_orig == "Hello")
    eng2.feed_openai_trans_delta("Xin ")
    eng2.feed_openai_trans_delta("chào")
    eng2.feed_openai_trans_done("")            # commit
    check("openai commit tạo block", len(eng2.committed_blocks) == 1)
    check("openai block đúng nội dung", eng2.committed_blocks[0]["orig"] == "Hello" and eng2.committed_blocks[0]["trans"] == "Xin chào")
    check("openai giải phóng prov sau commit", eng2.prov_orig == "" and eng2.prov_trans == "")

    print("== RollingSummary: delta + con trỏ tăng dần ==")
    rs = RollingSummary()
    transcript = ["câu 1", "câu 2"]
    check("should_run=True khi có dòng mới (auto)", rs.should_run(transcript, final=False) is True)
    model, msgs, snap, new_text, prev = rs.build_request(transcript, "Vietnamese", final=False)
    check("auto dùng gpt-4o-mini", model == "gpt-4o-mini")
    check("lần đầu prev_report=null", prev == "null")
    check("new_text gồm toàn bộ 2 câu", new_text == "câu 1 câu 2")
    check("snapshot = 2", snap == 2)

    data1 = mock_summary_data(rs.report, new_text, "Vietnamese", final=False)
    rs.apply(data1, snap)
    check("con trỏ tiến tới 2", rs.count == 2)
    check("report có 1 topic", len(rs.report["detailed_discussion_ledger"]) == 1)

    # Thêm dòng mới -> chỉ gửi phần delta, KHÔNG gửi lại câu cũ (chống đốt tiền)
    transcript.append("câu 3")
    check("should_run=True có delta mới", rs.should_run(transcript, final=False) is True)
    model2, msgs2, snap2, new_text2, prev2 = rs.build_request(transcript, "English", final=True)
    check("final dùng gpt-4o", model2 == "gpt-4o")
    check("delta chỉ chứa câu mới (không gửi lại câu cũ)", new_text2 == "câu 3")
    check("prev_report lần 2 không còn null", prev2 != "null" and "Demo topic #1" in prev2)

    data2 = mock_summary_data(rs.report, new_text2, "English", final=True)
    rs.apply(data2, snap2)
    check("rolling: giữ topic cũ + thêm topic mới (2 topic)", len(rs.report["detailed_discussion_ledger"]) == 2)
    check("con trỏ tiến tới 3", rs.count == 3)

    # Không có dòng mới: auto bỏ qua, final vẫn chạy (đánh bóng)
    check("auto bỏ qua khi không có dòng mới", rs.should_run(transcript, final=False) is False)
    check("final vẫn chạy dù không có dòng mới", rs.should_run(transcript, final=True) is True)
    _, _, _, new_text3, _ = rs.build_request(transcript, "English", final=True)
    check("final không dòng mới -> new_text rỗng", new_text3 == "")

    print("== build_markdown / Export ==")
    check("mmss 0s", seconds_to_mmss(0) == "00:00")
    check("mmss 65s", seconds_to_mmss(65) == "01:05")
    blocks = [
        {"time": "00:00", "orig": "Hello there", "trans": "Xin chào"},
        {"time": "00:05", "orig": "Second line", "trans": "Dòng hai"},
    ]
    md = build_markdown(blocks, rs.report, meta={"output_lang": "Vietnamese", "exported_at": "2026-06-22 10:00"})
    check("markdown có tiêu đề", md.startswith("# Avbuds AI"))
    check("markdown có phần Bản gốc", "## 🎤 Bản gốc (Original)" in md)
    check("markdown có phần Bản dịch", "## ✅ Bản dịch (Translation)" in md)
    check("phần gốc nằm trước phần dịch", md.index("## 🎤 Bản gốc") < md.index("## ✅ Bản dịch"))
    check("dòng gốc có timestamp + text", "`[00:00]` Hello there" in md)
    check("dòng dịch có timestamp + text", "`[00:05]` Dòng hai" in md)
    check("markdown có topic từ report", "Demo topic #1" in md)
    check("markdown rỗng vẫn hợp lệ", isinstance(build_markdown([], None), str))

    print("== Glossary: context + correction ==")
    gl = Glossary(
        terms=[{"en": "impedance", "ko": "임피던스", "vi": "trở kháng", "cat": "acoustics"},
               {"en": "surface mount technology (SMT)", "ko": "표면실장 (SMT)",
                "vi": "hàn dán bề mặt (SMT)", "cat": "process"}],
        keep_original=["CRESYN", "PCB"],
        meta={"domain": "electronics"})
    ctx = gl.build_soniox_context(source_code="en", target_code="vi", translate=True)
    check("context có terms + keep_original", "impedance" in ctx["terms"] and "CRESYN" in ctx["terms"])
    check("context tách dạng trong ngoặc", "SMT" in ctx["terms"] and "surface mount technology" in ctx["terms"])
    check("context có general/domain", ctx["general"][0]["value"] == "electronics")
    check("translation_terms en->vi đúng cặp",
          any(p["source"] == "impedance" and p["target"] == "trở kháng" for p in ctx["translation_terms"]))
    ctx2 = gl.build_soniox_context(source_code="vi", target_code="vi", translate=False)
    check("show-text (translate=False) không có translation_terms", "translation_terms" not in ctx2)
    gl.add_correction("impedin", "impedance")
    check("correct_text sửa từ nghe sai", gl.correct_text("the impedin is high") == "the impedance is high")
    check("correct_text không đụng từ không khớp", gl.correct_text("hello world") == "hello world")
    check("correct_text khớp nguyên từ (không phá substring)", gl.correct_text("impeding") == "impeding")
    check("add_term tăng số lượng", gl.add_term(en="ferrite bead", vi="hạt ferrite") and gl.counts()["terms"] == 3)
    check("Glossary.load file thiếu -> rỗng", Glossary.load("__khong_ton_tai__.json").counts()["terms"] == 0)

    print(f"\n==== KẾT QUẢ: {passed} PASS / {failed} FAIL ====")
    return failed == 0


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ok = _selftest()
    sys.exit(0 if ok else 1)
