import os
import re
import shutil
import subprocess
import tempfile
import threading
import traceback
import uuid
import asyncio
import json
import time

from urllib.parse import urlparse, parse_qs

from flask import Flask, request, jsonify, send_from_directory
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError
from faster_whisper import WhisperModel
from deep_translator import GoogleTranslator, MyMemoryTranslator
import edge_tts

app = Flask(__name__)

# Thư mục lưu video lồng tiếng đã xử lý xong (để phục vụ tải về / xem lại)
OUTPUT_DIR = os.path.join(os.getcwd(), "dubbed_outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Dọn dẹp file output cũ tự động
# ---------------------------------------------------------------------------
# Nếu không dọn, dubbed_outputs/ sẽ phình to vô hạn theo thời gian (mỗi lần dịch
# tạo 1 file video mới, không ai tự xoá) -> đầy đĩa -> server chậm/crash khi deploy
# chạy lâu ngày. Cơ chế dưới đây tự xoá các file cũ hơn OUTPUT_RETENTION_SECONDS,
# chạy nền định kỳ mỗi OUTPUT_CLEANUP_INTERVAL_SECONDS.
#
# Có thể chỉnh qua biến môi trường, ví dụ giữ file 6 tiếng thay vì 2 tiếng:
#   OUTPUT_RETENTION_SECONDS=21600
OUTPUT_RETENTION_SECONDS = int(os.environ.get("OUTPUT_RETENTION_SECONDS", 2 * 60 * 60))  # mặc định 2 tiếng
OUTPUT_CLEANUP_INTERVAL_SECONDS = int(os.environ.get("OUTPUT_CLEANUP_INTERVAL_SECONDS", 15 * 60))  # quét mỗi 15 phút

# Giới hạn tổng dung lượng thư mục output (an toàn thêm, phòng khi retention chưa
# kịp dọn mà traffic tăng đột biến). 0 = không giới hạn.
OUTPUT_MAX_TOTAL_BYTES = int(os.environ.get("OUTPUT_MAX_TOTAL_BYTES", 0))


def cleanup_old_outputs():
    """Xoá các file trong OUTPUT_DIR đã tồn tại lâu hơn OUTPUT_RETENTION_SECONDS.
    Nếu vẫn vượt OUTPUT_MAX_TOTAL_BYTES sau khi xoá theo tuổi, xoá thêm file cũ nhất
    cho đến khi về dưới giới hạn (phòng trường hợp lượng file tạo ra dồn dập)."""
    now = time.time()
    entries = []
    try:
        for filename in os.listdir(OUTPUT_DIR):
            file_path = os.path.join(OUTPUT_DIR, filename)
            if not os.path.isfile(file_path):
                continue
            try:
                mtime = os.path.getmtime(file_path)
                size = os.path.getsize(file_path)
            except OSError:
                continue
            entries.append((file_path, mtime, size))
    except Exception as e:
        print(f"[Dọn dẹp] Lỗi khi quét thư mục output: {e}")
        return

    remaining = []
    for file_path, mtime, size in entries:
        age = now - mtime
        if age > OUTPUT_RETENTION_SECONDS:
            try:
                os.remove(file_path)
                # print(f"[Dọn dẹp] Đã xoá file cũ ({int(age)}s): {os.path.basename(file_path)}")
            except Exception as e:
                print(f"[Dọn dẹp] Không xoá được {file_path}: {e}")
                remaining.append((file_path, mtime, size))
        else:
            remaining.append((file_path, mtime, size))

    if OUTPUT_MAX_TOTAL_BYTES > 0:
        total_size = sum(size for _, _, size in remaining)
        if total_size > OUTPUT_MAX_TOTAL_BYTES:
            # Xoá bớt file cũ nhất trước, cho đến khi về dưới giới hạn dung lượng
            remaining.sort(key=lambda x: x[1])  # sắp theo mtime tăng dần (cũ nhất trước)
            for file_path, mtime, size in remaining:
                if total_size <= OUTPUT_MAX_TOTAL_BYTES:
                    break
                try:
                    os.remove(file_path)
                    total_size -= size
                    print(f"[Dọn dẹp] Xoá bớt do vượt quota dung lượng: {os.path.basename(file_path)}")
                except Exception as e:
                    print(f"[Dọn dẹp] Không xoá được {file_path}: {e}")


def _cleanup_loop():
    while True:
        cleanup_old_outputs()
        time.sleep(OUTPUT_CLEANUP_INTERVAL_SECONDS)


def start_cleanup_thread():
    """Chạy vòng lặp dọn dẹp trong 1 thread nền (daemon), không chặn Flask app."""
    thread = threading.Thread(target=_cleanup_loop, daemon=True)
    thread.start()
    print(
        f"[Dọn dẹp] Đã bật tự động dọn output cũ: giữ tối đa {OUTPUT_RETENTION_SECONDS}s, "
        f"quét mỗi {OUTPUT_CLEANUP_INTERVAL_SECONDS}s."
    )

# Tải model Whisper 1 lần khi khởi động server (model "small": cân bằng tốc độ/độ chính xác)
print("Đang tải model nhận diện giọng nói (lần đầu có thể mất vài phút)...")
model = WhisperModel("small", device="cpu", compute_type="int8")
print("Model đã sẵn sàng! Server đang chạy tại http://127.0.0.1:5000")

# Danh sách ngôn ngữ gợi ý cho dropdown (người dùng vẫn có thể gõ mã ngôn ngữ khác)
LANG_OPTIONS = [
    ("vi", "Tiếng Việt"),
    ("en", "Tiếng Anh"),
    ("ja", "Tiếng Nhật"),
    ("ko", "Tiếng Hàn"),
    ("zh-CN", "Tiếng Trung"),
    ("fr", "Tiếng Pháp"),
    ("es", "Tiếng Tây Ban Nha"),
    ("th", "Tiếng Thái"),
    ("de", "Tiếng Đức"),
]

# Ánh xạ mã ngôn ngữ -> giọng đọc edge-tts (miễn phí, chất lượng tốt), tách riêng nam/nữ
VOICE_MAP = {
    "vi": {"female": "vi-VN-HoaiMyNeural", "male": "vi-VN-NamMinhNeural"},
    "en": {"female": "en-US-AriaNeural", "male": "en-US-GuyNeural"},
    "ja": {"female": "ja-JP-NanamiNeural", "male": "ja-JP-KeitaNeural"},
    "ko": {"female": "ko-KR-SunHiNeural", "male": "ko-KR-InJoonNeural"},
    "zh-CN": {"female": "zh-CN-XiaoxiaoNeural", "male": "zh-CN-YunxiNeural"},
    "fr": {"female": "fr-FR-DeniseNeural", "male": "fr-FR-HenriNeural"},
    "es": {"female": "es-ES-ElviraNeural", "male": "es-ES-AlvaroNeural"},
    "th": {"female": "th-TH-PremwadeeNeural", "male": "th-TH-NiwatNeural"},
    "de": {"female": "de-DE-KatjaNeural", "male": "de-DE-ConradNeural"},
}

DEFAULT_VOICE_GENDER = "female"


def normalize_video_url(url: str) -> str:
    """
    Một số link chia sẻ (đặc biệt là Douyin dạng 'jingxuan?modal_id=...') không phải
    link video chuẩn mà yt-dlp nhận diện được. Hàm này tự động chuyển các link như vậy
    về đúng định dạng '/video/<id>' mà yt-dlp hỗ trợ.
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()

        if "douyin.com" in host:
            qs = parse_qs(parsed.query)
            modal_id = qs.get("modal_id", [None])[0]
            if modal_id:
                return f"https://www.douyin.com/video/{modal_id}"

            # Một số link Douyin nhúng ID trong path dạng khác, thử bắt bằng regex số dài
            match = re.search(r"(\d{15,25})", url)
            if match and "/video/" not in url:
                return f"https://www.douyin.com/video/{match.group(1)}"

    except Exception:
        pass

    return url


def is_douyin_url(url: str) -> bool:
    try:
        return "douyin.com" in urlparse(url).netloc.lower()
    except Exception:
        return False


MAX_DURATION_SECONDS = int(os.environ.get("MAX_VIDEO_DURATION_SECONDS", 600))  # mặc định 10 phút

# User-Agent giả lập trình duyệt thật. Douyin/TikTok đôi khi đối chiếu User-Agent
# với cookie đã lưu, nên NÊN dùng cùng UA với trình duyệt bạn dùng để lấy cookie.
# Có thể ghi đè qua biến môi trường YTDLP_USER_AGENT nếu cần.
DEFAULT_USER_AGENT = os.environ.get(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# Số lần thử lại khi tải/trích xuất thông tin từ Douyin thất bại do lỗi MẠNG/tạm thời.
# LƯU Ý: không dùng để retry lỗi cookie/auth, vì cookie sai thì retry bao nhiêu lần cũng
# vẫn sai như nhau -> chỉ tổ tốn thời gian chờ của người dùng.
DOUYIN_MAX_RETRIES = int(os.environ.get("DOUYIN_MAX_RETRIES", 3))
DOUYIN_RETRY_DELAY_SECONDS = float(os.environ.get("DOUYIN_RETRY_DELAY_SECONDS", 2.0))


# ---------------------------------------------------------------------------
# Phân loại & validate cookie
# ---------------------------------------------------------------------------

# Các cụm từ trong thông báo lỗi của yt-dlp/Douyin thường xuất hiện khi cookie
# không hợp lệ, hết hạn, hoặc bị coi là bot. Dùng để phát hiện SỚM và fail-fast,
# tránh retry vô ích.
COOKIE_ERROR_SIGNATURES = (
    "cookie",
    "login required",
    "please log in",
    "unable to log in",
    "403",
    "forbidden",
    "not available",
    "sign in",
)


def _is_cookie_related_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(sig in msg for sig in COOKIE_ERROR_SIGNATURES)


def validate_cookie_file(cookies_file: str):
    """
    Kiểm tra nhanh file cookies.txt (định dạng Netscape) TRƯỚC khi bắt đầu tải video:
    - File có tồn tại, không rỗng không?
    - Có ít nhất 1 dòng cookie hợp lệ cho domain douyin.com không?
    - Có cookie nào đã hết hạn (theo timestamp trong file) không?

    Trả về (is_valid: bool, message: str). Đây chỉ là kiểm tra sơ bộ để báo lỗi sớm;
    Douyin vẫn có thể từ chối cookie dù file "trông" hợp lệ (vd: bị revoke phía server).
    """
    if not cookies_file:
        return False, "Chưa cấu hình YTDLP_COOKIES_FILE."

    if not os.path.exists(cookies_file):
        return False, f"File cookie không tồn tại: {cookies_file}"

    if os.path.getsize(cookies_file) == 0:
        return False, f"File cookie rỗng: {cookies_file}"

    douyin_lines = 0
    expired_lines = 0
    now = time.time()

    try:
        with open(cookies_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _, _, _, expires, name, _ = parts[:7]
                if "douyin.com" not in domain:
                    continue
                douyin_lines += 1
                try:
                    expires_ts = float(expires)
                    # expires == 0 nghĩa là session cookie (không có hạn cố định trong file)
                    if expires_ts != 0 and expires_ts < now:
                        expired_lines += 1
                except ValueError:
                    pass
    except Exception as e:
        return False, f"Không đọc được file cookie: {e}"

    if douyin_lines == 0:
        return False, (
            "File cookie không chứa cookie nào cho domain douyin.com. "
            "Kiểm tra lại bạn đã export đúng trang (douyin.com) chưa."
        )

    if expired_lines == douyin_lines:
        return False, (
            "Toàn bộ cookie cho douyin.com trong file đã hết hạn theo timestamp. "
            "Cần export cookie mới."
        )

    return True, "ok"


def _friendly_douyin_error(original_error: Exception) -> RuntimeError:
    return RuntimeError(
        "Douyin từ chối yêu cầu vì cookie không hợp lệ hoặc đã hết hạn. "
        "Hãy mở lại Douyin trong trình duyệt (cửa sổ ẩn danh mới), truy cập đúng "
        "trang video này cho đến khi xem được, rồi xuất lại cookie mới bằng extension "
        "'Get cookies.txt LOCALLY' và cập nhật biến môi trường YTDLP_COOKIES_FILE. "
        "Đồng thời kiểm tra User-Agent dùng để lấy cookie có khớp với cấu hình server không."
    )


class DouyinCookieError(RuntimeError):
    """Lỗi riêng cho cookie Douyin hỏng/hết hạn, để phân biệt với lỗi mạng thông thường
    ở tầng route (giúp trả về error_type khác cho frontend)."""
    pass


def _build_ydl_opts(out_template: str, url: str) -> dict:
    ydl_opts = {
        # Bỏ ràng buộc ext=m4a: một số site (Douyin...) không có audio định dạng m4a riêng,
        # ép buộc dễ khiến yt-dlp fallback về file không có audio -> gây lỗi khi transcribe.
        "format": "bestvideo*+bestaudio/best",
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": DEFAULT_USER_AGENT,
        },
    }

    if is_douyin_url(url):
        # Douyin cần Referer hợp lệ trỏ về chính site, nếu không rất dễ bị coi là bot
        ydl_opts["http_headers"]["Referer"] = "https://www.douyin.com/"
        # Ép dùng API host ổn định hơn cho một số phiên bản extractor TikTok/Douyin
        ydl_opts["extractor_args"] = {
            "tiktok": {"api_hostname": ["api22-normal-c-useast2a.tiktokv.com"]},
        }

    # Một số site (đặc biệt Douyin) yêu cầu cookie hợp lệ để chống bot.
    # Cấu hình qua biến môi trường, không cần sửa code mỗi lần:
    #   YTDLP_COOKIES_FILE=C:\path\to\cookies.txt   -> ưu tiên dùng trước (ổn định, không bị khóa file)
    #   YTDLP_COOKIES_BROWSER=chrome                -> chỉ dùng nếu không có COOKIES_FILE
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    cookies_browser = os.environ.get("YTDLP_COOKIES_BROWSER")
    if cookies_file:
        if not os.path.exists(cookies_file):
            print(f"[Cookie] CẢNH BÁO: đường dẫn cookie không tồn tại: {cookies_file}")
        ydl_opts["cookiefile"] = cookies_file
        print(f"[Cookie] Đang dùng file cookie: {cookies_file}")
    elif cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_browser,)
        print(f"[Cookie] Đang lấy cookie trực tiếp từ trình duyệt: {cookies_browser}")
    else:
        print("[Cookie] Không có cookie nào được cấu hình (YTDLP_COOKIES_FILE / YTDLP_COOKIES_BROWSER).")

    return ydl_opts


def download_video(url: str, out_dir: str) -> str:
    """Tải cả video (kèm audio gốc) về, trả về đường dẫn file mp4."""
    url = normalize_video_url(url)
    out_template = os.path.join(out_dir, "source.%(ext)s")
    ydl_opts = _build_ydl_opts(out_template, url)

    douyin = is_douyin_url(url)

    # --- Validate cookie SỚM cho Douyin, trước khi tốn thời gian gọi mạng ---
    if douyin:
        cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
        is_valid, msg = validate_cookie_file(cookies_file)
        if not is_valid:
            print(f"[Cookie] Kiểm tra sơ bộ thất bại: {msg}")
            raise DouyinCookieError(str(_friendly_douyin_error(RuntimeError(msg))))

    # Douyin: fail-fast nếu lỗi là do cookie/auth (retry không giúp gì).
    # Các lỗi khác (mạng chập chờn...) vẫn được retry như cũ.
    max_attempts = DOUYIN_MAX_RETRIES if douyin else 1

    last_error = None
    info = None
    for attempt in range(1, max_attempts + 1):
        try:
            with YoutubeDL(ydl_opts) as ydl:
                # Kiểm tra thời lượng TRƯỚC khi tải, tránh tốn thời gian tải video quá dài rồi mới báo lỗi
                info = ydl.extract_info(url, download=False)
                duration = info.get("duration")
                if duration and duration > MAX_DURATION_SECONDS:
                    raise RuntimeError(
                        f"Video dài {int(duration // 60)} phút {int(duration % 60)} giây, "
                        f"vượt quá giới hạn cho phép ({MAX_DURATION_SECONDS // 60} phút). "
                        f"Vui lòng chọn video ngắn hơn."
                    )

                ydl.download([url])
            last_error = None
            break
        except RuntimeError:
            # Lỗi về thời lượng video, không cần thử lại
            raise
        except (DownloadError, ExtractorError) as e:
            last_error = e
            print(f"[Tải video] Lần thử {attempt}/{max_attempts} thất bại: {e}")
            if douyin and _is_cookie_related_error(e):
                # Cookie sai -> mọi lần thử tiếp theo cũng sẽ sai y hệt, dừng ngay
                # thay vì chờ thêm DOUYIN_RETRY_DELAY_SECONDS * attempt vô ích.
                print("[Tải video] Phát hiện lỗi liên quan cookie -> dừng retry sớm.")
                break
            if attempt < max_attempts:
                time.sleep(DOUYIN_RETRY_DELAY_SECONDS * attempt)
        except Exception as e:
            last_error = e
            print(f"[Tải video] Lần thử {attempt}/{max_attempts} thất bại: {e}")
            if attempt < max_attempts:
                time.sleep(DOUYIN_RETRY_DELAY_SECONDS * attempt)

    if last_error is not None:
        if douyin and _is_cookie_related_error(last_error):
            raise DouyinCookieError(str(_friendly_douyin_error(last_error)))
        if douyin:
            raise RuntimeError(f"Không tải được video từ Douyin: {last_error}")
        raise RuntimeError(f"Không tải được video từ link này: {last_error}")

    video_path = os.path.join(out_dir, "source.mp4")
    if not os.path.exists(video_path):
        raise RuntimeError("Không tải được video từ link này. Kiểm tra lại link hoặc thử link khác.")

    # Kiểm tra file tải về có audio track không, báo lỗi rõ ràng thay vì để Whisper crash khó hiểu
    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "json", video_path,
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(probe_result.stdout)
        if not probe_data.get("streams"):
            raise RuntimeError(
                "Video tải về không có track âm thanh (có thể do site nguồn không cung cấp audio "
                "cho định dạng này, hoặc thiếu cookie hợp lệ). Thử lại hoặc dùng link khác."
            )
    except subprocess.CalledProcessError:
        pass  # Nếu ffprobe lỗi vì lý do khác, để bước transcribe tự báo lỗi tiếp

    return video_path


def transcribe(video_path: str):
    """Nhận diện giọng nói trực tiếp từ file video, trả về (văn bản gốc, mã ngôn ngữ, danh sách segment có timestamp)."""
    segments, info = model.transcribe(video_path, beam_size=5)
    segments = list(segments)
    full_text = " ".join(seg.text.strip() for seg in segments)
    return full_text.strip(), info.language, segments


# faster-whisper trả về mã ngôn ngữ kiểu ISO ngắn gọn (vd: "zh"), nhưng deep-translator
# (cả Google lẫn MyMemory) lại cần mã cụ thể hơn cho một số ngôn ngữ (vd: "zh-CN").
# Bảng này ánh xạ lại cho đúng trước khi gọi dịch.
WHISPER_TO_TRANSLATOR_LANG = {
    "zh": "zh-CN",
    "he": "iw",       # deep-translator dùng mã cũ "iw" cho tiếng Do Thái
    "jw": "jv",        # Javanese
}


def normalize_source_lang(lang_code: str) -> str:
    if not lang_code:
        return lang_code
    return WHISPER_TO_TRANSLATOR_LANG.get(lang_code, lang_code)


def _translate_one_chunk(text: str, source_lang: str, target_lang: str) -> str:
    """Dịch 1 đoạn văn bản: thử Google Translate trước, nếu thất bại/không đổi thì thử MyMemory."""
    text = text.strip()
    if not text:
        return ""

    source_lang = normalize_source_lang(source_lang)

    google_error = None
    try:
        result = GoogleTranslator(source=source_lang or "auto", target=target_lang).translate(text)
        if result and result.strip() and result.strip().lower() != text.lower():
            return result
    except Exception as e:
        google_error = e
        print(f"[Dịch] Google Translate lỗi: {e}")

    mymemory_error = None
    try:
        src = source_lang if source_lang and source_lang != "auto" else "en"
        result = MyMemoryTranslator(source=src, target=target_lang).translate(text)
        if result and result.strip():
            return result
    except Exception as e:
        mymemory_error = e
        print(f"[Dịch] MyMemory lỗi: {e}")

    print(f"[Dịch] Cả 2 dịch vụ đều thất bại cho đoạn: '{text[:50]}...' "
          f"(Google: {google_error}, MyMemory: {mymemory_error})")
    return text


def split_into_chunks_by_sentence(text: str, chunk_size: int = 450):
    """
    Chia văn bản thành các đoạn nhỏ hơn chunk_size, nhưng luôn cắt tại ranh giới câu
    (kết thúc bằng . ! ? 。！？ hoặc xuống dòng) thay vì cắt cứng theo số ký tự.
    Việc cắt giữa câu là nguyên nhân chính khiến bản dịch trước đây bị sai ngữ cảnh,
    cụt nghĩa hoặc lặp từ ở ranh giới các đoạn.
    """
    text = text.strip()
    if not text:
        return []

    # Tách câu, giữ lại dấu kết câu
    sentences = re.split(r"(?<=[.!?。！？])\s+", text)

    chunks = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue

        if len(current) + len(sent) + 1 <= chunk_size:
            current = (current + " " + sent).strip() if current else sent
        else:
            if current:
                chunks.append(current)
                current = ""

            if len(sent) > chunk_size:
                # 1 câu đơn lẻ đã dài hơn giới hạn -> đành cắt cứng riêng câu này
                for i in range(0, len(sent), chunk_size):
                    chunks.append(sent[i:i + chunk_size])
            else:
                current = sent

    if current:
        chunks.append(current)

    return chunks


def translate_text(text: str, target_lang: str, source_lang: str = None) -> str:
    if not text.strip():
        return ""
    chunks = split_into_chunks_by_sentence(text, chunk_size=450)
    translated_chunks = [_translate_one_chunk(c, source_lang, target_lang) for c in chunks]
    return " ".join(translated_chunks)


def translate_segments(segments, target_lang: str, source_lang: str = None):
    """Dịch riêng từng câu (segment) để giữ được mốc thời gian, phục vụ lồng tiếng."""
    translated = []
    for seg in segments:
        translated.append(_translate_one_chunk(seg.text, source_lang, target_lang))
    return translated


def resolve_voice(target_lang: str, voice_gender: str):
    """Lấy giọng đọc edge-tts theo ngôn ngữ + giới tính. Trả về (voice, error_message)."""
    voice_gender = (voice_gender or DEFAULT_VOICE_GENDER).strip().lower()
    if voice_gender not in ("male", "female"):
        voice_gender = DEFAULT_VOICE_GENDER

    lang_voices = VOICE_MAP.get(target_lang)
    if not lang_voices:
        return None, (
            f"Chưa hỗ trợ giọng đọc lồng tiếng cho ngôn ngữ '{target_lang}'. "
            f"Hiện hỗ trợ: {', '.join(VOICE_MAP.keys())}."
        )

    voice = lang_voices.get(voice_gender)
    if not voice:
        return None, f"Chưa có giọng '{voice_gender}' cho ngôn ngữ '{target_lang}'."

    return voice, None


def tts_to_file(text: str, voice: str, out_path: str, max_retries: int = 3):
    """Tổng hợp giọng đọc (TTS) cho 1 đoạn văn bản bằng edge-tts, tự thử lại nếu lỗi tạm thời."""
    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(out_path)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            asyncio.run(_run())
            return
        except Exception as e:
            last_error = e
            time.sleep(1.5 * attempt)  # chờ tăng dần rồi thử lại (tránh bị rate-limit)

    raise RuntimeError(f"edge-tts thất bại sau {max_retries} lần thử: {last_error}")


def _atempo_filter_chain(factor: float) -> str:
    """
    ffmpeg atempo chỉ nhận hệ số trong khoảng [0.5, 2.0] mỗi lần áp dụng.
    Hàm này chia factor thành chuỗi nhiều atempo nối tiếp nếu cần, để hỗ trợ
    tăng/giảm tốc vượt ngoài khoảng đó mà vẫn giữ nguyên cao độ giọng nói.
    """
    factor = max(0.05, factor)
    filters = []
    remaining = factor
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


def speed_up_audio(in_path: str, out_path: str, speed_factor: float):
    """
    Tăng tốc 1 đoạn audio theo speed_factor (>1 nghĩa là nói nhanh hơn) mà VẪN GIỮ
    NGUYÊN cao độ giọng (khác với tăng tốc thô làm giọng bị "chipmunk").
    Dùng để nén đoạn lồng tiếng cho vừa khoảng thời gian còn trống trước câu kế tiếp,
    tránh tình trạng audio đè lên nhau khi câu dịch dài hơn câu gốc.
    """
    if abs(speed_factor - 1.0) < 0.02:
        shutil.copyfile(in_path, out_path)
        return

    filter_chain = _atempo_filter_chain(speed_factor)
    cmd = [
        "ffmpeg", "-y",
        "-i", in_path,
        "-filter:a", filter_chain,
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def get_media_duration(path: str) -> float:
    """Lấy thời lượng (giây) của file media bằng ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def build_dubbed_audio(segment_audio_paths, segment_starts_ms, total_duration_sec, out_path):
    """Ghép các đoạn audio lồng tiếng vào đúng mốc thời gian gốc bằng ffmpeg."""
    if not segment_audio_paths:
        raise RuntimeError("Không tạo được đoạn lồng tiếng nào (có thể video không có lời thoại).")

    cmd = ["ffmpeg", "-y"]
    for p in segment_audio_paths:
        cmd += ["-i", p]

    filter_parts = []
    mix_labels = []
    for i, start_ms in enumerate(segment_starts_ms):
        start_ms = max(0, int(start_ms))
        filter_parts.append(f"[{i}:a]adelay={start_ms}|{start_ms}[a{i}]")
        mix_labels.append(f"[a{i}]")

    n = len(segment_audio_paths)
    filter_parts.append(f"{''.join(mix_labels)}amix=inputs={n}:duration=longest:normalize=0[mixed]")
    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[mixed]",
        "-t", str(total_duration_sec),
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def mux_video_with_audio(video_path, audio_path, out_path):
    """
    Ghép audio lồng tiếng mới vào video gốc, THAY THẾ HOÀN TOÀN audio gốc.
    CHÚ Ý: không dùng "-shortest" ở đây, vì nếu các câu lồng tiếng bị đẩy trễ (để
    tránh đè lên nhau) khiến audio dài hơn video gốc một chút, "-shortest" sẽ cắt
    mất phần lời thoại còn lại. Video sẽ dừng ở khung hình cuối trong khi audio
    tiếp tục phát nốt cho hết câu.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def mux_video_with_background_audio(video_path, dubbed_audio_path, out_path, original_volume: float = 0.25):
    """
    Ghép giọng lồng tiếng ĐÈ LÊN audio gốc (thay vì thay thế hoàn toàn):
    - Audio gốc được giữ lại nhưng giảm âm lượng còn `original_volume` (0.0 - 1.0)
      để nghe mờ ở nền, không lấn át giọng lồng tiếng.
    - Giọng lồng tiếng giữ nguyên âm lượng, mix đè lên trên.
    Không dùng "-shortest" vì lý do tương tự mux_video_with_audio: tránh cắt mất
    câu lồng tiếng bị đẩy trễ ở cuối video.
    """
    original_volume = max(0.0, min(1.0, original_volume))
    filter_complex = (
        f"[0:a]volume={original_volume}[bg];"
        f"[bg][1:a]amix=inputs=2:duration=longest:normalize=0[aout]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", dubbed_audio_path,
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@app.route("/")
def index():
    return jsonify({"status": "ok", "message": "Video translator backend đang chạy."})


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/check-cookies", methods=["GET"])
def check_cookies_endpoint():
    """Endpoint tiện lợi để kiểm tra nhanh cookie Douyin có còn hợp lệ (theo timestamp)
    trước khi thực sự chạy job dịch, thay vì phải chờ cả video xử lý xong mới biết."""
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
    is_valid, msg = validate_cookie_file(cookies_file)
    return jsonify({"valid": is_valid, "message": msg, "cookies_file": cookies_file})


@app.route("/voices", methods=["GET"])
def voices_endpoint():
    """Trả về danh sách ngôn ngữ + các giới tính giọng đọc khả dụng, để frontend dựng dropdown."""
    return jsonify({
        "languages": [{"code": code, "label": label} for code, label in LANG_OPTIONS],
        "voice_map": VOICE_MAP,
    })


@app.route("/cleanup-outputs", methods=["POST"])
def cleanup_outputs_endpoint():
    """Cho phép kích hoạt dọn dẹp output ngay lập tức (thay vì chờ chu kỳ tự động),
    hữu ích khi cần giải phóng đĩa gấp hoặc gọi từ cron job ngoài."""
    before = len(os.listdir(OUTPUT_DIR)) if os.path.exists(OUTPUT_DIR) else 0
    cleanup_old_outputs()
    after = len(os.listdir(OUTPUT_DIR)) if os.path.exists(OUTPUT_DIR) else 0
    return jsonify({
        "removed": before - after,
        "remaining": after,
        "retention_seconds": OUTPUT_RETENTION_SECONDS,
    })


@app.route("/translate", methods=["POST"])
def translate_endpoint():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    target_lang = (data.get("target_lang") or "vi").strip()
    dub_video = bool(data.get("dub_video", False))
    voice_gender = (data.get("voice_gender") or DEFAULT_VOICE_GENDER).strip().lower()
    if voice_gender not in ("male", "female"):
        voice_gender = DEFAULT_VOICE_GENDER

    # Mặc định GIỮ LẠI âm thanh gốc ở nền (nhỏ tiếng hơn) thay vì thay thế hoàn toàn.
    keep_original_audio = bool(data.get("keep_original_audio", True))
    try:
        original_audio_volume = float(data.get("original_audio_volume", 0.25))
    except (TypeError, ValueError):
        original_audio_volume = 0.25
    original_audio_volume = max(0.0, min(1.0, original_audio_volume))

    if not url:
        return jsonify({"error": "Vui lòng nhập link video."}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vid2text_")
    try:
        video_path = download_video(url, tmp_dir)
        full_text, detected_lang, segments = transcribe(video_path)
        translated_text = translate_text(full_text, target_lang, source_lang=detected_lang)

        warning = None
        if full_text.strip() and translated_text.strip().lower() == full_text.strip().lower():
            warning = (
                "Không dịch được văn bản (cả Google Translate và MyMemory đều thất bại). "
                "Có thể do mất mạng hoặc bị chặn tạm thời. Hãy thử lại sau ít phút."
            )

        response = {
            "detected_lang": detected_lang,
            "original_text": full_text,
            "translated_text": translated_text,
            "dubbed_video_url": None,
            "voice_gender": voice_gender,
            "warning": warning,
        }

        if dub_video:
            voice, voice_err = resolve_voice(target_lang, voice_gender)
            if voice_err:
                response["dub_error"] = voice_err
            elif not segments:
                response["dub_error"] = "Không phát hiện lời thoại nào trong video để lồng tiếng."
            else:
                translated_segments = translate_segments(segments, target_lang, source_lang=detected_lang)
                video_duration = get_media_duration(video_path)

                # --- Lập lịch tuần tự để đảm bảo các câu KHÔNG BAO GIỜ đè lên nhau,
                # đồng thời cố gắng khớp ĐÚNG khung thời gian câu nói gốc ---
                # Với mỗi câu, ta lấy target = (seg.end - seg.start) tức thời lượng câu đó
                # được nói trong video gốc, rồi tăng/giảm tốc audio dịch cho vừa khung đó,
                # để giọng lồng tiếng bắt đầu và kết thúc gần sát với lúc người nói gốc
                # bắt đầu/kết thúc câu (khớp thời gian phát âm), thay vì chỉ né khoảng trống
                # tới câu kế tiếp một cách máy móc.
                # Dù vậy, việc bắt đầu câu vẫn được chốt qua cursor_ms để đảm bảo tuyệt đối
                # không audio nào đè lên audio trước, kể cả khi khung thời gian gốc quá khít.
                MIN_SPEEDUP = 0.85   # cho phép nói CHẬM lại chút nếu câu dịch ngắn hơn gốc, khớp nhịp tốt hơn
                MAX_SPEEDUP = 1.6    # giới hạn tăng tốc để giọng không bị biến dạng quá mức
                MIN_GAP_MS = 60      # khoảng nghỉ tối thiểu giữa các câu
                MIN_TARGET_MS = 300  # sàn tối thiểu cho khung thời gian mục tiêu (câu quá ngắn/lỗi timestamp)

                seg_audio_paths = []
                seg_starts_ms = []
                failed_count = 0
                cursor_ms = 0

                for i, (seg, t_text) in enumerate(zip(segments, translated_segments)):
                    if not t_text.strip():
                        continue

                    raw_path = os.path.join(tmp_dir, f"seg_{i}_raw.mp3")
                    try:
                        tts_to_file(t_text, voice, raw_path)
                    except Exception as e:
                        failed_count += 1
                        print(f"[Lồng tiếng] Bỏ qua câu {i} do lỗi TTS: {e}")
                        time.sleep(0.3)
                        continue

                    raw_duration_ms = get_media_duration(raw_path) * 1000
                    original_start_ms = max(0, int(round(seg.start * 1000)))
                    original_end_ms = max(original_start_ms, int(round(seg.end * 1000)))
                    target_duration_ms = max(MIN_TARGET_MS, original_end_ms - original_start_ms)

                    speed_factor = raw_duration_ms / target_duration_ms if target_duration_ms > 0 else 1.0
                    speed_factor = max(MIN_SPEEDUP, min(MAX_SPEEDUP, speed_factor))

                    seg_path = os.path.join(tmp_dir, f"seg_{i}.mp3")
                    try:
                        speed_up_audio(raw_path, seg_path, speed_factor)
                    except Exception as e:
                        print(f"[Lồng tiếng] Nén tốc độ câu {i} thất bại, dùng bản gốc: {e}")
                        seg_path = raw_path

                    final_duration_ms = get_media_duration(seg_path) * 1000

                    # Chốt mốc bắt đầu thực tế: ưu tiên đúng mốc gốc, nhưng KHÔNG BAO GIỜ
                    # được sớm hơn lúc câu trước vừa nói xong (cursor_ms) -> đảm bảo không đè tiếng
                    # dù khớp thời gian gốc có thể khiến câu này bắt đầu trễ hơn dự kiến một chút.
                    start_ms = max(original_start_ms, cursor_ms)

                    seg_audio_paths.append(seg_path)
                    seg_starts_ms.append(start_ms)
                    cursor_ms = start_ms + final_duration_ms + MIN_GAP_MS

                    time.sleep(0.3)  # nghỉ ngắn giữa các lần gọi để tránh bị rate-limit

                if not seg_audio_paths:
                    response["dub_error"] = (
                        "Không tạo được bất kỳ đoạn lồng tiếng nào (edge-tts thất bại toàn bộ). "
                        "Kiểm tra lại kết nối mạng hoặc cập nhật edge-tts: pip install -U edge-tts"
                    )
                else:
                    # Tổng thời lượng audio cuối cùng có thể dài hơn video gốc một chút nếu
                    # nhiều câu bị đẩy trễ, nên lấy max giữa 2 giá trị để không cắt mất tiếng.
                    duration = max(video_duration, cursor_ms / 1000.0)
                    dubbed_audio_path = os.path.join(tmp_dir, "dubbed_audio.m4a")
                    build_dubbed_audio(seg_audio_paths, seg_starts_ms, duration, dubbed_audio_path)

                    out_filename = f"dubbed_{uuid.uuid4().hex}.mp4"
                    out_path = os.path.join(OUTPUT_DIR, out_filename)

                    if keep_original_audio:
                        mux_video_with_background_audio(
                            video_path, dubbed_audio_path, out_path,
                            original_volume=original_audio_volume,
                        )
                    else:
                        mux_video_with_audio(video_path, dubbed_audio_path, out_path)

                    response["dubbed_video_url"] = f"/outputs/{out_filename}"
                    response["voice_used"] = voice
                    response["keep_original_audio"] = keep_original_audio
                    response["output_retention_seconds"] = OUTPUT_RETENTION_SECONDS
                    if keep_original_audio:
                        response["original_audio_volume"] = original_audio_volume
                    if failed_count:
                        response["dub_error"] = (
                            f"Lồng tiếng thành công nhưng bỏ qua {failed_count} câu bị lỗi TTS "
                            f"(video có thể thiếu vài đoạn lời nói)."
                        )

        # print("=== DEBUG response gửi về frontend ===")
        # print(response)
        # print("dub_video nhận được từ request:", dub_video, "| voice_gender:", voice_gender)
        # print("=======================================")

        return jsonify(response)
    except DouyinCookieError as e:
        # Lỗi cookie: trả về error_type riêng để frontend hiển thị hướng dẫn rõ ràng hơn
        # thay vì gộp chung với lỗi hệ thống khác.
        traceback.print_exc()
        return jsonify({"error": str(e), "error_type": "douyin_cookie_expired"}), 502
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Đã có lỗi xảy ra: {str(e)}"}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    start_cleanup_thread()
    app.run(host="127.0.0.1", port=5000, debug=False)