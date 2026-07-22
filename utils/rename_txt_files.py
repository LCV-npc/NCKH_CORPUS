"""
rename_txt_files.py
===================
Đổi tên tất cả file .txt trong `Kho_Ngu_Lieu_Txt/` từ format cũ:
    DDMMYYYY_<TenBaiBaoRatDai...>_<NNNN>.txt
Về format mới gọn hơn:
    DDMMYYYY_<NNNN>.txt

Chạy:
    python rename_txt_files.py            # thực hiện đổi tên thật
    python rename_txt_files.py --dry-run  # chỉ xem, không đổi
"""

import os
import re
import sys
import logging
from pathlib import Path

# Fix encoding cho Windows console
if sys.stdout.encoding != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ── Cấu hình ─────────────────────────────────────────────────
FOLDER      = Path(__file__).parent / "Kho_Ngu_Lieu_Txt"
# Pattern: DDMMYYYY _ <bất kỳ ký tự> _ NNNN .txt
# Capture group 1 = date (8 chữ số), group 2 = STT (4 chữ số)
PATTERN     = re.compile(r'^(\d{8})_.+?_(\d{4})\.txt$')
DRY_RUN     = "--dry-run" in sys.argv


def _new_name(match: re.Match) -> str:
    date_str, stt = match.group(1), match.group(2)
    return f"{date_str}_{stt}.txt"


def rename_all() -> None:
    if not FOLDER.exists():
        log.error(f"Thư mục không tồn tại: {FOLDER}")
        return

    files   = sorted(FOLDER.glob("*.txt"))
    renamed = skipped = conflicts = 0
    conflict_log: list[str] = []

    for fp in files:
        m = PATTERN.match(fp.name)
        if not m:
            log.debug(f"Bỏ qua (không khớp pattern): {fp.name}")
            skipped += 1
            continue

        new_fp = fp.parent / _new_name(m)

        if new_fp.exists() and new_fp != fp:
            msg = f"CONFLICT — đích đã tồn tại: {fp.name} → {new_fp.name}"
            log.warning(msg)
            conflict_log.append(msg)
            conflicts += 1
            continue

        if DRY_RUN:
            log.info(f"[DRY-RUN] {fp.name}  →  {new_fp.name}")
        else:
            fp.rename(new_fp)
            log.info(f"OK  {fp.name}  →  {new_fp.name}")
        renamed += 1

    # ── Tóm tắt ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"{'[DRY-RUN] ' if DRY_RUN else ''}Kết quả:")
    print(f"  Đổi tên thành công : {renamed}")
    print(f"  Bỏ qua (pattern lạ): {skipped}")
    print(f"  Conflict (giữ nguyên): {conflicts}")
    if conflict_log:
        print("\nDanh sách conflict:")
        for c in conflict_log:
            print(f"  {c}")
    print("=" * 60)


if __name__ == "__main__":
    if DRY_RUN:
        print(">>> Chế độ DRY-RUN — không đổi tên thật <<<\n")
    rename_all()
