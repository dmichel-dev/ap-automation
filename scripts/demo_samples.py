"""Records the OCR response of each public demo sample (services/core/app/demo_samples.py), so the
demo can process them without calling the OCR service. One Mistral OCR call per sample without a
recorded response; --force records them all again.

Run with scripts/demo_samples.sh. What is kept: the model, the usage, the document annotation and,
per page, its text and confidence scores (no word-level scores, no images).
"""

import argparse
import json
import sys
from typing import Any

from app import demo_samples
from app.config import get_settings
from app.providers import MistralOcrProvider


def storable(response: dict[str, Any]) -> dict[str, Any]:
    pages = [
        {
            "index": p.get("index"),
            "markdown": p.get("markdown") or "",
            "confidence_scores": {
                k: v for k, v in (p.get("confidence_scores") or {}).items() if k != "word_confidence_scores"
            },
        }
        for p in response.get("pages") or []
    ]
    keep = ("model", "usage_info", "document_annotation")
    return {**{k: response.get(k) for k in keep}, "pages": pages}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="record every sample again")
    args = parser.parse_args(argv)
    s = get_settings()
    provider = MistralOcrProvider(s.mistral_api_key.get_secret_value(), model=s.mistral_ocr_model)
    for sample in demo_samples.CATALOG:
        if sample.response.exists() and not args.force:
            print(f"kept      {sample.key}")
            continue
        response = storable(provider.fetch(sample.file.read_bytes(), "application/pdf"))
        sample.response.write_text(json.dumps(response, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        result = MistralOcrProvider.parse_response(response)
        print(f"recorded  {sample.key}: {result.pages} page(s), model {result.model}, avg {result.ocr_confidence_avg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
