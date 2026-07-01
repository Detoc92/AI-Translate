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

# Đọc bản dịch (TTS -> MIC ảo): "edge" (Microsoft Edge neural, FREE) hoặc "openai" (gpt-4o-mini-tts)
TTS_PROVIDER = "edge"

# ----- Glossary trên GitHub (cloud) -----
# App TẢI glossary.json từ repo mỗi lần mở (cache offline) và có thể ĐẨY từ mới lên.
GLOSSARY_GITHUB_REPO = "Detoc92/AI-Translate"   # owner/repo chứa glossary.json
GLOSSARY_GITHUB_BRANCH = "main"
GLOSSARY_GITHUB_PATH = "glossary.json"
# Personal Access Token (scope: repo hoặc Contents:write) để THÊM từ mới lên cloud.
# ĐỂ TRỐNG = chỉ ĐỌC (tải về), không đẩy lên. Token này KHÔNG commit (nằm trong config.py).
GLOSSARY_GITHUB_TOKEN = ""
