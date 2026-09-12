"""图片处理：化验单/检查报告照片在上传前压缩，控制外发体积与费用。

有 Pillow 时缩放到最长边 1600px 并存为 JPEG（通常 < 500KB）；没有 Pillow 时原样返回，
保证功能不因缺依赖而不可用。
"""
from __future__ import annotations

import io

MAX_EDGE = 1600
JPEG_QUALITY = 82
ALLOWED = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


def prepare_image(raw: bytes, mime: str) -> tuple[bytes, str, tuple[int, int] | None, str]:
    """返回 (数据, mime, (宽,高) 或 None, 说明)。"""
    if not raw:
        return raw, mime, None, "空文件"
    try:
        from PIL import Image
    except ImportError:
        return raw, mime, None, f"未安装 Pillow，原样上传（{len(raw) // 1024}KB）"
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        size = img.size
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > MAX_EDGE:
            scale = MAX_EDGE / float(max(w, h))
            _RS = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), _RS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        data = buf.getvalue()
        # 压缩后反而更大就用原图（例如本来就是小体积高质量 JPEG）
        if len(data) >= len(raw):
            return raw, mime, size, f"原图更小，保留原文件（{len(raw) // 1024}KB）"
        return data, "image/jpeg", img.size, (
            f"已压缩 {size[0]}×{size[1]} → {img.size[0]}×{img.size[1]}，"
            f"{len(raw) // 1024}KB → {len(data) // 1024}KB")
    except Exception as e:  # noqa: BLE001 - 图片损坏时不应让整个请求失败
        return raw, mime, None, f"无法解析图片（{e}），原样上传"
