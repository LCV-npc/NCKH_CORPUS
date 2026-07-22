from langdetect import detect, DetectorFactory
from langdetect.lang_detect_exception import LangDetectException

# Đảm bảo kết quả nhất quán
DetectorFactory.seed = 0

def detect_language(text: str) -> str:
    """
    Phát hiện ngôn ngữ của văn bản.
    Trả về 'English' nếu là tiếng Anh, 'Vietnamese' nếu là tiếng Việt, hoặc 'Unknown'.
    Nếu không thể nhận diện (chuỗi rỗng hoặc ít ký tự), mặc định trả về 'Vietnamese'.
    """
    if not text or len(text.strip()) < 10:
        return 'Vietnamese'
        
    try:
        lang_code = detect(text)
        if lang_code == 'en':
            return 'English'
        elif lang_code == 'vi':
            return 'Vietnamese'
        else:
            # Nếu là ngôn ngữ khác, mặc định đưa vào Vietnamese
            return 'Vietnamese'
    except LangDetectException:
        return 'Vietnamese'
