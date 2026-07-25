#!/usr/bin/env python3
"""Generate and backfill archive card thumbnails in local output and S3."""

# pylint: disable=broad-exception-caught,import-error

import argparse
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import boto3  # pyright: ignore[reportMissingImports]
from botocore.exceptions import ClientError  # pyright: ignore[reportMissingImports]
from PIL import Image, ImageOps  # pyright: ignore[reportMissingImports]

DEFAULT_OUTPUT_ROOT = Path(__file__).parent / "output"
DEFAULT_KEY_PREFIX = "archive"
THUMBNAIL_MAX_SIZE = (512, 512)
THUMBNAIL_QUALITY = 82
THUMBNAIL_CACHE_CONTROL = "public, max-age=31536000, immutable"
THUMBNAIL_METADATA_KEYS = (
    "thumbnail_filename",
    "thumbnail_width",
    "thumbnail_height",
    "thumbnail_bytes",
    "thumbnail_sha256",
    "thumbnail_source_sha256",
)
NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


@dataclass(frozen=True)
class ThumbnailResult:
    filename: str
    data: bytes
    width: int
    height: int
    byte_count: int
    sha256: str
    source_sha256: str

    def index_metadata(self) -> dict:
        return {
            "thumbnail_filename": self.filename,
            "thumbnail_width": self.width,
            "thumbnail_height": self.height,
            "thumbnail_bytes": self.byte_count,
            "thumbnail_sha256": self.sha256,
            "thumbnail_source_sha256": self.source_sha256,
        }


@dataclass
class SyncSummary:
    processed: int = 0
    succeeded: int = 0
    generated: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProductSyncResult:
    generated: bool
    uploaded: bool
    skipped_upload: bool


def create_thumbnail(source_data: bytes, product_id: str) -> ThumbnailResult:
    """Return a card-sized WebP thumbnail without mutating the source image."""
    source_sha256 = hashlib.sha256(source_data).hexdigest()
    with Image.open(io.BytesIO(source_data)) as source:
        normalized = ImageOps.exif_transpose(source)
        normalized.load()
        if _has_transparency(normalized):
            thumbnail = normalized.convert("RGBA")
        else:
            thumbnail = normalized.convert("RGB")

    thumbnail.thumbnail(THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    thumbnail.save(
        output,
        format="WEBP",
        quality=THUMBNAIL_QUALITY,
        method=6,
        exact=_has_transparency(thumbnail),
    )
    data = output.getvalue()
    return ThumbnailResult(
        filename=f"{product_id}.thumb.webp",
        data=data,
        width=thumbnail.width,
        height=thumbnail.height,
        byte_count=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        source_sha256=source_sha256,
    )


def sync_thumbnails(
    *,
    output_root: Path,
    s3_client,
    bucket: str,
    key_prefix: str = DEFAULT_KEY_PREFIX,
    product_ids: Iterable[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> SyncSummary:
    """Synchronize thumbnails for local product indexes, continuing on errors."""
    summary = SyncSummary()
    selected_ids = _selected_product_ids(output_root, product_ids)

    for product_id in selected_ids:
        summary.processed += 1
        try:
            result = _sync_product_thumbnail(
                output_root=output_root,
                s3_client=s3_client,
                bucket=bucket,
                key_prefix=key_prefix,
                product_id=product_id,
                force=force,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - backfill must continue per product.
            summary.failed += 1
            summary.errors[product_id] = f"{exc.__class__.__name__}: {exc}"
            continue

        summary.succeeded += 1
        summary.generated += int(result.generated)
        summary.uploaded += int(result.uploaded)
        summary.skipped += int(result.skipped_upload)

    return summary


def parse_product_ids(
    product_ids: Iterable[str] | None,
    ids_files: Iterable[Path] | None,
) -> list[str] | None:
    selected = []
    for product_id in product_ids or []:
        _add_product_id(selected, product_id)
    for ids_file in ids_files or []:
        for line in ids_file.read_text(encoding="utf-8").splitlines():
            clean_line = line.strip()
            if clean_line and not clean_line.startswith("#"):
                _add_product_id(selected, clean_line)
    return selected or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate archive thumbnails and synchronize them to Backblaze B2 S3.",
    )
    parser.add_argument("--endpoint-url", required=True, help="S3-compatible endpoint URL")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument(
        "--key-prefix",
        default=DEFAULT_KEY_PREFIX,
        help=f"Object key prefix (default: {DEFAULT_KEY_PREFIX})",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Local archive output root",
    )
    parser.add_argument(
        "--product-id",
        action="append",
        default=[],
        help="Product ID to process; repeatable",
    )
    parser.add_argument(
        "--ids-file",
        action="append",
        type=Path,
        default=[],
        help="Text file containing product IDs; repeatable",
    )
    parser.add_argument("--force", action="store_true", help="Regenerate and upload even when current")
    parser.add_argument("--dry-run", action="store_true", help="Read and report without any writes")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, s3_client=None) -> int:
    args = parse_args(argv)
    try:
        product_ids = parse_product_ids(args.product_id, args.ids_file)
        client = s3_client or boto3.client("s3", endpoint_url=args.endpoint_url)
        summary = sync_thumbnails(
            output_root=args.output_root,
            s3_client=client,
            bucket=args.bucket,
            key_prefix=args.key_prefix,
            product_ids=product_ids,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, ClientError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for product_id, error in summary.errors.items():
        print(f"{product_id}: {error}", file=sys.stderr)
    print(
        "summary: "
        f"processed={summary.processed} "
        f"succeeded={summary.succeeded} "
        f"generated={summary.generated} "
        f"uploaded={summary.uploaded} "
        f"skipped={summary.skipped} "
        f"failed={summary.failed}"
    )
    return 1 if summary.failed else 0


def _sync_product_thumbnail(
    *,
    output_root: Path,
    s3_client,
    bucket: str,
    key_prefix: str,
    product_id: str,
    force: bool,
    dry_run: bool,
) -> ProductSyncResult:
    product_dir = output_root / product_id
    index_path = product_dir / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"missing product index: {index_path}")

    with open(index_path, encoding="utf-8") as index_file:
        index = json.load(index_file)
    image_id, entry = _primary_image_entry(index, product_id)
    filename = str(entry.get("filename") or "").strip()
    if not filename:
        extension = str(index.get("extension") or "png").strip().lstrip(".")
        filename = f"{image_id}.{extension}"

    source_path = product_dir / filename
    source_key = _object_key(key_prefix, product_id, filename)
    if source_path.is_file():
        source_data = source_path.read_bytes()
    else:
        source_data = _download_object(s3_client, bucket, source_key)

    source_sha256 = hashlib.sha256(source_data).hexdigest()
    thumbnail_filename = f"{product_id}.thumb.webp"
    thumbnail_path = product_dir / thumbnail_filename
    thumbnail_key = _object_key(key_prefix, product_id, thumbnail_filename)
    remote_metadata = None if force else _head_metadata(s3_client, bucket, thumbnail_key)
    remote_current = (
        remote_metadata is not None
        and remote_metadata.get("source-sha256") == source_sha256
    )

    thumbnail = None if force else _current_local_thumbnail(entry, thumbnail_path, source_sha256)
    if thumbnail is not None and remote_current:
        return ProductSyncResult(
            generated=False,
            uploaded=False,
            skipped_upload=True,
        )

    generated = thumbnail is None
    if thumbnail is None:
        thumbnail = create_thumbnail(source_data, product_id)

    if not dry_run and (force or not remote_current):
        s3_client.put_object(
            Bucket=bucket,
            Key=thumbnail_key,
            Body=thumbnail.data,
            ContentType="image/webp",
            CacheControl=THUMBNAIL_CACHE_CONTROL,
            Metadata={"source-sha256": thumbnail.source_sha256},
        )

    if not dry_run:
        _atomic_write_bytes(thumbnail_path, thumbnail.data)
        entry.update(thumbnail.index_metadata())
        _atomic_write_json(index_path, index)

    uploaded = not dry_run and (force or not remote_current)
    return ProductSyncResult(
        generated=generated,
        uploaded=uploaded,
        skipped_upload=remote_current and not force,
    )


def _current_local_thumbnail(
    entry: dict,
    thumbnail_path: Path,
    source_sha256: str,
) -> ThumbnailResult | None:
    if entry.get("thumbnail_source_sha256") != source_sha256:
        return None
    if entry.get("thumbnail_filename") != thumbnail_path.name or not thumbnail_path.is_file():
        return None

    try:
        width = int(entry["thumbnail_width"])
        height = int(entry["thumbnail_height"])
        expected_bytes = int(entry["thumbnail_bytes"])
        expected_sha256 = str(entry["thumbnail_sha256"])
    except (KeyError, TypeError, ValueError):
        return None

    data = thumbnail_path.read_bytes()
    if len(data) != expected_bytes or hashlib.sha256(data).hexdigest() != expected_sha256:
        return None
    return ThumbnailResult(
        filename=thumbnail_path.name,
        data=data,
        width=width,
        height=height,
        byte_count=len(data),
        sha256=expected_sha256,
        source_sha256=source_sha256,
    )


def _primary_image_entry(index: dict, product_id: str) -> tuple[str, dict]:
    images = index.get("images") or {}
    if product_id in images:
        return product_id, images[product_id]
    if not images:
        raise ValueError("product index has no images")
    image_id = sorted(images, key=_image_sort_key)[0]
    return image_id, images[image_id]


def _selected_product_ids(
    output_root: Path,
    product_ids: Iterable[str] | None,
) -> list[str]:
    if product_ids is not None:
        selected = []
        for product_id in product_ids:
            _add_product_id(selected, product_id)
        return selected
    if not output_root.is_dir():
        return []
    return sorted(
        (
            path.name
            for path in output_root.iterdir()
            if path.is_dir() and path.name.isdigit() and (path / "index.json").is_file()
        ),
        key=int,
    )


def _add_product_id(selected: list[str], product_id: str) -> None:
    clean_product_id = str(product_id).strip()
    if not clean_product_id.isdigit():
        raise ValueError(f"Product ID must be numeric: {product_id}")
    if clean_product_id not in selected:
        selected.append(clean_product_id)


def _object_key(key_prefix: str, product_id: str, filename: str) -> str:
    clean_prefix = key_prefix.strip("/")
    parts = [part for part in (clean_prefix, product_id, filename) if part]
    return "/".join(parts)


def _download_object(s3_client, bucket: str, key: str) -> bytes:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    return body.read()


def _head_metadata(s3_client, bucket: str, key: str) -> dict[str, str] | None:
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error = exc.response.get("Error") or {}
        status_code = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if str(error.get("Code") or "") in NOT_FOUND_CODES or status_code == 404:
            return None
        raise
    return {
        str(key).lower(): str(value)
        for key, value in (response.get("Metadata") or {}).items()
    }


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with open(temporary_path, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with open(temporary_path, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _has_transparency(image: Image.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _image_sort_key(image_id: str) -> tuple[int, int, str]:
    if "o" not in image_id:
        return (0, 0, image_id)
    base, suffix = image_id.rsplit("o", 1)
    if suffix.isdigit():
        return (1, int(suffix), base)
    return (2, 0, image_id)


if __name__ == "__main__":
    raise SystemExit(main())
