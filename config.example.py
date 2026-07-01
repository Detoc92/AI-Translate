# ==========================================================================
# config.example.py — MẪU cấu hình. Copy file này thành `config.py` rồi điền key THẬT.
#   cp config.example.py config.py
# LƯU Ý: config.py bị .gitignore (KHÔNG commit) để tránh lộ API key.
# ==========================================================================

# ----- API keys -----
OPENAI_API_KEY = "sk-...your-openai-key..."
SONIOX_API_KEY = "...your-soniox-key..."
MODEL_PATH = "base"           # thư mục model Whisper offline (nếu dùng)
DEFAULT_TARGET_LANG = "en"

# ----- Tiết kiệm chi phí: chọn nhà cung cấp cho từng phần -----
# Tóm tắt biên bản: "gemini" (free tier) hoặc "openai" (gpt-4o, trả phí)
TEXT_PROVIDER = "gemini"
# Lấy key free tại https://aistudio.google.com/apikey (dạng AIza...). Để trống -> tự fallback OpenAI.
GEMINI_API_KEY = "AIza...your-gemini-key..."

# Xử lý file thu sẵn: "soniox" (async, STT+dịch 1 lần) hoặc "openai" (whisper-1 + gpt-4o)
FILE_STT_PROVIDER = "soniox"

# Đọc bản dịch (TTS -> MIC ảo):
#   "edge"   = Microsoft Edge neural, FREE (mặc định)
#   "soniox" = tts-rt-v1, trả phí ~$0.70/giờ, độ trễ thấp + ổn định (dùng KEY SONIOX sẵn có)
#   "openai" = gpt-4o-mini-tts (trả phí)
TTS_PROVIDER = "edge"
SONIOX_TTS_VOICE = "Adrian"   # giọng khi TTS_PROVIDER="soniox" (xem danh mục voice của Soniox)

# Half-duplex (TÙY CHỌN dự phòng, MẶC ĐỊNH TẮT): đang NGHE thì tạm khóa mic NÓI. Gây phân mảnh
# lời nói nên để False; chống loop chính dùng echo-suppression theo nội dung (song công).
HALF_DUPLEX = False

# ----- Glossary trên GitHub (cloud) -----
# App TẢI glossary.json từ repo mỗi lần mở (cache offline) và có thể ĐẨY từ mới lên.
GLOSSARY_GITHUB_REPO = "Detoc92/AI-Translate"   # owner/repo chứa glossary.json
GLOSSARY_GITHUB_BRANCH = "main"
GLOSSARY_GITHUB_PATH = "glossary.json"
# Personal Access Token (fine-grained, quyền Contents:write cho repo) để THÊM từ mới lên cloud.
# ĐỂ TRỐNG = chỉ ĐỌC (tải về). App tìm token theo thứ tự: env AVBUDS_GH_TOKEN →
# file 'github_token.txt' CẠNH .exe (khuyến nghị khi dùng bản đóng gói, không phải build lại)
# → biến này. Token KHÔNG commit (đã .gitignore).
GLOSSARY_GITHUB_TOKEN = ""
