import re

# Bảng quy tắc chuẩn hóa dấu thanh tiếng Việt (đưa về chuẩn mới "oà", "uỷ", thay vì "òa", "ủy")
# Dựa trên quy tắc: dấu thanh đặt ở nguyên âm chính.
_VIETNAMESE_TONE_NORMALIZATION = {
    "òa": "oà", "óa": "oá", "ỏa": "oả", "õa": "oã", "ọa": "oạ",
    "òe": "oè", "óe": "oé", "ỏe": "oẻ", "õe": "oẽ", "ọe": "oẹ",
    "ùy": "uỳ", "úy": "uý", "ủy": "uỷ", "ũy": "uỹ", "ụy": "uỵ",
    "ùa": "uà", "úa": "uá", "ủa": "uả", "ũa": "uã", "ụa": "uạ",
    # Viết hoa
    "ÒA": "OÀ", "ÓA": "OÁ", "ỎA": "OẢ", "ÕA": "OÃ", "ỌA": "OẠ",
    "ÒE": "OÈ", "ÓE": "OÉ", "ỎE": "OẺ", "ÕE": "OẼ", "ỌE": "OẸ",
    "ÙY": "UỲ", "ÚY": "UÝ", "ỦY": "UỶ", "ŨY": "UỸ", "ỤY": "UỴ",
    "ÙA": "UÀ", "ÚA": "UÁ", "ỦA": "UẢ", "ŨA": "UÃ", "ỤA": "UẠ",
    # Hỗn hợp hoa thường
    "Òa": "Oà", "Óa": "Oá", "Ỏa": "Oả", "Õa": "Oã", "Ọa": "Oạ",
    "Òe": "Oè", "Óe": "Oé", "Ỏe": "Oẻ", "Õe": "Oẽ", "Ọe": "Oẹ",
    "Ùy": "Uỳ", "Úy": "Uý", "Ủy": "Uỷ", "Ũy": "Uỹ", "Ụy": "Uỵ",
    "Ùa": "Uà", "Úa": "Uá", "Ủa": "Uả", "Ũa": "Uã", "Ụa": "Uạ",
}

def normalize_vietnamese_tones(text: str) -> str:
    """
    Chuẩn hóa vị trí đặt dấu thanh trong tiếng Việt (ví dụ: hòa -> hoà).
    Điều này giúp tăng tỷ lệ khớp (match) với các từ điển sử dụng chuẩn mới.
    """
    if not text:
        return text
    
    for old, new in _VIETNAMESE_TONE_NORMALIZATION.items():
        text = text.replace(old, new)
        
    return text

if __name__ == "__main__":
    test_cases = ["Hòa bình", "Thủy điện", "khỏe mạnh", "hủy hoại"]
    for t in test_cases:
        print(f"{t} -> {normalize_vietnamese_tones(t)}")
