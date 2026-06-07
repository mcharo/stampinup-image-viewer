#!/usr/bin/env python3
"""
Archive all available Stampin' Up CDN product images for explicit product IDs.

Examples:
    python archive/archive_images.py --product-id 166635
    python archive/archive_images.py --ids-file product_ids.txt --describe
"""

import argparse
import base64
import hashlib
import io
import json
import os
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping

import anthropic  # pyright: ignore[reportMissingImports]
from PIL import Image, ImageOps  # pyright: ignore[reportMissingImports]
import requests  # pyright: ignore[reportMissingModuleSource]
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential  # pyright: ignore[reportMissingImports]

BASE_URL = "https://res.cloudinary.com/stampin-up/image/upload/prod/images/default-source/product-image"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_OUTPUT_ROOT = Path(__file__).parent / "output"
DEFAULT_ARCHIVE_CATALOG_DATA = Path(__file__).parent / "catalog-data.js"
DEFAULT_B2_BASE_URL = "https://stamps.charo.fun/archive/"
VALID_EXTENSIONS = {"png", "jpg"}
MAX_RANGE_PRODUCT_IDS = 10000
FETCH_DELAY = 1
FETCH_DELAY_JITTER_RATIO = 0.1
FETCH_DELAY_MAX_JITTER = 0.15
SCAN_INDEX_FILENAME = "scan-index.json"
MAX_DESCRIPTION_BASE64_BYTES = 10 * 1024 * 1024
MAX_DESCRIPTION_IMAGE_DIMENSION = 2000
DESCRIPTION_JPEG_QUALITY = 85
MIN_DESCRIPTION_IMAGE_DIMENSION = 256
MAX_TOKENS = 1500

DESCRIPTION_PROMPT = """\
Analyze this Stampin' Up product image for a searchable image archive.

Be generic: do not assume the product is a stamp set or a die.

Return only valid JSON with these fields:
- description: 1-3 concise sentences describing the visible product type when
  apparent, colors, patterns, materials or tools, packaging, quantities/layout,
  and any readable text.
- full_text: only text directly visible in the image. Use an empty string when
  there is no readable text.
- language: one of english, french, german, dutch, spanish, or unknown. Use
  english when there is no readable text.
- tags: 5-12 lowercase search-friendly keywords or short phrases for the image.
"""

VALID_LANGUAGES = {"english", "french", "german", "dutch", "spanish", "unknown"}


@dataclass
class ArchiveResult:
    product_id: str
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    described: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DescriptionImagePayload:
    media_type: str
    data: bytes
    downsampled: bool


@dataclass(frozen=True)
class DescriptionResult:
    description: str
    full_text: str = ""
    language: str = "english"
    tags: list[str] = field(default_factory=list)


class DescriptionResponseError(ValueError):
    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


class SkippedImageError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


FetchImage = Callable[[str, str], bytes | None]
DescribeImage = Callable[[Path, str, str], str | DescriptionResult]
DebugLogger = Callable[[str, str], None]
Now = Callable[[], str]
MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]
DelayJitter = Callable[[float], float]


def _default_fetch_delay_jitter(minimum_interval: float) -> float:
    if minimum_interval <= 0:
        return 0
    return random.uniform(0, min(FETCH_DELAY_MAX_JITTER, minimum_interval * FETCH_DELAY_JITTER_RATIO))


class FetchRateLimiter:
    def __init__(
        self,
        minimum_interval: float,
        clock: MonotonicClock = time.monotonic,
        sleeper: Sleeper | None = None,
        jitter: DelayJitter = _default_fetch_delay_jitter,
    ):
        self.minimum_interval = max(0, minimum_interval)
        self.clock = clock
        self.sleeper = sleeper or _sleep
        self.jitter = jitter
        self._last_request_started_at: float | None = None

    def before_request(self, product_id: str | None = None, debug_logger: DebugLogger | None = None) -> float:
        now = self.clock()
        if self._last_request_started_at is None:
            self._last_request_started_at = now
            return 0

        if self.minimum_interval <= 0:
            self._last_request_started_at = now
            return 0

        target_time = self._last_request_started_at + self.minimum_interval + max(0, self.jitter(self.minimum_interval))
        wait_time = max(0, target_time - now)
        if wait_time > 0:
            if product_id is not None and debug_logger is not None:
                _debug(debug_logger, product_id, f"waiting {wait_time:.2f}s before next CDN request")
            self.sleeper(wait_time)
            now = self.clock()

        self._last_request_started_at = now
        return wait_time


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_image_url(image_id: str, extension: str = "png") -> str:
    clean_extension = extension.lower().lstrip(".")
    if clean_extension not in VALID_EXTENSIONS:
        raise ValueError(f"Unsupported extension: {extension}")
    return f"{BASE_URL}/{image_id}.{clean_extension}"


def generate_image_ids(product_id: str, max_suffix: int) -> Iterable[str]:
    yield product_id
    for suffix in range(1, max_suffix + 1):
        yield f"{product_id}o{suffix:02d}"


def parse_product_ids(
    product_ids: list[str] | None,
    ids_file: Path | None,
    start_id: str | None,
    end_id: str | None,
) -> list[str]:
    collected: list[str] = []

    for product_id in product_ids or []:
        _add_product_id(collected, product_id)

    if ids_file is not None:
        for line in ids_file.read_text().splitlines():
            product_id = line.strip()
            if not product_id or product_id.startswith("#"):
                continue
            _add_product_id(collected, product_id)

    if start_id is not None or end_id is not None:
        if start_id is None or end_id is None:
            raise ValueError("--start-id and --end-id must be provided together")
        start = _parse_numeric_product_id(start_id)
        end = _parse_numeric_product_id(end_id)
        if _product_id_range_size(start, end) > MAX_RANGE_PRODUCT_IDS:
            raise ValueError(f"Product ID range is too large; maximum is {MAX_RANGE_PRODUCT_IDS}")
        for product_id in _inclusive_product_id_range(start, end):
            _add_product_id(collected, str(product_id))

    if not collected:
        raise ValueError("At least one product ID source is required")

    return collected


def validate_description_config(describe: bool, environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    if describe and not env.get("ANTHROPIC_API_KEY"):
        raise ValueError("ANTHROPIC_API_KEY is required when --describe is used")


def archive_products(
    product_ids: Iterable[str],
    output_root: Path,
    extension: str,
    max_missing_suffixes: int,
    delay: float,
    force: bool,
    describe: bool,
    force_descriptions: bool,
    model: str,
    debug: bool = False,
    skip_scan_index: bool = False,
) -> list[ArchiveResult]:
    session = requests.Session()
    session.headers.update({"User-Agent": "stampinup-image-archive/1.0"})
    fetcher = _make_fetcher(session)
    describer = make_describer() if describe else None
    debug_logger = _stderr_debug_logger if debug else None
    results = []

    for product_id in product_ids:
        result = archive_product(
            product_id=product_id,
            output_root=output_root,
            extension=extension,
            max_missing_suffixes=max_missing_suffixes,
            delay=delay,
            force=force,
            describe=describe,
            force_descriptions=force_descriptions,
            model=model,
            fetcher=fetcher,
            describer=describer,
            debug_logger=debug_logger,
        )
        results.append(result)
        if not skip_scan_index:
            update_scan_index(output_root, [result])

    return results


def update_scan_index(output_root: Path, results: Iterable[ArchiveResult], now: Now = now_iso) -> dict:
    output_root.mkdir(parents=True, exist_ok=True)
    scan_index_path = output_root / SCAN_INDEX_FILENAME
    scan_index = _load_scan_index(scan_index_path)
    seen = set(scan_index["scanned"]["seen"])
    missing = set(scan_index["scanned"]["missing"])

    for result in results:
        product_id = result.product_id
        if result.downloaded or result.skipped or result.described:
            seen.add(product_id)
            missing.discard(product_id)
        elif product_id in result.missing:
            missing.add(product_id)
            seen.discard(product_id)

    scan_index["updated_at"] = now()
    scan_index["scanned"]["seen"] = _sorted_product_ids(seen)
    scan_index["scanned"]["missing"] = _sorted_product_ids(missing)
    scan_index["ranges"] = _scan_ranges(scan_index["scanned"]["seen"], scan_index["scanned"]["missing"])
    _save_index(scan_index_path, scan_index)
    return scan_index


def backfill_scan_index(output_root: Path, start_id: str, end_id: str, now: Now = now_iso) -> dict:
    start = _parse_numeric_product_id(start_id)
    end = _parse_numeric_product_id(end_id)

    results = []
    for product_id_number in _inclusive_product_id_range(start, end):
        product_id = str(product_id_number)
        if _product_output_seen(output_root / product_id):
            results.append(ArchiveResult(product_id=product_id, skipped=[product_id]))
        else:
            results.append(ArchiveResult(product_id=product_id, missing=[product_id]))

    return update_scan_index(output_root, results, now=now)


def build_archive_catalog_data(
    output_root: Path,
    b2_base_url: str = DEFAULT_B2_BASE_URL,
    myss_metadata_path: Path | None = None,
    official_metadata_path: Path | None = None,
    now: Now = now_iso,
) -> dict:
    myss_metadata = load_myss_metadata(myss_metadata_path) if myss_metadata_path is not None else {}
    official_metadata = load_official_metadata(official_metadata_path) if official_metadata_path is not None else {}
    products = {}
    for product_dir in sorted(
        (path for path in output_root.iterdir() if path.is_dir() and path.name.isdigit()),
        key=lambda path: int(path.name),
    ) if output_root.is_dir() else []:
        index_path = product_dir / "index.json"
        if not index_path.exists():
            continue
        try:
            with open(index_path, encoding="utf-8") as f:
                product_index = json.load(f)
        except json.JSONDecodeError:
            continue

        product_id = str(product_index.get("product_id") or "").strip()
        product = _catalog_product_from_index(
            product_index,
            b2_base_url,
            myss_metadata.get(product_id),
            official_metadata.get(product_id),
        )
        if product is not None:
            products[product["product_id"]] = product

    return {
        "updated_at": now(),
        "b2_base_url": _ensure_trailing_slash(b2_base_url),
        "products": products,
    }


def load_jsonl_metadata(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        clean_line = line.strip()
        if not clean_line:
            continue
        try:
            record = json.loads(clean_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        records.append(record)
    return records


def load_myss_metadata(path: Path) -> dict[str, dict]:
    return _index_metadata_by_item_number(load_jsonl_metadata(path))


def load_official_metadata(path: Path) -> dict[str, dict]:
    return _index_metadata_by_item_number(load_jsonl_metadata(path))


def write_archive_catalog_data(catalog_data_path: Path, catalog: dict) -> None:
    catalog_data_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(catalog, separators=(",", ":"))
    catalog_data_path.write_text(f"window.ARCHIVE_CATALOG_DATA = {payload};\n", encoding="utf-8")


def describe_existing_products(
    output_root: Path,
    product_ids: list[str] | None,
    extension: str,
    force_descriptions: bool,
    model: str,
    describer: DescribeImage | None = None,
    debug: bool = False,
    skip_scan_index: bool = False,
    now: Now = now_iso,
) -> list[ArchiveResult]:
    clean_extension = extension.lower().lstrip(".")
    if clean_extension not in VALID_EXTENSIONS:
        raise ValueError(f"Unsupported extension: {extension}")

    selected_product_ids = product_ids or _existing_product_ids(output_root)
    describe_fn = describer or make_describer()
    debug_logger = _stderr_debug_logger if debug else None
    results = []

    for product_id in selected_product_ids:
        result = describe_existing_product(
            product_id=product_id,
            output_root=output_root,
            extension=clean_extension,
            force_descriptions=force_descriptions,
            model=model,
            describer=describe_fn,
            debug_logger=debug_logger,
            now=now,
        )
        results.append(result)
        if not skip_scan_index:
            update_scan_index(output_root, [result], now=now)

    return results


def describe_existing_product(
    product_id: str,
    output_root: Path,
    extension: str,
    force_descriptions: bool,
    model: str,
    describer: DescribeImage,
    debug_logger: DebugLogger | None = None,
    now: Now = now_iso,
) -> ArchiveResult:
    clean_extension = extension.lower().lstrip(".")
    product_dir = output_root / product_id
    result = ArchiveResult(product_id=product_id)
    _debug(debug_logger, product_id, "describe-existing start")
    if not product_dir.is_dir():
        result.missing.append(product_id)
        _debug(debug_logger, product_id, "describe-existing missing product directory")
        return result

    image_paths = sorted(product_dir.glob(f"*.{clean_extension}"))
    index_path = product_dir / "index.json"
    index = _load_index(index_path, product_id, clean_extension)

    for image_path in image_paths:
        image_id = image_path.stem
        image_bytes = image_path.read_bytes()
        _upsert_image_entry(
            index,
            image_id,
            clean_extension,
            image_bytes,
            now(),
            preserve_description=True,
            preserve_downloaded_at=True,
        )
        result.skipped.append(image_id)
        _debug(debug_logger, product_id, f"describing existing {image_path.name}")
        _maybe_describe(
            index,
            image_id,
            image_path,
            clean_extension,
            True,
            force_descriptions,
            model,
            describer,
            now,
            result,
        )

    index["updated_at"] = now()
    _save_index(index_path, index)
    _debug(
        debug_logger,
        product_id,
        f"describe-existing complete images={len(image_paths)} described={len(result.described)}",
    )
    return result


def archive_product(
    product_id: str,
    output_root: Path,
    extension: str = "png",
    max_missing_suffixes: int = 1,
    delay: float = FETCH_DELAY,
    force: bool = False,
    describe: bool = False,
    force_descriptions: bool = False,
    model: str = DEFAULT_MODEL,
    fetcher: FetchImage | None = None,
    describer: DescribeImage | None = None,
    debug_logger: DebugLogger | None = None,
    rate_limiter: FetchRateLimiter | None = None,
    now: Now = now_iso,
) -> ArchiveResult:
    if max_missing_suffixes < 1:
        raise ValueError("max_missing_suffixes must be at least 1")
    clean_extension = extension.lower().lstrip(".")
    if clean_extension not in VALID_EXTENSIONS:
        raise ValueError(f"Unsupported extension: {extension}")

    output_root.mkdir(parents=True, exist_ok=True)
    product_dir = output_root / product_id
    product_dir.mkdir(parents=True, exist_ok=True)
    index_path = product_dir / "index.json"
    index = _load_index(index_path, product_id, clean_extension)
    result = ArchiveResult(product_id=product_id)
    fetch = fetcher or _make_fetcher(_default_session())
    limiter = rate_limiter or FetchRateLimiter(delay)
    missing_suffixes = 0
    suffix = -1
    _debug(debug_logger, product_id, "start")

    try:
        while True:
            image_id = product_id if suffix == -1 else f"{product_id}o{suffix + 1:02d}"
            suffix += 1
            image_path = product_dir / f"{image_id}.{clean_extension}"

            if image_path.exists() and not force:
                _debug(debug_logger, product_id, f"skipping existing {image_path.name}")
                image_bytes = image_path.read_bytes()
                _upsert_image_entry(
                    index,
                    image_id,
                    clean_extension,
                    image_bytes,
                    now(),
                    preserve_description=True,
                    preserve_downloaded_at=True,
                )
                result.skipped.append(image_id)
                if describe:
                    _debug(debug_logger, product_id, f"describing {image_path.name}")
                _maybe_describe(index, image_id, image_path, clean_extension, describe, force_descriptions, model, describer, now, result)
                if suffix == 0:
                    missing_suffixes = 0
                continue

            _debug(debug_logger, product_id, f"fetching {image_path.name}")
            limiter.before_request(product_id, debug_logger)
            try:
                image_bytes = fetch(image_id, clean_extension)
            except SkippedImageError as exc:
                _debug(debug_logger, product_id, f"skipping {image_path.name}: HTTP {exc.status_code}")
                continue
            if image_bytes is None:
                result.missing.append(image_id)
                _debug(debug_logger, product_id, f"missing {image_path.name}")
                if suffix == 0:
                    break
                missing_suffixes += 1
                if missing_suffixes >= max_missing_suffixes:
                    break
                continue

            image_path.write_bytes(image_bytes)
            _debug(debug_logger, product_id, f"downloaded {image_path.name}")
            _upsert_image_entry(
                index,
                image_id,
                clean_extension,
                image_bytes,
                now(),
                preserve_description=True,
                preserve_downloaded_at=False,
            )
            result.downloaded.append(image_id)
            missing_suffixes = 0
            if describe:
                _debug(debug_logger, product_id, f"describing {image_path.name}")
            _maybe_describe(index, image_id, image_path, clean_extension, describe, force_descriptions, model, describer, now, result)
    finally:
        if result.downloaded or result.skipped or result.described:
            index["updated_at"] = now()
            _save_index(index_path, index)
        elif product_dir.exists():
            shutil.rmtree(product_dir)
        _debug(
            debug_logger,
            product_id,
            f"complete downloaded={len(result.downloaded)} skipped={len(result.skipped)} "
            f"missing={len(result.missing)} described={len(result.described)}",
        )

    return result


def make_describer() -> DescribeImage:
    client = anthropic.Anthropic()

    def describe(image_path: Path, extension: str, model: str) -> str:
        return describe_image(client, image_path, extension, model)

    return describe


def describe_image(client: anthropic.Anthropic, image_path: Path, extension: str, model: str) -> DescriptionResult:
    payload = prepare_description_image(image_path, extension)
    img_b64 = base64.standard_b64encode(payload.data).decode()
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": payload.media_type,
                        "data": img_b64,
                    },
                },
                {"type": "text", "text": DESCRIPTION_PROMPT},
            ],
        }],
    )
    return parse_description_response(response.content[0].text.strip())


def parse_description_response(raw: str) -> DescriptionResult:
    try:
        data = json.loads(_extract_json_object(raw))
    except json.JSONDecodeError as exc:
        raise DescriptionResponseError(
            f"Description response JSON could not be parsed: {exc.msg}",
            raw_response=raw,
        ) from exc
    description = str(data.get("description") or "").strip()
    full_text = str(data.get("full_text") or "").strip()
    language = _normalize_language(str(data.get("language") or "english"))
    tags = _normalize_tags(data.get("tags") or [])
    return DescriptionResult(
        description=description,
        full_text=full_text,
        language=language,
        tags=tags,
    )


def prepare_description_image(
    image_path: Path,
    extension: str,
    max_base64_bytes: int = MAX_DESCRIPTION_BASE64_BYTES,
) -> DescriptionImagePayload:
    original_data = image_path.read_bytes()
    original_media_type = "image/png" if extension == "png" else "image/jpeg"

    with Image.open(io.BytesIO(original_data)) as img:
        normalized = ImageOps.exif_transpose(img).convert("RGB")

    width, height = normalized.size
    if _base64_size(original_data) <= max_base64_bytes and max(width, height) <= MAX_DESCRIPTION_IMAGE_DIMENSION:
        return DescriptionImagePayload(
            media_type=original_media_type,
            data=original_data,
            downsampled=False,
        )

    if max(width, height) > MAX_DESCRIPTION_IMAGE_DIMENSION:
        scale = MAX_DESCRIPTION_IMAGE_DIMENSION / max(width, height)
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))

    quality = DESCRIPTION_JPEG_QUALITY

    while True:
        candidate = normalized
        if candidate.size != (width, height):
            candidate = normalized.resize((width, height), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if _base64_size(data) <= max_base64_bytes:
            return DescriptionImagePayload(
                media_type="image/jpeg",
                data=data,
                downsampled=True,
            )

        if width <= MIN_DESCRIPTION_IMAGE_DIMENSION and height <= MIN_DESCRIPTION_IMAGE_DIMENSION:
            return DescriptionImagePayload(
                media_type="image/jpeg",
                data=data,
                downsampled=True,
            )

        width = max(MIN_DESCRIPTION_IMAGE_DIMENSION, int(width * 0.75))
        height = max(MIN_DESCRIPTION_IMAGE_DIMENSION, int(height * 0.75))
        quality = max(60, quality - 5)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive all available Stampin' Up CDN product images for explicit product IDs."
    )
    parser.add_argument("--product-id", action="append", default=[], help="Product ID to archive; repeatable")
    parser.add_argument("--ids-file", type=Path, help="Text file containing one product ID per line")
    parser.add_argument("--start-id", help="Inclusive numeric range start")
    parser.add_argument("--end-id", help="Inclusive numeric range end")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Output root directory")
    parser.add_argument(
        "--backfill-scan-index",
        action="store_true",
        help="Build scan-index.json from existing output directories for --start-id/--end-id without CDN requests",
    )
    parser.add_argument(
        "--describe-existing",
        action="store_true",
        help="Run descriptions against existing local output only, without CDN requests",
    )
    parser.add_argument(
        "--build-catalog-data",
        action="store_true",
        help="Build archive catalog-data.js from existing output indexes without CDN requests",
    )
    parser.add_argument(
        "--catalog-data",
        type=Path,
        default=DEFAULT_ARCHIVE_CATALOG_DATA,
        help="Path for --build-catalog-data output",
    )
    parser.add_argument(
        "--b2-base-url",
        default=DEFAULT_B2_BASE_URL,
        help="Public B2 base URL for archive image links",
    )
    parser.add_argument(
        "--myss-metadata",
        type=Path,
        help="Optional MYSS JSONL metadata to merge into --build-catalog-data",
    )
    parser.add_argument(
        "--official-metadata",
        type=Path,
        help="Optional official Stampin' Up JSONL metadata to merge into --build-catalog-data",
    )
    parser.add_argument("--extension", choices=sorted(VALID_EXTENSIONS), default="png", help="CDN image extension")
    parser.add_argument(
        "--max-missing-suffixes",
        type=int,
        default=1,
        help="Stop suffix enumeration after this many consecutive missing suffix images",
    )
    parser.add_argument("--delay", type=float, default=FETCH_DELAY, help="Delay in seconds between CDN requests")
    parser.add_argument("--force", action="store_true", help="Re-download images even when local files exist")
    parser.add_argument("--describe", action="store_true", help="Generate generic image descriptions with Claude")
    parser.add_argument("--debug", action="store_true", help="Print per-product progress messages to stderr")
    parser.add_argument(
        "--skip-scan-index",
        action="store_true",
        help="Do not update scan-index.json during archive or describe-existing runs",
    )
    parser.add_argument(
        "--force-descriptions",
        action="store_true",
        help="Refresh existing descriptions when --describe is used",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model for --describe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        if args.backfill_scan_index:
            scan_index = backfill_scan_index(args.output, args.start_id, args.end_id)
            print(
                f"scan-index: seen={len(scan_index['scanned']['seen'])} "
                f"missing={len(scan_index['scanned']['missing'])}"
            )
            return 0

        if args.build_catalog_data:
            catalog = build_archive_catalog_data(
                args.output,
                args.b2_base_url,
                myss_metadata_path=args.myss_metadata,
                official_metadata_path=args.official_metadata,
            )
            write_archive_catalog_data(args.catalog_data, catalog)
            print(f"catalog-data: products={len(catalog['products'])} path={args.catalog_data}")
            return 0

        if args.describe_existing:
            product_ids = None
            if args.product_id or args.ids_file or args.start_id or args.end_id:
                product_ids = parse_product_ids(args.product_id, args.ids_file, args.start_id, args.end_id)
            validate_description_config(True)
            results = describe_existing_products(
                output_root=args.output,
                product_ids=product_ids,
                extension=args.extension,
                force_descriptions=args.force_descriptions,
                model=args.model,
                debug=args.debug,
                skip_scan_index=args.skip_scan_index,
            )
            for result in results:
                print(
                    f"{result.product_id}: "
                    f"existing={len(result.skipped)} "
                    f"missing={len(result.missing)} "
                    f"described={len(result.described)}"
                )
            return 0

        product_ids = parse_product_ids(args.product_id, args.ids_file, args.start_id, args.end_id)
        validate_description_config(args.describe)
        results = archive_products(
            product_ids=product_ids,
            output_root=args.output,
            extension=args.extension,
            max_missing_suffixes=args.max_missing_suffixes,
            delay=args.delay,
            force=args.force,
            describe=args.describe,
            force_descriptions=args.force_descriptions,
            model=args.model,
            debug=args.debug,
            skip_scan_index=args.skip_scan_index,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for result in results:
        print(
            f"{result.product_id}: "
            f"downloaded={len(result.downloaded)} "
            f"skipped={len(result.skipped)} "
            f"missing={len(result.missing)} "
            f"described={len(result.described)}"
        )
    return 0


def _add_product_id(collected: list[str], product_id: str) -> None:
    normalized = product_id.strip()
    if not normalized:
        return
    _parse_numeric_product_id(normalized)
    if normalized not in collected:
        collected.append(normalized)


def _parse_numeric_product_id(product_id: str) -> int:
    if not product_id.isdigit():
        raise ValueError(f"Product ID must be numeric: {product_id}")
    return int(product_id)


def _product_id_range_size(start: int, end: int) -> int:
    return abs(end - start) + 1


def _inclusive_product_id_range(start: int, end: int) -> range:
    step = 1 if end >= start else -1
    return range(start, end + step, step)


def _load_index(index_path: Path, product_id: str, extension: str) -> dict:
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
        index.setdefault("product_id", product_id)
        index.setdefault("language", "english")
        index.setdefault("tags", [])
        index.setdefault("images", {})
        index["extension"] = extension
        for entry in index["images"].values():
            entry.setdefault("full_text", "")
        return index
    return {
        "product_id": product_id,
        "updated_at": now_iso(),
        "extension": extension,
        "language": "english",
        "tags": [],
        "images": {},
    }


def _load_scan_index(scan_index_path: Path) -> dict:
    if scan_index_path.exists():
        with open(scan_index_path, encoding="utf-8") as f:
            scan_index = json.load(f)
        scan_index.setdefault("scanned", {})
        scan_index["scanned"].setdefault("seen", [])
        scan_index["scanned"].setdefault("missing", [])
        scan_index.setdefault("ranges", {"scanned": [], "seen": [], "missing": []})
        return scan_index
    return {
        "updated_at": now_iso(),
        "scanned": {
            "seen": [],
            "missing": [],
        },
        "ranges": {
            "scanned": [],
            "seen": [],
            "missing": [],
        },
    }


def _save_index(index_path: Path, index: dict) -> None:
    tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, index_path)


def _scan_ranges(seen_product_ids: list[str], missing_product_ids: list[str]) -> dict:
    scanned = _sorted_product_ids(set(seen_product_ids) | set(missing_product_ids))
    return {
        "scanned": _compress_product_id_ranges(scanned),
        "seen": _compress_product_id_ranges(seen_product_ids),
        "missing": _compress_product_id_ranges(missing_product_ids),
    }


def _compress_product_id_ranges(product_ids: Iterable[str]) -> list[dict[str, str]]:
    sorted_ids = _sorted_product_ids(product_ids)
    if not sorted_ids:
        return []

    ranges = []
    range_start = int(sorted_ids[0])
    previous = range_start
    for product_id in sorted_ids[1:]:
        current = int(product_id)
        if current == previous + 1:
            previous = current
            continue
        ranges.append({"start": str(range_start), "end": str(previous)})
        range_start = current
        previous = current
    ranges.append({"start": str(range_start), "end": str(previous)})
    return ranges


def _sorted_product_ids(product_ids: Iterable[str]) -> list[str]:
    return [str(product_id) for product_id in sorted({_parse_numeric_product_id(product_id) for product_id in product_ids})]


def _product_output_seen(product_dir: Path) -> bool:
    if not product_dir.is_dir():
        return False
    if (product_dir / "index.json").is_file():
        return True
    return any(path.is_file() for path in product_dir.iterdir())


def _existing_product_ids(output_root: Path) -> list[str]:
    if not output_root.is_dir():
        return []
    return _sorted_product_ids(path.name for path in output_root.iterdir() if path.is_dir() and path.name.isdigit())


def _catalog_product_from_index(
    product_index: dict,
    b2_base_url: str,
    myss_metadata: dict | None = None,
    official_metadata: dict | None = None,
) -> dict | None:
    product_id = str(product_index.get("product_id") or "").strip()
    if not product_id:
        return None
    catalog_exclusion = product_index.get("catalog_exclusion") or {}
    if catalog_exclusion.get("reason") == "duplicate":
        return None

    base_url = _ensure_trailing_slash(b2_base_url)
    language = _normalize_language(str(product_index.get("language") or "english"))
    tags = _normalize_tags(product_index.get("tags") or [])
    images = []
    for image_id, entry in sorted(
        (product_index.get("images") or {}).items(),
        key=lambda item: _image_sort_key(item[0]),
    ):
        filename = entry.get("filename") or f"{image_id}.{product_index.get('extension', 'png')}"
        description = str(entry.get("description") or "").strip()
        full_text = str(entry.get("full_text") or "").strip()
        image = {
            "image_id": image_id,
            "filename": filename,
            "b2_url": f"{base_url}{product_id}/{filename}",
            "description": description,
            "full_text": full_text,
            "bytes": entry.get("bytes"),
        }
        images.append(image)

    if not images:
        return None

    product = {
        "product_id": product_id,
        "language": language,
        "tags": tags,
        "images": images,
        "updated_at": product_index.get("updated_at"),
    }
    _merge_catalog_metadata(product, myss_metadata, official_metadata)
    return product


def _index_metadata_by_item_number(records: Iterable[dict]) -> dict[str, dict]:
    indexed = {}
    for record in records:
        item_number = str(record.get("item_number") or "").strip()
        if item_number:
            indexed[item_number] = record
    return indexed


def _merge_catalog_metadata(product: dict, myss_metadata: dict | None, official_metadata: dict | None) -> None:
    myss = _catalog_source_metadata(
        myss_metadata,
        ("site_item_id", "name", "item_number", "price", "status", "category", "detail_url", "image_url", "description"),
    )
    official = _catalog_source_metadata(
        official_metadata,
        ("name", "item_number", "status", "category", "category_confidence", "detail_url", "description"),
    )

    name = _clean_metadata_value(official.get("name")) or _clean_metadata_value(myss.get("name"))
    if name:
        product["name"] = name

    official_category = _clean_metadata_value(official.get("category"))
    myss_category = _clean_metadata_value(myss.get("category"))
    if official_category and official.get("category_confidence") == "inferred":
        product["category"] = official_category
    elif myss_category:
        product["category"] = myss_category

    status = _clean_metadata_value(official.get("status")) or _clean_metadata_value(myss.get("status"))
    if status:
        product["status"] = status
    elif not name:
        product["status"] = "unknown"

    price = _clean_metadata_value(myss.get("price"))
    if price:
        product["price"] = price

    descriptions = _catalog_descriptions(product, myss, official)
    if descriptions:
        product["descriptions"] = descriptions


def _catalog_source_metadata(record: dict | None, keys: tuple[str, ...]) -> dict:
    if not record:
        return {}
    metadata = {}
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            metadata[key] = value
    return metadata


def _catalog_descriptions(product: dict, myss: dict, official: dict) -> list[dict]:
    descriptions = []
    official_description = _clean_metadata_value(official.get("description"))
    if official_description:
        descriptions.append({"source": "official", "text": official_description})

    myss_description = _clean_metadata_value(myss.get("description"))
    if myss_description:
        descriptions.append({"source": "myss", "text": myss_description})
    return descriptions


def _catalog_search_text(product: dict) -> str:
    description_text = " ".join(description.get("text") or "" for description in product.get("descriptions") or [])
    return " ".join(
        part
        for part in [
            product.get("product_id"),
            product.get("language"),
            product.get("name"),
            product.get("category"),
            product.get("status"),
            " ".join(product.get("tags") or []),
            " ".join(image["filename"] for image in product.get("images") or []),
            " ".join(image["description"] for image in product.get("images") or []),
            " ".join(image["full_text"] for image in product.get("images") or []),
            description_text,
        ]
        if part
    ).lower()


def _clean_metadata_value(value) -> str:
    return str(value or "").strip()


def _image_sort_key(image_id: str) -> tuple[int, int, str]:
    if "o" not in image_id:
        return (0, 0, image_id)
    base, suffix = image_id.rsplit("o", 1)
    if suffix.isdigit():
        return (1, int(suffix), base)
    return (2, 0, image_id)


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _upsert_image_entry(
    index: dict,
    image_id: str,
    extension: str,
    image_bytes: bytes,
    downloaded_at: str,
    preserve_description: bool,
    preserve_downloaded_at: bool,
) -> None:
    existing = index["images"].get(image_id, {})
    entry = {
        "filename": f"{image_id}.{extension}",
        "url": build_image_url(image_id, extension),
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "bytes": len(image_bytes),
        "downloaded_at": existing.get("downloaded_at", downloaded_at) if preserve_downloaded_at else downloaded_at,
        "description": None,
        "full_text": "",
    }

    if preserve_description:
        for key in (
            "description",
            "description_model",
            "described_at",
            "description_error",
            "description_error_at",
            "full_text",
        ):
            if key in existing:
                entry[key] = existing[key]

    index["images"][image_id] = entry


def _maybe_describe(
    index: dict,
    image_id: str,
    image_path: Path,
    extension: str,
    describe: bool,
    force_descriptions: bool,
    model: str,
    describer: DescribeImage | None,
    now: Now,
    result: ArchiveResult,
) -> None:
    if not describe:
        return
    entry = index["images"][image_id]
    if entry.get("description") and not force_descriptions:
        return
    if describer is None:
        describer = make_describer()

    try:
        description = _normalize_description_result(describer(image_path, extension, model))
    except Exception as exc:  # noqa: BLE001 - archive runs should survive per-image model failures.
        entry["description"] = None
        description_error = {
            "type": exc.__class__.__name__,
            "message": str(exc),
            "model": model,
        }
        raw_response = getattr(exc, "raw_response", None)
        if raw_response is not None:
            description_error["raw_response"] = str(raw_response)
        entry["description_error"] = description_error
        entry["description_error_at"] = now()
        return

    _merge_description_result(index, image_id, description, model, now())
    result.described.append(image_id)


def _merge_description_result(
    index: dict,
    image_id: str,
    description: DescriptionResult,
    model: str,
    described_at: str,
) -> None:
    entry = index["images"][image_id]
    entry["description"] = description.description
    entry["full_text"] = description.full_text
    entry["description_model"] = model
    entry["described_at"] = described_at
    entry.pop("description_error", None)
    entry.pop("description_error_at", None)

    language = _normalize_language(description.language)
    if language not in ("english", "unknown"):
        index["language"] = language
    else:
        index.setdefault("language", "english")

    existing_tags = index.get("tags") or []
    index["tags"] = _normalize_tags([*existing_tags, *description.tags])


def _normalize_description_result(result: str | DescriptionResult) -> DescriptionResult:
    if isinstance(result, DescriptionResult):
        return DescriptionResult(
            description=result.description.strip(),
            full_text=result.full_text.strip(),
            language=_normalize_language(result.language),
            tags=_normalize_tags(result.tags),
        )
    return DescriptionResult(description=str(result).strip())


def _extract_json_object(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise DescriptionResponseError("Description response did not contain a JSON object", raw_response=raw)
    return stripped[start:end + 1]


def _normalize_language(language: str) -> str:
    normalized = language.strip().lower()
    return normalized if normalized in VALID_LANGUAGES else "english"


def _normalize_tags(tags) -> list[str]:
    if not isinstance(tags, list):
        return []
    normalized = []
    for tag in tags:
        clean_tag = " ".join(str(tag).lower().strip().split())
        if clean_tag and clean_tag not in normalized:
            normalized.append(clean_tag)
    return sorted(normalized)


def _default_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "stampinup-image-archive/1.0"})
    return session


def _make_fetcher(session: requests.Session) -> FetchImage:
    def fetch(image_id: str, extension: str) -> bytes | None:
        return fetch_image(build_image_url(image_id, extension), session)

    return fetch


def _stderr_debug_logger(product_id: str, message: str) -> None:
    print(f"{now_iso()} [{product_id}] {message}", file=sys.stderr)


def _debug(debug_logger: DebugLogger | None, product_id: str, message: str) -> None:
    if debug_logger is not None:
        debug_logger(product_id, message)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in (429, 503)
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
)
def fetch_image(url: str, session: requests.Session) -> bytes | None:
    response = session.get(url, timeout=30)
    if response.status_code == 400:
        raise SkippedImageError("CDN returned 400 Bad Request", status_code=400)
    if response.status_code == 404:
        return None
    if response.status_code in (429, 503):
        _sleep(_retry_after_seconds(response.headers.get("Retry-After")))
        response.raise_for_status()
    response.raise_for_status()
    return response.content


def _retry_after_seconds(header_value: str | None) -> float:
    if not header_value:
        return 0
    try:
        return max(0, float(header_value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(header_value)
        except (TypeError, ValueError):
            return 0
        return max(0, (retry_at - datetime.now(retry_at.tzinfo)).total_seconds())


def _base64_size(data: bytes) -> int:
    return len(base64.standard_b64encode(data))


def _sleep(delay: float) -> None:
    if delay > 0:
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
