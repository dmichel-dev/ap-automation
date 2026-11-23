"""Shows stored documents as PNG page images, rendered on the server.

The browser never opens the original PDF, so whatever a PDF contains (scripts, forms, links,
exploits aimed at a PDF viewer) never reaches it. Each rendering runs in a child process:
  - limited memory and CPU time, and a wall-clock timeout;
  - an environment without the database URL or any secret;
  - at most MAX_PAGES pages, MAX_SOURCE_PIXELS per source image (decompression bombs),
    MAX_PAGE_PIXELS per rendered page; PDF form fields are not drawn and no script runs.

The parent calls page_count() and render_page(); the child is `python -m app.web.render`.
"""

import logging
import math
import subprocess
import sys
import threading
from io import BytesIO
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)
MAX_PAGES = 20
TARGET_WIDTH = 1400  # pixels: readable on a laptop screen, small enough to send quickly
MAX_PAGE_PIXELS = 4_000_000
MAX_SOURCE_PIXELS = 40_000_000  # a 40-megapixel scan is already more than any invoice needs
CHILD_MEMORY = 768 * 1024 * 1024  # address space of the child (bytes)
CHILD_CPU_SECONDS = 10
TIMEOUT_SECONDS = 15
RENDERABLE = ("application/pdf", "image/png", "image/jpeg", "image/webp")  # what intake stores
APP_ROOT = Path(__file__).resolve().parents[2]  # the directory holding the "app" package

_slots = threading.BoundedSemaphore(2)  # renderings at once (memory)


class RenderError(Exception):
    """The document can't be shown (damaged, encrypted, too large, or took too long)."""


# =====================================================================================
# Parent side
# =====================================================================================
def page_count(path: Path, mime: str) -> int:
    """Pages that can be shown (at most MAX_PAGES)."""
    out = _run("count", path, mime, 0)
    try:
        return int(out.decode())
    except ValueError as e:
        raise RenderError("unexpected answer from the renderer") from e


def render_page(path: Path, mime: str, page: int) -> bytes:
    """PNG bytes of one page (0-based)."""
    if not 0 <= page < MAX_PAGES:
        raise RenderError("no such page")
    png = _run("page", path, mime, page)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RenderError("unexpected answer from the renderer")
    return png


def _run(command: str, path: Path, mime: str, page: int) -> bytes:
    if mime not in RENDERABLE:
        raise RenderError(f"this file type can't be shown: {mime}")
    args = [sys.executable, "-m", "app.web.render", command, str(path), mime, str(page)]
    with _slots:
        try:
            done = subprocess.run(
                args, cwd=APP_ROOT, env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, timeout=TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as e:
            raise RenderError("the document took too long to render") from e
    if done.returncode != 0:
        reason = done.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no reason given"]
        log.warning("rendering refused or failed (%s): %s", command, reason[0][:300])
        raise RenderError("the document can't be shown (damaged, protected or too large)")
    return done.stdout


# =====================================================================================
# Child side
# =====================================================================================
def _limit_resources() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY, CHILD_MEMORY))
    resource.setrlimit(resource.RLIMIT_CPU, (CHILD_CPU_SECONDS, CHILD_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


def _pdf_count(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    return min(len(pdf), MAX_PAGES)


def _pdf_page(path: Path, page: int) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    if page >= min(len(pdf), MAX_PAGES):
        raise SystemExit("no such page")
    p = pdf[page]
    width, height = p.get_size()  # points
    if width <= 0 or height <= 0:
        raise SystemExit("page without a size")
    scale = min(TARGET_WIDTH / width, math.sqrt(MAX_PAGE_PIXELS / (width * height)))
    image = p.render(scale=scale, may_draw_forms=False).to_pil()
    return _png(image)


def _image_page(path: Path, page: int) -> bytes:
    import warnings

    if page != 0:
        raise SystemExit("no such page")  # stored images have one page (TIFF scans are stored as PDF)
    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(path) as image:
        if image.width * image.height > MAX_SOURCE_PIXELS:
            raise SystemExit("image too large")
        image.draft("RGB", (TARGET_WIDTH, TARGET_WIDTH * 2))  # JPEG: decode at a reduced size
        image.thumbnail((TARGET_WIDTH, TARGET_WIDTH * 2))
        return _png(image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB"))


def _png(image: Image.Image) -> bytes:
    """Re-encoded as a plain PNG: no metadata or anything else from the original survives."""
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def child_main(argv: list[str]) -> None:
    _limit_resources()
    command, path, mime, page = argv[0], Path(argv[1]), argv[2], int(argv[3])
    if command == "count":
        sys.stdout.write(str(_pdf_count(path) if mime == "application/pdf" else 1))
        return
    png = _pdf_page(path, page) if mime == "application/pdf" else _image_page(path, page)
    sys.stdout.buffer.write(png)


if __name__ == "__main__":
    child_main(sys.argv[1:])
