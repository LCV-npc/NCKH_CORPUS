"""
custom_entities.py
──────────────────
Chứa hàm inject_custom_entities() để thêm các thực thể y khoa tùy chỉnh
vào term_dict thông qua hàm _add() được truyền vào từ ner_dict.

Cách dùng (trong ner_dict.py):
    from custom_entities import inject_custom_entities
    inject_custom_entities(_add)
"""

from __future__ import annotations
from typing import Callable


def inject_custom_entities(_add: Callable) -> None:
    """Thêm các thực thể hoặc từ đồng nghĩa tùy chỉnh vào từ điển."""

    # ── Bệnh thận mạn giai đoạn 5 ──────────────────────────────────────────
    _LABEL = "Bệnh thận mạn giai đoạn 5 (Suy thận giai đoạn 5)"
    _CODE  = "N18.5"
    _CAT   = "Bệnh Lý"
    for term in [
        "bệnh thận giai đoạn 5",
        "bệnh thận mạn giai đoạn 5",
        "bệnh thận mạn tính giai đoạn 5",
        "bệnh thận mạn giai đoạn cuối",
        "bệnh thận mạn tính giai đoạn cuối",
        "benh than giai doan 5",
        "benh than man giai doan 5",
        "benh than man tinh giai doan 5",
    ]:
        _add(term, _CAT, _CODE, _LABEL, "custom_alias")
    for term in [
        "suy thận giai đoạn 5",
        "suy thận mạn giai đoạn 5",
        "suy thận mạn tính giai đoạn 5",
        "suy thận mạn giai đoạn cuối",
        "suy thận mạn tính giai đoạn cuối",
        "suy than giai doan 5",
        "suy than man giai doan 5",
        "suy than man tinh giai doan 5",
    ]:
        _add(term, _CAT, _CODE, _LABEL, "custom_alias")

    # ── Bệnh tay chân miệng ────────────────────────────────────────────────
    _LABEL_TCM = "Bệnh tay chân miệng"
    _CODE_TCM  = "B08.4"
    _CAT_TCM   = "Bệnh Lý"
    for term in [
        # Tên chính thức
        "bệnh tay chân miệng", "bệnh tay chân miệng (TCM)",
        "tay chân miệng",
        # Không dấu
        "benh tay chan mieng",
        "tay chan mieng",
        # Viết tắt
        "TCM",
        "BTCM",
        # Từ đồng nghĩa lâm sàng (Herpangina với phát ban)
        "viêm họng có phỏng nước do virus đường ruột với phát ban",
        "viem hong co phong nuoc do virus duong ruot voi phat ban",
        "viêm họng phỏng nước do virus đường ruột có phát ban",
        "viem hong phong nuoc do virus duong ruot co phat ban",
        "herpangina với phát ban",
        "herpangina voi phat ban",
        "herpangina có phát ban",
        "herpangina co phat ban",
        # Tên tiếng Anh
        "hand foot and mouth disease",
        "hand-foot-and-mouth disease",
        "HFMD",
    ]:
        _add(term, _CAT_TCM, _CODE_TCM, _LABEL_TCM, "custom_alias")

    # ── Rối loạn lipid máu ────────────────────────────────────────────────
    _LABEL_Lipid = "Rối loạn chuyển hóa lipoprotein và tình trạng tăng lipid máu khác"
    _CODE_Lipid  = "E78"
    _CAT_Lipid   = "Bệnh Lý"
    for term in [
        # Tên chính thức
        "rối loạn chuyển hóa lipoprotein",
        "tình trạng tăng lipid máu",
        # Không dấu
        "roi loan chuyen hoa lipoprotein",
        "tinh trang tang lipid mau",
        # Đồng nghĩa lâm sàng
        "rối loạn lipid máu",
        "roi loan lipid mau",
        "rối loạn chuyển hóa lipid máu",
        "roi loan chuyen hoa lipid mau",
        "rối loạn mỡ máu",
        "roi loan mo mau",
        "tăng lipid máu",
        "tang lipid mau",
        "tăng mỡ máu",
        "tang mo mau",
        "dyslipidemia",
        "disorders of lipoprotein metabolism",
        "other hyperlipidemia",
    ]:
        _add(term, _CAT_Lipid, _CODE_Lipid, _LABEL_Lipid, "custom_alias")

    # ── Loãng xương ───────────────────────────────────────────────────────
    _LABEL_Loangxuong = "Loãng xương sau mãn kinh"
    _CODE_Loangxuong  = "M81.0"
    _CAT_Loangxuong   = "Bệnh Lý"
    for term in [
        "loãng xương nguyên phát",
        "loang xuong nguyen phat",
        "loãng xương sau mãn kinh",
        "loang xuong sau man kinh",
        "loãng xương",
        "loang xuong",
        "osteoporosis",
    ]:
        _add(term, _CAT_Loangxuong, _CODE_Loangxuong, _LABEL_Loangxuong, "custom_alias")

    # ── Gãy xương đốt sống ────────────────────────────────────────────────
    _LABEL_GXDS = "Gãy xương đốt sống do bệnh lý"
    _CODE_GXDS  = "M48.4"
    _CAT_GXDS   = "Bệnh Lý"
    for term in [
        # Tên chính thức
        "gãy xương đốt sống",
        "gãy đốt sống",
        # Không dấu
        "gay xuong dot song",
        "gay dot song",
        # Viết tắt thường gặp trong văn bản lâm sàng VN
        "GXĐS",
        "GĐS",
        # Biến thể mô tả
        "gãy xương cột sống",
        "gay xuong cot song",
        "lún đốt sống",
        "lun dot song",
        "xẹp đốt sống",
        "xep dot song",
        # Tiếng Anh
        "vertebral fracture",
        "spinal fracture",
        "vertebral compression fracture",
        "VCF",
    ]:
        _add(term, _CAT_GXDS, _CODE_GXDS, _LABEL_GXDS, "custom_alias")

    # ── Gút ───────────────────────────────────────────────────────────────
    _LABEL_GUT = "Gút, không xác định"
    _CODE_GUT  = "M10.9"
    _CAT_GUT   = "Bệnh Lý"
    for term in [
        # Tên chính thức tiếng Việt
        "gút",
        "bệnh gút",
        "thống phong",
        # Không dấu
        "gut",
        "benh gut",
        "thong phong",
        # Tên tiếng Anh
        "gout",
        "gouty arthritis",
        # Biến thể mô tả
        "gút cấp",
        "gút mạn tính",
        "gút mãn tính",
        "viêm khớp do gút",
        "viem khop do gut",
    ]:
        _add(term, _CAT_GUT, _CODE_GUT, _LABEL_GUT, "custom_alias")

    # ── Viêm khớp ─────────────────────────────────────────────────────────
    _LABEL_VK = "Viêm khớp, không xác định"
    _CODE_VK  = "M13.9"
    _CAT_VK   = "Bệnh Lý"
    for term in [
        # Tên chính thức
        "viêm khớp",
        "bệnh viêm khớp",
        # Không dấu
        "viem khop",
        "benh viem khop",
        # Tiếng Anh
        "arthritis",
        "joint inflammation",
        # Biến thể lâm sàng phổ biến
        "viêm đa khớp",
        "viem da khop",
        "viêm khớp không đặc hiệu",
    ]:
        _add(term, _CAT_VK, _CODE_VK, _LABEL_VK, "custom_alias")

    # ── Tăng acid uric máu ────────────────────────────────────────────────
    _LABEL_UAU = "Tăng acid uric máu"
    _CODE_UAU  = "E79.0"
    _CAT_UAU   = "Bệnh Lý"
    for term in [
        # Tên chính thức
        "tăng acid uric máu",
        "tăng axit uric máu",
        "tăng uric acid máu",
        # Không dấu
        "tang acid uric mau",
        "tang axit uric mau",
        "tang uric acid mau",
        # Tên ngắn / biến thể lâm sàng
        "tăng uric máu",
        "tăng uric huyết",
        "tang uric mau",
        "tang uric huyet",
        "tăng acid uric",
        "tang acid uric",
        # Tiếng Anh
        "hyperuricemia",
        "hyperuricaemia",
        "elevated uric acid",
        "high uric acid",
    ]:
        _add(term, _CAT_UAU, _CODE_UAU, _LABEL_UAU, "custom_alias")

    # ── Tăng huyết áp ─────────────────────────────────────────────────────
    _LABEL_THA = "Tăng huyết áp nguyên phát"
    _CODE_THA  = "I10"
    _CAT_THA   = "Bệnh Lý"
    for term in [
        # Tên chính thức
        "tăng huyết áp",
        "bệnh tăng huyết áp",
        "huyết áp cao",
        "cao huyết áp",
        # Không dấu
        "tang huyet ap",
        "benh tang huyet ap",
        "huyet ap cao",
        "cao huyet ap",
        # Viết tắt phổ biến
        "THA",
        "HA",
        # Biến thể lâm sàng
        "tăng huyết áp nguyên phát",
        "tăng huyết áp mạn tính",
        "tăng huyết áp mãn tính",
        "tăng huyết áp hệ thống",
        # Tiếng Anh
        "hypertension",
        "arterial hypertension",
        "high blood pressure",
    ]:
        _add(term, _CAT_THA, _CODE_THA, _LABEL_THA, "custom_alias")

    # ── Đột quỵ ───────────────────────────────────────────────────────────
    _LABEL_DQ = "Đột quỵ, không xác định do xuất huyết hay nhồi máu"
    _CODE_DQ  = "I64"
    _CAT_DQ   = "Bệnh Lý"
    for term in [
        # Tên có dấu
        "đột quỵ",
        "đột quỵ não",
        "đột quỵ cấp",
        "đột quỵ cấp tính",
        "đột quỵ thiếu máu não",
        "đột quỵ do thiếu máu cục bộ",
        "nhồi máu não",
        "xuất huyết não",
        "xuất huyết dưới nhện",
        "tai biến mạch máu não",
        "tai biến mạch não",
        # Không dấu
        "dot quy",
        "dot quy nao",
        "dot quy cap",
        "dot quy cap tinh",
        "nhoi mau nao",
        "xuat huyet nao",
        "xuat huyet duoi nhen",
        "tai bien mach mau nao",
        "tai bien mach nao",
        # Viết tắt
        "TBMMN",
        # Tiếng Anh
        "stroke",
        "brain stroke",
        "cerebral stroke",
        "ischemic stroke",
        "hemorrhagic stroke",
        "cerebral infarction",
        "cerebral hemorrhage",
        "subarachnoid hemorrhage",
    ]:
        _add(term, _CAT_DQ, _CODE_DQ, _LABEL_DQ, "custom_alias")

    # ── Thiếu máu não cục bộ thoáng qua (TIA) ────────────────────────────
    _LABEL_TIA = "Thiếu máu não cục bộ thoáng qua, không xác định"
    _CODE_TIA  = "G45.9"
    _CAT_TIA   = "Bệnh Lý"
    for term in [
        # Tên có dấu — chính thức
        "thiếu máu não thoáng qua",
        "thiếu máu não cục bộ thoáng qua",
        "cơn thiếu máu não thoáng qua",
        "cơn thiếu máu cục bộ thoáng qua",
        "thiếu máu não thoáng qua tái phát",
        "cơn thoáng thiếu máu não",
        # Không dấu
        "thieu mau nao thoang qua",
        "thieu mau nao cuc bo thoang qua",
        "con thieu mau nao thoang qua",
        "con thieu mau cuc bo thoang qua",
        "con thoang thieu mau nao",
        # Viết tắt
        "TIA",
        "TMNTQ",
        # Tiếng Anh
        "transient ischemic attack",
        "transient cerebral ischemia",
        "transient ischemic episode",
        "TIA attack",
        "mini-stroke",
        "mini stroke",
    ]:
        _add(term, _CAT_TIA, _CODE_TIA, _LABEL_TIA, "custom_alias")

    # ── Helicobacter pylori ───────────────────────────────────────────────
    _LABEL_Heli = "Helicobacter pylori [H.pylori] gây các bệnh đã được phân loại ở chương khác"
    _CODE_Heli  = "B98.0"
    _CAT_Heli   = "Bệnh Lý"
    for term in [
        "Helicobacter pylori",
        "H.pylori",
        "vi khuẩn Helicobacter pylori",
        "helicobacter pylori",
        "h.pylori",
        "vi khuẩn hp",
        "vi khuan hp",
    ]:
        _add(term, _CAT_Heli, _CODE_Heli, _LABEL_Heli, "custom_alias")

    # ── Phá thai nội khoa ─────────────────────────────────────────────────
    _LABEL_PhaThai = "Phá thai nội khoa (Sảy không hoàn toàn, gây biến chứng nhiễm khuẩn đường sinh dục và tiểu khung)"
    _CODE_PhaThai  = "O04"
    _CAT_PhaThai   = "Bệnh Lý"
    for term in [
        "phá thai nội khoa",
        "phá thai",
        "đình chỉ thai kì",
    ]:
        _add(term, _CAT_PhaThai, _CODE_PhaThai, _LABEL_PhaThai, "custom_alias")

    # ── Sảy thai tự nhiên ─────────────────────────────────────────────────
    _LABEL_SayThai = "Sẩy thai tự nhiên"
    _CODE_SayThai  = "O03"
    _CAT_SayThai   = "Bệnh Lý"
    for term in [
        "sảy thai tự nhiên",
        "say thai",
        "sẩy thai",
        "sảy thai",
    ]:
        _add(term, _CAT_SayThai, _CODE_SayThai, _LABEL_SayThai, "custom_alias")

    # ── Sốt xuất huyết Dengue ─────────────────────────────────────────────
    _LABEL_Dengue = "Sốt xuất huyết Dengue"
    _CODE_Dengue  = "A97"
    _CAT_Dengue   = "Bệnh Lý"
    for term in [
        "sốt xuất huyết dengue",
        "sốt xuất huyết Dengue (SXHD)",
        "sốt xuất huyết do virus dengue",
        "sốt xuất huyết do virus Dengue",
        "sot xuat huyet dengue",
        "sot xuat huyet do virus dengue",
        "SXHD",
    ]:
        _add(term, _CAT_Dengue, _CODE_Dengue, _LABEL_Dengue, "custom_alias")

    # ── Tắc ống mật ───────────────────────────────────────────────────────
    _LABEL_Mat = "Tắc ống mật"
    _CODE_Mat  = "K83.1"
    _CAT_Mat   = "Bệnh Lý"
    for term in [
        "tắc ống mật",
        "tac ong mat",
        "tắc mật",
        "tac mat",
    ]:
        _add(term, _CAT_Mat, _CODE_Mat, _LABEL_Mat, "custom_alias")

    # ── Viêm đường mật ───────────────────────────────────────────────────
    _LABEL_TrungMat = "Viêm đường mật"
    _CODE_TrungMat  = "K83.0"
    _CAT_TrungMat   = "Bệnh Lý"
    for term in [
        "viêm đường mật",
        "vien duong mat",
        "nhiễm trùng đường mật",
        "nhiem trung duong mat",
    ]:
        _add(term, _CAT_TrungMat, _CODE_TrungMat, _LABEL_TrungMat, "custom_alias")
    _LABEL_Thaiquangay = "Thai quá ngày sinh"
    _CODE_Thaiquangay  = "O48"
    _CAT_Thaiquangay   = "Bệnh Lý"
    for term in [
        "thai quá ngày sinh",
        "thai qua ngay sinh",
        "thai quá ngày dự sinh",
        "thai qua ngay du sinh",
        "thai quá ngày",
        "thai qua ngay",
    ]:
        _add(term, _CAT_Thaiquangay, _CODE_Thaiquangay, _LABEL_Thaiquangay, "custom_alias")
    _LABEL_Tuvongthainhi = "Thai chết vì nguyên nhân không được định rõ"
    _CODE_Tuvongthainhi  = "P95"
    _CAT_Tuvongthainhi   = "Bệnh Lý"
    for term in [
        "thai chết vì nguyên nhân không được định rõ",
        "thai chet vi nguyen nhan khong duoc dinh ro",
        "thai chết không rõ nguyên nhân",
        "thai chet khong ro nguyen nhan",
        "tử vong thai nhi",
        "tu vong thai nhi",
    ]:
        _add(term, _CAT_Tuvongthainhi, _CODE_Tuvongthainhi, _LABEL_Tuvongthainhi, "custom_alias")
    _LABEL_Benhtimmach = "Bệnh tim mạch do xơ vữa động mạch"
    _CODE_Benhtimmach  = "I25.1"
    _CAT_Benhtimmach   = "Bệnh Lý"
    for term in [
        "bệnh tim mạch do xơ vữa động mạch",
        "bệnh tim mạch do xơ vữa",
        "tim mạch do xơ vữa",
        "tim mach do xa vua",
        "bệnh tim mạch do xơ vữa ",
        "benh tim mach do xo vua",
        "tim mạch do xơ vữa động mạch",
        "tim mach do xơ vữa động mạch",
    ]:
        _add(term, _CAT_Benhtimmach, _CODE_Benhtimmach, _LABEL_Benhtimmach, "custom_alias")
    _LABEL_Tangcholesterol = "Tăng cholesterol máu, không xác định"     
    _CODE_Tangcholesterol  = "E78.0"
    _CAT_Tangcholesterol   = "Bệnh Lý"
    for term in [
        "tăng cholesterol máu",
        "tang cholesterol mau",
        "tăng cholesterol huyết",
        "tang cholesterol huyet",
    ]:
        _add(term, _CAT_Tangcholesterol, _CODE_Tangcholesterol, _LABEL_Tangcholesterol, "custom_alias")
    _LABEL_DCCT="Bong gân và căng cơ (phía trước) (phía sau) tổn thương dây chằng chéo khớp gối"
    _CODE_DCCT="S83.5"
    _CAT_DCCT="Bệnh Lý"
    for term in [
        "bong gân và căng cơ tổn thương dây chằng chéo khớp gối",
        "bong gan va cang co ton thuong day chang cheo khop goi",
        "bong gân tổn thương dây chằng chéo khớp gối",
        "bong gan ton thuong day chang cheo khop goi",
        "căng cơ tổn thương dây chằng chéo khớp gối",
        "cang co ton thuong day chang cheo khop goi",
        "đứt DCCT khớp gối",
        "dut DCCT khop goi",
    ]:
        _add(term, _CAT_DCCT, _CODE_DCCT, _LABEL_DCCT, "custom_alias")
    _LABEL_GayKin = "Gãy xương tại cẳng chân, gãy kín"
    _CODE_GayKin = "S827.0"
    _CAT_GayKin = "Bệnh Lý"
    for term in [
        "gãy xương tại cẳng chân, gãy kín",
        "gãy xương cẳng chân, gãy kín",
        "gay xuong tai canh chan, gay kin",
        "gay xuong canh chan, gay kin",
        "gãy kín hai xương cẳng chân",  
        "gay kin hai xuong canh chan",
    ]:
        _add(term, _CAT_GayKin, _CODE_GayKin, _LABEL_GayKin, "custom_alias")
    _LABEL_Gaychan = "Gãy xương tại cẳng chân"
    _CODE_Gaychan = "S827"
    _CAT_Gaychan = "Bệnh Lý"
    for term in [
        "gãy xương tại cẳng chân",
        "gãy xương cẳng chân",
        "gay xuong tai canh chan",
        "gay xuong cang chan",
        "gãy hai xương cẳng chân",
    ]:
        _add(term, _CAT_Gaychan, _CODE_Gaychan, _LABEL_Gaychan, "custom_alias") 
    _LABEL_Khantieng = "Chứng khó phát âm"
    _CODE_Khantieng = "R48.0"
    _CAT_Khantieng = "Triệu chứng"
    for term in [
        "chứng khó phát âm",
        "khó phát âm",
        "kho phat am",
        "khàn tiếng",
        "khan tieng",
        "dysarthria",
    ]:
        _add(term, _CAT_Khantieng, _CODE_Khantieng, _LABEL_Khantieng, "custom_alias")
    _LABEL_Tuyengiap ="u ác của tuyến giáp"
    _CODE_Tuyengiap = "C73"
    _CAT_Tuyengiap = "Bệnh Lý"
    for term in [
        "u ác của tuyến giáp",
        "u ac cua tuyen giap",
        "ung thư tuyến giáp",
        "ung thu tuyen giap",
        "ung thư tuyến giáp thể nhú",
        "ung thu tuyen giap the nhu",
        "ung thư biểu mô tuyến giáp thể nhú",
        "ung thu bieu mo tuyen giap the nhu",
    ]:
        _add(term, _CAT_Tuyengiap, _CODE_Tuyengiap, _LABEL_Tuyengiap, "custom_alias")
    _LABEL_HaCalci = "Rối loạn chuyển hóa calci"
    _CODE_HaCalci = "E83.5"
    _CAT_HaCalci = "Bệnh Lý"
    for term in [
        "rối loạn chuyển hóa calci",
        "roi loan chuyen hoa calci",
        "rối loạn chuyển hóa calcium",
        "roi loan chuyen hoa calcium",
        "hạ calci",
        "tăng calci",
        "ha calci",
        "tang calci",
    ]:
        _add(term, _CAT_HaCalci, _CODE_HaCalci, _LABEL_HaCalci, "custom_alias")
    _LABEL_GayHo = "Gãy xương tại cẳng chân, gãy hở"
    _CODE_GayHo = "S827.1"
    _CAT_GayHo = "Bệnh Lý"
    for term in [
        "gãy xương tại cẳng chân, gãy hở",
        "gãy xương cẳng chân, gãy hở",
        "gay xuong tai canh chan, gay ho",
        "gay xuong canh chan, gay ho",
        "gãy hở hai xương cẳng chân",
        "gay ho hai xuong canh chan",
    ]:
        _add(term, _CAT_GayHo, _CODE_GayHo, _LABEL_GayHo, "custom_alias")
        _LABEL_NhiemTrung = "Nhiễm trùng vết thương sau chấn thương, không xếp loại ở nơi khác"
        _CODE_NhiemTrung = "T793"
        _CAT_NhiemTrung = "Bệnh Lý"
    for term in [
        "nhiễm trùng vết thương sau chấn thương",
        "nhiem trung vet thuong sau chan thuong",
        "nhiễm trùng vết thương sau chấn thương, không xếp loại ở nơi khác",
        "nhiem trung vet thuong sau chan thuong khong xep loai o noi khac",
        "nhiem trung",
        "nhiễm trùng",
    ]:
        _add(term, _CAT_NhiemTrung, _CODE_NhiemTrung, _LABEL_NhiemTrung, "custom_alias")
        _LABEL_ChamLienXuong="Gãy xương chậm liền"
        _CODE_ChamLienXuong="M84.2"
        _CAT_ChamLienXuong="Bệnh Lý"
    for term in [
        "chậm liền xương",
        "cham lien xuong",
        "gãy xương chậm liền",
        "gay xuong cham lien",
    ]:
        _add(term, _CAT_ChamLienXuong, _CODE_ChamLienXuong, _LABEL_ChamLienXuong, "custom_alias")
        _LABEL_KhopGia = "Gãy xương không liền (khớp giả)"
        _CODE_KhopGia = "M84.1"
        _CAT_KhopGia = "Bệnh Lý"
    for term in [
        "khớp giả",
        "khop gia",
        "gãy xương không liền",
        "gay xuong khong lien",
    ]:
        _add(term, _CAT_KhopGia, _CODE_KhopGia, _LABEL_KhopGia, "custom_alias")
        _LABEL_Catcutchi = "Mất chi mắc phải"
        _CODE_Catcutchi = "Z89"
        _CAT_Catcutchi = "Bệnh Lý"
    for term in [
        "mất chi mắc phải",
        "mat chi mac phai",
        "cắt cụt chi",
        "cat cut chi",
    ]:
        _add(term, _CAT_Catcutchi, _CODE_Catcutchi, _LABEL_Catcutchi, "custom_alias")
        
