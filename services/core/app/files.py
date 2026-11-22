"""Incoming file checks. The file type comes from the file's own bytes, never from the
declared MIME type or file name (both are attacker-controlled)."""

import io
from dataclasses import dataclass

from PIL import Image

MAX_FILE_BYTES = 20 * 1024 * 1024


class UnsupportedFile(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code   # unsupported_type | file_too_large | empty_file | unreadable_file


@dataclass
class PreparedFile:
    content: bytes          # what is sent to the provider
    mime_type: str          # application/pdf, image/png, image/jpeg, image/webp
    detected_type: str      # type of the original file (tiff is converted to pdf)


def sniff_mime(content: bytes) -> str | None:
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def prepare(content: bytes) -> PreparedFile:
    if not content:
        raise UnsupportedFile("empty_file", "file is empty")
    if len(content) > MAX_FILE_BYTES:
        raise UnsupportedFile("file_too_large", f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    mime = sniff_mime(content)
    if mime is None:
        raise UnsupportedFile("unsupported_type", "only PDF, PNG, JPEG, WebP and TIFF files are accepted")
    if mime == "image/tiff":
        # TIFF is not a documented Mistral input: convert all frames to one PDF
        return PreparedFile(_tiff_to_pdf(content), "application/pdf", mime)
    return PreparedFile(content, mime, mime)


def _tiff_to_pdf(content: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(content)) as img:
            frames = []
            for i in range(getattr(img, "n_frames", 1)):
                img.seek(i)
                frames.append(img.convert("RGB"))
    except (OSError, Image.DecompressionBombError) as exc:
        raise UnsupportedFile("unreadable_file", f"cannot read TIFF: {exc}") from exc
    out = io.BytesIO()
    frames[0].save(out, format="PDF", save_all=True, append_images=frames[1:], resolution=200)
    return out.getvalue()
