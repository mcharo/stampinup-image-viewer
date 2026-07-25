import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

import archive_images


def valid_image_bytes(size: tuple[int, int] = (800, 400), color=(20, 80, 140)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeFetcher:
    def __init__(self, existing_image_ids: set[str]):
        self.existing_image_ids = existing_image_ids
        self.requested_urls: list[str] = []

    def __call__(self, image_id: str, extension: str) -> bytes | None:
        self.requested_urls.append(archive_images.build_image_url(image_id, extension))
        if image_id not in self.existing_image_ids:
            return None
        return f"image bytes for {image_id}".encode()


class FakeDescriber:
    def __init__(self):
        self.described_paths: list[Path] = []

    def __call__(self, image_path: Path, extension: str, model: str) -> str:
        self.described_paths.append(image_path)
        return f"description for {image_path.stem}"


class SequenceDescriber:
    def __init__(self, results):
        self.results = list(results)
        self.described_paths: list[Path] = []

    def __call__(self, image_path: Path, extension: str, model: str):
        self.described_paths.append(image_path)
        return self.results.pop(0)


class FakeClock:
    def __init__(self, current: float = 100.0):
        self.current = current

    def __call__(self) -> float:
        return self.current

    def sleep(self, delay: float) -> None:
        self.current += delay


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requested_urls: list[str] = []

    def get(self, url: str, timeout: int):
        self.requested_urls.append(url)
        return self.response


class ArchiveImagesTest(unittest.TestCase):
    def test_build_image_url_defaults_to_png(self):
        self.assertEqual(
            archive_images.build_image_url("166635"),
            "https://res.cloudinary.com/stampin-up/image/upload/prod/images/default-source/product-image/166635.png",
        )

    def test_build_image_url_allows_jpg_override(self):
        self.assertEqual(
            archive_images.build_image_url("166635o01", "jpg"),
            "https://res.cloudinary.com/stampin-up/image/upload/prod/images/default-source/product-image/166635o01.jpg",
        )

    def test_generate_image_ids_includes_base_then_zero_padded_suffixes(self):
        self.assertEqual(
            list(archive_images.generate_image_ids("166635", 3)),
            ["166635", "166635o01", "166635o02", "166635o03"],
        )

    def test_fetch_image_treats_bad_request_as_skippable_image(self):
        session = FakeSession(FakeResponse(400, b"bad request"))

        with self.assertRaises(archive_images.SkippedImageError) as cm:
            archive_images.fetch_image("https://example.test/image.png", session)

        self.assertEqual(cm.exception.status_code, 400)

    def test_fetch_rate_limiter_does_not_sleep_before_first_request(self):
        clock = FakeClock()
        sleeps: list[float] = []
        limiter = archive_images.FetchRateLimiter(
            minimum_interval=1.0,
            clock=clock,
            sleeper=lambda delay: sleeps.append(delay),
            jitter=lambda interval: 0,
        )

        limiter.before_request()

        self.assertEqual(sleeps, [])

    def test_fetch_rate_limiter_sleeps_only_remaining_interval(self):
        clock = FakeClock()
        sleeps: list[float] = []
        limiter = archive_images.FetchRateLimiter(
            minimum_interval=1.0,
            clock=clock,
            sleeper=lambda delay: (sleeps.append(delay), clock.sleep(delay)),
            jitter=lambda interval: 0,
        )

        limiter.before_request()
        clock.current += 0.25
        limiter.before_request()

        self.assertEqual(sleeps, [0.75])

    def test_fetch_rate_limiter_skips_sleep_when_enough_time_elapsed(self):
        clock = FakeClock()
        sleeps: list[float] = []
        limiter = archive_images.FetchRateLimiter(
            minimum_interval=1.0,
            clock=clock,
            sleeper=lambda delay: sleeps.append(delay),
            jitter=lambda interval: 0,
        )

        limiter.before_request()
        clock.current += 1.25
        limiter.before_request()

        self.assertEqual(sleeps, [])

    def test_archive_product_stops_after_first_missing_suffix_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = FakeFetcher({"166635", "166635o01", "166635o02"})

            result = archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=fetcher,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertEqual(result.downloaded, ["166635", "166635o01", "166635o02"])
            self.assertEqual(result.missing, ["166635o03"])
            self.assertEqual(
                [Path(url).name for url in fetcher.requested_urls],
                ["166635.png", "166635o01.png", "166635o02.png", "166635o03.png"],
            )

    def test_archive_product_continues_after_bad_request_suffix(self):
        def fetcher(image_id: str, extension: str) -> bytes | None:
            requested.append(image_id)
            if image_id == "166635o03":
                raise archive_images.SkippedImageError("CDN returned 400 Bad Request", status_code=400)
            if image_id in {"166635", "166635o01", "166635o02", "166635o04"}:
                return f"image bytes for {image_id}".encode()
            return None

        with tempfile.TemporaryDirectory() as tmp:
            requested: list[str] = []

            result = archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=fetcher,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertEqual(result.downloaded, ["166635", "166635o01", "166635o02", "166635o04"])
            self.assertEqual(result.missing, ["166635o05"])
            self.assertEqual(requested, ["166635", "166635o01", "166635o02", "166635o03", "166635o04", "166635o05"])
            self.assertFalse((Path(tmp) / "166635" / "166635o03.png").exists())
            self.assertTrue((Path(tmp) / "166635" / "166635o04.png").exists())

    def test_archive_product_uses_description_time_to_offset_next_cdn_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            sleeps: list[float] = []
            limiter = archive_images.FetchRateLimiter(
                minimum_interval=1.0,
                clock=clock,
                sleeper=lambda delay: (sleeps.append(delay), clock.sleep(delay)),
                jitter=lambda interval: 0,
            )

            def describe(image_path: Path, extension: str, model: str) -> str:
                clock.current += 1.25
                return f"description for {image_path.stem}"

            result = archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=1,
                describe=True,
                fetcher=FakeFetcher({"166635", "166635o01"}),
                describer=describe,
                rate_limiter=limiter,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertEqual(result.downloaded, ["166635", "166635o01"])
            self.assertEqual(sleeps, [])

    def test_archive_product_debug_logger_prefixes_current_product_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            debug_lines: list[str] = []

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=FakeFetcher({"166635"}),
                debug_logger=lambda product_id, message: debug_lines.append(f"[{product_id}] {message}"),
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertEqual(debug_lines[0], "[166635] start")
            self.assertIn("[166635] fetching 166635.png", debug_lines)
            self.assertIn("[166635] downloaded 166635.png", debug_lines)
            self.assertIn("[166635] missing 166635o01.png", debug_lines)
            self.assertEqual(
                debug_lines[-1],
                "[166635] complete downloaded=1 skipped=0 missing=1 described=0",
            )

    def test_archive_product_removes_empty_directory_when_base_image_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=FakeFetcher(set()),
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertEqual(result.downloaded, [])
            self.assertEqual(result.missing, ["166635"])
            self.assertFalse((Path(tmp) / "166635").exists())

    def test_archive_product_skips_existing_files_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            product_dir = Path(tmp) / "166635"
            product_dir.mkdir()
            (product_dir / "166635.png").write_bytes(b"existing bytes")
            fetcher = FakeFetcher({"166635", "166635o01"})

            result = archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=fetcher,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertEqual(result.skipped, ["166635"])
            self.assertNotIn(
                "https://res.cloudinary.com/stampin-up/image/upload/prod/images/default-source/product-image/166635.png",
                fetcher.requested_urls,
            )
            self.assertEqual((product_dir / "166635.png").read_bytes(), b"existing bytes")

            forced_result = archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                force=True,
                fetcher=fetcher,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertIn("166635", forced_result.downloaded)
            self.assertEqual((product_dir / "166635.png").read_bytes(), b"image bytes for 166635")

    def test_index_preserves_existing_description_when_downloading_new_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            product_dir = Path(tmp) / "166635"
            product_dir.mkdir()
            index_path = product_dir / "index.json"
            index_path.write_text(json.dumps({
                "product_id": "166635",
                "updated_at": "2026-06-02T15:00:00Z",
                "extension": "png",
                "images": {
                    "166635": {
                        "filename": "166635.png",
                        "url": archive_images.build_image_url("166635"),
                        "sha256": "old",
                        "bytes": 12,
                        "downloaded_at": "2026-06-02T15:00:00Z",
                        "description": "existing description",
                        "description_model": "claude-haiku-4-5-20251001",
                        "described_at": "2026-06-02T15:00:01Z",
                    }
                },
            }))

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                force=True,
                fetcher=FakeFetcher({"166635", "166635o01"}),
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads(index_path.read_text())
            self.assertEqual(index["images"]["166635"]["description"], "existing description")
            self.assertIn("166635o01", index["images"])
            self.assertIsNone(index["images"]["166635o01"]["description"])

    def test_parse_product_ids_combines_flags_file_and_range_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            ids_file = Path(tmp) / "ids.txt"
            ids_file.write_text("166636\n\n# comment\n166637\n")

            product_ids = archive_images.parse_product_ids(
                product_ids=["166635", "166636"],
                ids_file=ids_file,
                start_id="166637",
                end_id="166639",
            )

            self.assertEqual(product_ids, ["166635", "166636", "166637", "166638", "166639"])

    def test_parse_product_ids_accepts_descending_range(self):
        product_ids = archive_images.parse_product_ids(
            product_ids=[],
            ids_file=None,
            start_id="166639",
            end_id="166637",
        )

        self.assertEqual(product_ids, ["166639", "166638", "166637"])

    def test_parse_product_ids_requires_at_least_one_source(self):
        with self.assertRaisesRegex(ValueError, "At least one product ID source"):
            archive_images.parse_product_ids(product_ids=[], ids_file=None, start_id=None, end_id=None)

    def test_parse_product_ids_rejects_overly_large_ranges(self):
        with self.assertRaisesRegex(ValueError, "range is too large"):
            archive_images.parse_product_ids(
                product_ids=[],
                ids_file=None,
                start_id="100000",
                end_id=str(100000 + archive_images.MAX_RANGE_PRODUCT_IDS),
            )

    def test_descriptions_require_api_key_only_when_enabled(self):
        archive_images.validate_description_config(describe=False, environ={})

        with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
            archive_images.validate_description_config(describe=True, environ={})

    def test_archive_product_describes_images_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            describer = FakeDescriber()

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                describe=True,
                model="test-model",
                fetcher=FakeFetcher({"166635"}),
                describer=describer,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((Path(tmp) / "166635" / "index.json").read_text())
            self.assertEqual(index["images"]["166635"]["description"], "description for 166635")
            self.assertEqual(index["images"]["166635"]["description_model"], "test-model")
            self.assertEqual(index["images"]["166635"]["full_text"], "")
            self.assertEqual(index["language"], "english")
            self.assertEqual(index["tags"], [])
            self.assertEqual(len(describer.described_paths), 1)

    def test_archive_product_merges_structured_descriptions_into_product_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            describer = SequenceDescriber([
                archive_images.DescriptionResult(
                    description="Blue floral paper pack.",
                    full_text="",
                    language="english",
                    tags=["paper", "blue", "floral"],
                ),
                archive_images.DescriptionResult(
                    description="French packaging label.",
                    full_text="bonjour",
                    language="french",
                    tags=["paper", "french"],
                ),
            ])

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                describe=True,
                model="test-model",
                fetcher=FakeFetcher({"166635", "166635o01"}),
                describer=describer,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((Path(tmp) / "166635" / "index.json").read_text())
            self.assertEqual(index["language"], "french")
            self.assertEqual(index["tags"], ["blue", "floral", "french", "paper"])
            self.assertEqual(index["images"]["166635"]["full_text"], "")
            self.assertEqual(index["images"]["166635o01"]["full_text"], "bonjour")

    def test_parse_description_response_reads_json_contract(self):
        result = archive_images.parse_description_response("""{
          "description": "Assorted marker colors in packaging.",
          "full_text": "Stampin' Blends",
          "language": "english",
          "tags": ["markers", "coloring", "stampin blends"]
        }""")

        self.assertEqual(result.description, "Assorted marker colors in packaging.")
        self.assertEqual(result.full_text, "Stampin' Blends")
        self.assertEqual(result.language, "english")
        self.assertEqual(result.tags, ["coloring", "markers", "stampin blends"])

    def test_parse_description_response_error_preserves_raw_response(self):
        raw_response = "I can describe the image, but I cannot return JSON."

        with self.assertRaises(archive_images.DescriptionResponseError) as cm:
            archive_images.parse_description_response(raw_response)

        self.assertEqual(str(cm.exception), "Description response did not contain a JSON object")
        self.assertEqual(cm.exception.raw_response, raw_response)

    def test_describe_existing_products_updates_local_files_without_cdn_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            product_dir = output_root / "166635"
            product_dir.mkdir()
            (product_dir / "166635.png").write_bytes(b"existing image")
            (product_dir / "166635o01.png").write_bytes(b"existing image 2")
            describer = SequenceDescriber([
                archive_images.DescriptionResult(
                    description="Base image.",
                    full_text="",
                    language="english",
                    tags=["base"],
                ),
                archive_images.DescriptionResult(
                    description="German text sample.",
                    full_text="Danke",
                    language="german",
                    tags=["german", "text"],
                ),
            ])

            results = archive_images.describe_existing_products(
                output_root=output_root,
                product_ids=["166635"],
                extension="png",
                force_descriptions=False,
                model="test-model",
                describer=describer,
                debug=False,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((product_dir / "index.json").read_text())
            self.assertEqual(results[0].described, ["166635", "166635o01"])
            self.assertEqual(index["language"], "german")
            self.assertEqual(index["tags"], ["base", "german", "text"])
            self.assertEqual(index["images"]["166635"]["description"], "Base image.")
            self.assertEqual(index["images"]["166635o01"]["full_text"], "Danke")

    def test_describe_existing_products_can_skip_scan_index_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            product_dir = output_root / "166635"
            product_dir.mkdir()
            (product_dir / "166635.png").write_bytes(b"existing image")

            archive_images.describe_existing_products(
                output_root=output_root,
                product_ids=["166635"],
                extension="png",
                force_descriptions=False,
                model="test-model",
                describer=FakeDescriber(),
                debug=False,
                skip_scan_index=True,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertFalse((output_root / "scan-index.json").exists())

    def test_archive_product_saves_index_when_description_fails(self):
        def failing_describer(image_path: Path, extension: str, model: str) -> str:
            raise RuntimeError("model unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            result = archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                describe=True,
                model="test-model",
                fetcher=FakeFetcher({"166635"}),
                describer=failing_describer,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((Path(tmp) / "166635" / "index.json").read_text())
            self.assertIn("166635", index["images"])
            self.assertIsNone(index["images"]["166635"]["description"])
            self.assertEqual(index["images"]["166635"]["description_error"]["type"], "RuntimeError")
            self.assertEqual(index["images"]["166635"]["description_error"]["message"], "model unavailable")
            self.assertEqual(index["images"]["166635"]["description_error"]["model"], "test-model")
            self.assertEqual(index["images"]["166635"]["description_error_at"], "2026-06-03T15:00:00Z")
            self.assertEqual(result.described, [])

    def test_archive_product_records_raw_response_when_description_parse_fails(self):
        raw_response = "I can describe the image, but I cannot return JSON."

        def failing_describer(image_path: Path, extension: str, model: str) -> archive_images.DescriptionResult:
            raise archive_images.DescriptionResponseError(
                "Description response did not contain a JSON object",
                raw_response=raw_response,
            )

        with tempfile.TemporaryDirectory() as tmp:
            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                describe=True,
                model="test-model",
                fetcher=FakeFetcher({"166635"}),
                describer=failing_describer,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((Path(tmp) / "166635" / "index.json").read_text())
            self.assertEqual(
                index["images"]["166635"]["description_error"]["raw_response"],
                raw_response,
            )

    def test_prepare_description_image_downsamples_payload_without_changing_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "large.png"
            Image.new("RGB", (300, 300), color=(150, 20, 20)).save(image_path, format="PNG")
            original_bytes = image_path.read_bytes()

            payload = archive_images.prepare_description_image(
                image_path,
                "png",
                max_base64_bytes=1000,
            )

            self.assertEqual(image_path.read_bytes(), original_bytes)
            self.assertEqual(payload.media_type, "image/jpeg")
            self.assertLess(len(base64.standard_b64encode(payload.data)), 1000)
            self.assertLess(len(payload.data), len(original_bytes))

    def test_prepare_description_image_downsamples_oversized_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "wide.png"
            Image.new("RGB", (2101, 80), color=(255, 255, 255)).save(image_path, format="PNG")
            original_bytes = image_path.read_bytes()

            payload = archive_images.prepare_description_image(
                image_path,
                "png",
                max_base64_bytes=archive_images.MAX_DESCRIPTION_BASE64_BYTES,
            )

            self.assertEqual(image_path.read_bytes(), original_bytes)
            self.assertTrue(payload.downsampled)
            self.assertEqual(payload.media_type, "image/jpeg")
            with Image.open(io.BytesIO(payload.data)) as img:
                self.assertLessEqual(max(img.size), archive_images.MAX_DESCRIPTION_IMAGE_DIMENSION)

    def test_archive_product_preserves_description_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            product_dir = Path(tmp) / "166635"
            product_dir.mkdir()
            (product_dir / "166635.png").write_bytes(b"existing bytes")
            (product_dir / "index.json").write_text(json.dumps({
                "product_id": "166635",
                "updated_at": "2026-06-02T15:00:00Z",
                "extension": "png",
                "images": {
                    "166635": {
                        "filename": "166635.png",
                        "url": archive_images.build_image_url("166635"),
                        "sha256": "old",
                        "bytes": 12,
                        "downloaded_at": "2026-06-02T15:00:00Z",
                        "description": "existing description",
                        "description_model": "old-model",
                        "described_at": "2026-06-02T15:00:01Z",
                    }
                },
            }))
            describer = FakeDescriber()

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                describe=True,
                model="new-model",
                fetcher=FakeFetcher({"166635"}),
                describer=describer,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((product_dir / "index.json").read_text())
            self.assertEqual(index["images"]["166635"]["description"], "existing description")
            self.assertEqual(describer.described_paths, [])

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                describe=True,
                force_descriptions=True,
                model="new-model",
                fetcher=FakeFetcher({"166635"}),
                describer=describer,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((product_dir / "index.json").read_text())
            self.assertEqual(index["images"]["166635"]["description"], "description for 166635")
            self.assertEqual(index["images"]["166635"]["description_model"], "new-model")
            self.assertEqual(len(describer.described_paths), 1)

    def test_archive_product_waits_before_terminal_missing_suffix_after_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            sleeps: list[float] = []
            limiter = archive_images.FetchRateLimiter(
                minimum_interval=1.5,
                clock=clock,
                sleeper=lambda delay: (sleeps.append(delay), clock.sleep(delay)),
                jitter=lambda interval: 0,
            )

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=1.5,
                fetcher=FakeFetcher({"166635"}),
                rate_limiter=limiter,
                now=lambda: "2026-06-03T15:00:00Z",
            )

            self.assertEqual(sleeps, [1.5])

    def test_archive_product_updates_downloaded_at_when_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            product_dir = Path(tmp) / "166635"
            product_dir.mkdir()
            (product_dir / "166635.png").write_bytes(b"existing bytes")
            (product_dir / "index.json").write_text(json.dumps({
                "product_id": "166635",
                "updated_at": "2026-06-02T15:00:00Z",
                "extension": "png",
                "images": {
                    "166635": {
                        "filename": "166635.png",
                        "url": archive_images.build_image_url("166635"),
                        "sha256": "old",
                        "bytes": 12,
                        "downloaded_at": "2026-06-02T15:00:00Z",
                        "description": None,
                    }
                },
            }))

            archive_images.archive_product(
                product_id="166635",
                output_root=Path(tmp),
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                force=True,
                fetcher=FakeFetcher({"166635"}),
                now=lambda: "2026-06-03T15:00:00Z",
            )

            index = json.loads((product_dir / "index.json").read_text())
            self.assertEqual(index["images"]["166635"]["downloaded_at"], "2026-06-03T15:00:00Z")

    def test_archive_product_creates_thumbnail_for_new_primary_image_only(self):
        source = valid_image_bytes()

        def fetcher(image_id: str, extension: str) -> bytes | None:
            if image_id in {"166635", "166635o01"}:
                return source
            return None

        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)

            result = archive_images.archive_product(
                product_id="166635",
                output_root=output_root,
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=fetcher,
                now=lambda: "2026-07-20T20:00:00Z",
            )

            self.assertEqual(result.downloaded, ["166635", "166635o01"])
            thumbnail_path = output_root / "166635" / "166635.thumb.webp"
            self.assertTrue(thumbnail_path.is_file())
            index = json.loads((output_root / "166635" / "index.json").read_text())
            primary = index["images"]["166635"]
            self.assertEqual(primary["thumbnail_filename"], "166635.thumb.webp")
            self.assertEqual(primary["thumbnail_width"], 512)
            self.assertEqual(primary["thumbnail_height"], 256)
            self.assertEqual(
                primary["thumbnail_source_sha256"],
                hashlib.sha256(source).hexdigest(),
            )
            self.assertNotIn("thumbnail_filename", index["images"]["166635o01"])

    def test_archive_product_preserves_current_thumbnail_for_existing_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            source = valid_image_bytes()
            product_dir = output_root / "166635"
            product_dir.mkdir()
            (product_dir / "166635.png").write_bytes(source)

            archive_images.archive_product(
                product_id="166635",
                output_root=output_root,
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=FakeFetcher(set()),
                now=lambda: "2026-07-20T20:00:00Z",
            )
            original_thumbnail = (product_dir / "166635.thumb.webp").read_bytes()

            with patch.object(
                archive_images,
                "create_thumbnail",
                side_effect=AssertionError("current thumbnail should be reused"),
            ):
                archive_images.archive_product(
                    product_id="166635",
                    output_root=output_root,
                    extension="png",
                    max_missing_suffixes=1,
                    delay=0,
                    fetcher=FakeFetcher(set()),
                    now=lambda: "2026-07-20T20:01:00Z",
                )

            self.assertEqual(
                (product_dir / "166635.thumb.webp").read_bytes(),
                original_thumbnail,
            )
            index = json.loads((product_dir / "index.json").read_text())
            self.assertEqual(
                index["images"]["166635"]["thumbnail_source_sha256"],
                hashlib.sha256(source).hexdigest(),
            )

    def test_archive_product_regenerates_thumbnail_when_primary_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            first_source = valid_image_bytes((800, 400), color=(200, 20, 20))
            second_source = valid_image_bytes((300, 600), color=(20, 20, 200))
            product_dir = output_root / "166635"
            product_dir.mkdir()
            (product_dir / "166635.png").write_bytes(first_source)

            archive_images.archive_product(
                product_id="166635",
                output_root=output_root,
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                fetcher=FakeFetcher(set()),
                now=lambda: "2026-07-20T20:00:00Z",
            )
            original_thumbnail = (product_dir / "166635.thumb.webp").read_bytes()

            archive_images.archive_product(
                product_id="166635",
                output_root=output_root,
                extension="png",
                max_missing_suffixes=1,
                delay=0,
                force=True,
                fetcher=lambda image_id, extension: second_source if image_id == "166635" else None,
                now=lambda: "2026-07-20T20:01:00Z",
            )

            index = json.loads((product_dir / "index.json").read_text())
            primary = index["images"]["166635"]
            self.assertEqual(primary["thumbnail_width"], 256)
            self.assertEqual(primary["thumbnail_height"], 512)
            self.assertEqual(
                primary["thumbnail_source_sha256"],
                hashlib.sha256(second_source).hexdigest(),
            )
            self.assertNotEqual(
                (product_dir / "166635.thumb.webp").read_bytes(),
                original_thumbnail,
            )

    def test_archive_product_thumbnail_failure_does_not_abort_archival(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)

            with patch.object(
                archive_images,
                "create_thumbnail",
                side_effect=OSError("malformed image"),
            ):
                result = archive_images.archive_product(
                    product_id="166635",
                    output_root=output_root,
                    extension="png",
                    max_missing_suffixes=1,
                    delay=0,
                    fetcher=FakeFetcher({"166635"}),
                    now=lambda: "2026-07-20T20:00:00Z",
                )

            self.assertEqual(result.downloaded, ["166635"])
            self.assertTrue((output_root / "166635" / "166635.png").is_file())
            index = json.loads((output_root / "166635" / "index.json").read_text())
            self.assertNotIn("thumbnail_filename", index["images"]["166635"])

    def test_archive_products_can_skip_scan_index_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)

            with patch.object(
                archive_images,
                "archive_product",
                return_value=archive_images.ArchiveResult(product_id="166635", downloaded=["166635"]),
            ):
                archive_images.archive_products(
                    product_ids=["166635"],
                    output_root=output_root,
                    extension="png",
                    max_missing_suffixes=1,
                    delay=0,
                    force=False,
                    describe=False,
                    force_descriptions=False,
                    model=archive_images.DEFAULT_MODEL,
                    skip_scan_index=True,
                )

            self.assertFalse((output_root / "scan-index.json").exists())

    def test_update_scan_index_tracks_seen_missing_and_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_images.update_scan_index(
                output_root=Path(tmp),
                results=[
                    archive_images.ArchiveResult(product_id="156900", downloaded=["156900"]),
                    archive_images.ArchiveResult(product_id="156901", missing=["156901"]),
                    archive_images.ArchiveResult(product_id="156902", skipped=["156902"]),
                ],
                now=lambda: "2026-06-03T15:00:00Z",
            )

            scan_index = json.loads((Path(tmp) / "scan-index.json").read_text())
            self.assertEqual(scan_index["scanned"]["seen"], ["156900", "156902"])
            self.assertEqual(scan_index["scanned"]["missing"], ["156901"])
            self.assertEqual(scan_index["ranges"]["scanned"], [{"start": "156900", "end": "156902"}])
            self.assertEqual(
                scan_index["ranges"]["seen"],
                [{"start": "156900", "end": "156900"}, {"start": "156902", "end": "156902"}],
            )
            self.assertEqual(scan_index["ranges"]["missing"], [{"start": "156901", "end": "156901"}])

    def test_update_scan_index_moves_product_between_missing_and_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)

            archive_images.update_scan_index(
                output_root=output_root,
                results=[archive_images.ArchiveResult(product_id="156900", missing=["156900"])],
                now=lambda: "2026-06-03T15:00:00Z",
            )
            archive_images.update_scan_index(
                output_root=output_root,
                results=[archive_images.ArchiveResult(product_id="156900", downloaded=["156900"])],
                now=lambda: "2026-06-03T15:00:01Z",
            )

            scan_index = json.loads((output_root / "scan-index.json").read_text())
            self.assertEqual(scan_index["scanned"]["seen"], ["156900"])
            self.assertEqual(scan_index["scanned"]["missing"], [])

    def test_backfill_scan_index_marks_existing_output_dirs_seen_and_others_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            seen_with_index = output_root / "156900"
            seen_with_index.mkdir()
            (seen_with_index / "index.json").write_text("{}")
            seen_with_image = output_root / "156902"
            seen_with_image.mkdir()
            (seen_with_image / "156902.png").write_bytes(b"image")

            archive_images.backfill_scan_index(
                output_root=output_root,
                start_id="156900",
                end_id="156903",
                now=lambda: "2026-06-03T15:00:00Z",
            )

            scan_index = json.loads((output_root / "scan-index.json").read_text())
            self.assertEqual(scan_index["scanned"]["seen"], ["156900", "156902"])
            self.assertEqual(scan_index["scanned"]["missing"], ["156901", "156903"])
            self.assertEqual(scan_index["ranges"]["scanned"], [{"start": "156900", "end": "156903"}])

    def test_backfill_scan_index_accepts_descending_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            seen = output_root / "156902"
            seen.mkdir()
            (seen / "index.json").write_text("{}")

            archive_images.backfill_scan_index(
                output_root=output_root,
                start_id="156903",
                end_id="156900",
                now=lambda: "2026-06-03T15:00:00Z",
            )

            scan_index = json.loads((output_root / "scan-index.json").read_text())
            self.assertEqual(scan_index["scanned"]["seen"], ["156902"])
            self.assertEqual(scan_index["scanned"]["missing"], ["156900", "156901", "156903"])
            self.assertEqual(scan_index["ranges"]["scanned"], [{"start": "156900", "end": "156903"}])

    def test_main_backfills_scan_index_without_archiving_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(archive_images, "archive_products") as archive_products:
                with patch("sys.stdout", new_callable=io.StringIO):
                    exit_code = archive_images.main([
                        "--backfill-scan-index",
                        "--start-id",
                        "156900",
                        "--end-id",
                        "156903",
                        "--output",
                        tmp,
                    ])

            self.assertEqual(exit_code, 0)
            archive_products.assert_not_called()
            self.assertTrue((Path(tmp) / "scan-index.json").exists())

    def test_main_runs_without_description_api_key_when_describe_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                with patch.object(archive_images, "archive_products") as archive_products:
                    exit_code = archive_images.main([
                        "--product-id",
                        "166635",
                        "--output",
                        tmp,
                        "--delay",
                        "0",
                    ])

            self.assertEqual(exit_code, 0)
            archive_products.assert_called_once()

    def test_main_accepts_debug_flag_and_forwards_it_to_archive_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(archive_images, "archive_products") as archive_products:
                exit_code = archive_images.main([
                    "--product-id",
                    "166635",
                    "--output",
                    tmp,
                    "--debug",
                ])

            self.assertEqual(exit_code, 0)
            self.assertTrue(archive_products.call_args.kwargs["debug"])

    def test_main_accepts_skip_scan_index_flag_and_forwards_it_to_archive_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(archive_images, "archive_products") as archive_products:
                exit_code = archive_images.main([
                    "--product-id",
                    "166635",
                    "--output",
                    tmp,
                    "--skip-scan-index",
                ])

            self.assertEqual(exit_code, 0)
            self.assertTrue(archive_products.call_args.kwargs["skip_scan_index"])

    def test_stderr_debug_logger_prefixes_timestamp_and_product_id(self):
        stderr = io.StringIO()

        with patch.object(archive_images, "now_iso", return_value="2026-06-03T21:41:00Z"):
            with patch("sys.stderr", stderr):
                archive_images._stderr_debug_logger("166635", "fetching 166635.png")

        self.assertEqual(
            stderr.getvalue(),
            "2026-06-03T21:41:00Z [166635] fetching 166635.png\n",
        )

    def test_main_describe_existing_uses_local_describe_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=True):
                with patch.object(archive_images, "archive_products") as archive_products:
                    with patch.object(archive_images, "describe_existing_products") as describe_existing_products:
                        describe_existing_products.return_value = []
                        exit_code = archive_images.main([
                            "--describe-existing",
                            "--product-id",
                            "166635",
                            "--output",
                            tmp,
                        ])

            self.assertEqual(exit_code, 0)
            archive_products.assert_not_called()
            describe_existing_products.assert_called_once()

    def test_main_describe_existing_forwards_skip_scan_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}, clear=True):
                with patch.object(archive_images, "describe_existing_products") as describe_existing_products:
                    describe_existing_products.return_value = []
                    exit_code = archive_images.main([
                        "--describe-existing",
                        "--product-id",
                        "166635",
                        "--output",
                        tmp,
                        "--skip-scan-index",
                    ])

            self.assertEqual(exit_code, 0)
            self.assertTrue(describe_existing_products.call_args.kwargs["skip_scan_index"])

    def test_build_archive_catalog_data_uses_b2_urls_and_search_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            product_dir = output_root / "158669"
            product_dir.mkdir(parents=True)
            (product_dir / "158669.png").write_bytes(b"image")
            (product_dir / "158669o01.png").write_bytes(b"image")
            (product_dir / "index.json").write_text(json.dumps({
                "product_id": "158669",
                "updated_at": "2026-06-03T19:07:07Z",
                "extension": "png",
                "language": "german",
                "tags": ["tea therapy", "german text"],
                "images": {
                    "158669": {
                        "filename": "158669.png",
                        "thumbnail_filename": "158669.thumb.webp",
                        "thumbnail_sha256": "a" * 64,
                        "description": "German tea stamp set.",
                        "full_text": "KLEINE THERAPIE",
                    },
                    "158669o01": {
                        "filename": "158669o01.png",
                        "description": "Coordinating die shapes.",
                        "full_text": "",
                    },
                },
            }))

            catalog = archive_images.build_archive_catalog_data(
                output_root=output_root,
                b2_base_url="https://stamps.charo.fun/archive/",
                now=lambda: "2026-06-03T20:00:00Z",
            )

            product = catalog["products"]["158669"]
            self.assertEqual(catalog["updated_at"], "2026-06-03T20:00:00Z")
            self.assertEqual(product["language"], "german")
            self.assertEqual(product["tags"], ["german text", "tea therapy"])
            self.assertEqual(
                product["images"][0]["b2_url"],
                "https://stamps.charo.fun/archive/158669/158669.png",
            )
            self.assertEqual(
                product["images"][0]["thumbnail_url"],
                f"https://stamps.charo.fun/archive/158669/158669.thumb.webp?v={'a' * 64}",
            )
            self.assertNotIn("thumbnail_url", product["images"][1])
            self.assertNotIn("primary_image", product)
            self.assertNotIn("search_text", product)

    def test_catalog_thumbnail_hash_changes_detail_and_summary_urls(self):
        def product_for_hash(thumbnail_sha256: str) -> dict:
            return archive_images._catalog_product_from_index(
                {
                    "product_id": "166635",
                    "images": {
                        "166635": {
                            "filename": "166635.png",
                            "thumbnail_filename": "166635.thumb.webp",
                            "thumbnail_sha256": thumbnail_sha256,
                        },
                    },
                },
                "https://stamps.charo.fun/archive/",
            )

        first_hash = "a" * 64
        second_hash = "b" * 64
        first_product = product_for_hash(first_hash)
        second_product = product_for_hash(second_hash)

        first_detail_url = first_product["images"][0]["thumbnail_url"]
        second_detail_url = second_product["images"][0]["thumbnail_url"]
        self.assertEqual(
            first_detail_url,
            f"https://stamps.charo.fun/archive/166635/166635.thumb.webp?v={first_hash}",
        )
        self.assertEqual(
            archive_images._catalog_summary_product(first_product, "166")["primary_image"]["thumbnail_url"],
            first_detail_url,
        )
        self.assertNotEqual(second_detail_url, first_detail_url)
        self.assertEqual(
            archive_images._catalog_summary_product(second_product, "166")["primary_image"]["thumbnail_url"],
            second_detail_url,
        )

    def test_catalog_summary_omits_thumbnail_url_without_index_metadata(self):
        product = {
            "product_id": "166635",
            "language": "english",
            "tags": [],
            "images": [{
                "image_id": "166635",
                "filename": "166635.png",
                "b2_url": "https://example.test/archive/166635/166635.png",
                "description": "",
                "full_text": "",
            }],
        }

        summary = archive_images._catalog_summary_product(product, "166")

        self.assertEqual(
            summary["primary_image"],
            {"b2_url": "https://example.test/archive/166635/166635.png"},
        )

    def test_metadata_loaders_index_jsonl_by_item_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            myss_path = Path(tmp) / "myss.jsonl"
            official_path = Path(tmp) / "official.jsonl"
            myss_path.write_text(
                "\n".join([
                    json.dumps({
                        "item_number": "166881",
                        "name": "Woolly Friends Stamp Set",
                        "category": "Stamps",
                        "status": "Current",
                    }),
                    "",
                ]),
                encoding="utf-8",
            )
            official_path.write_text(
                json.dumps({
                    "item_number": "166881",
                    "name": "WOOLLY FRIENDS PHOTOPOLYMER STAMP SET (ENGLISH)",
                    "category": "Stamps",
                    "category_confidence": "inferred",
                }) + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                archive_images.load_myss_metadata(myss_path)["166881"]["name"],
                "Woolly Friends Stamp Set",
            )
            self.assertEqual(
                archive_images.load_official_metadata(official_path)["166881"]["name"],
                "WOOLLY FRIENDS PHOTOPOLYMER STAMP SET (ENGLISH)",
            )

    def test_metadata_loader_rejects_non_object_jsonl_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = Path(tmp) / "metadata.jsonl"
            metadata_path.write_text("[1, 2, 3]\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                archive_images.load_jsonl_metadata(metadata_path)

    def test_build_archive_catalog_data_merges_myss_and_official_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            product_dir = output_root / "166881"
            product_dir.mkdir(parents=True)
            (product_dir / "166881.png").write_bytes(b"image")
            (product_dir / "index.json").write_text(json.dumps({
                "product_id": "166881",
                "updated_at": "2026-06-04T20:00:00Z",
                "extension": "png",
                "language": "english",
                "tags": ["sheep", "cards"],
                "images": {
                    "166881": {
                        "filename": "166881.png",
                        "description": "AI image description.",
                        "full_text": "Hooray",
                    },
                },
            }), encoding="utf-8")
            myss_path = Path(tmp) / "myss.jsonl"
            official_path = Path(tmp) / "official.jsonl"
            myss_path.write_text(json.dumps({
                "site_item_id": "6500",
                "name": "Woolly Friends Stamp Set",
                "item_number": "166881",
                "price": "$19.00",
                "status": "Current",
                "category": "Stamps",
                "detail_url": "https://www.mystampinstuff.com/item.cfm?enc_item_id=test",
                "image_url": "https://www.mystampinstuff.com/images/items/6500.jpg",
                "description": "MYSS product description.",
            }) + "\n", encoding="utf-8")
            official_path.write_text(json.dumps({
                "site_item_id": None,
                "name": "WOOLLY FRIENDS PHOTOPOLYMER STAMP SET (ENGLISH)",
                "item_number": "166881",
                "price": None,
                "status": "Current",
                "category": "Stamps",
                "category_confidence": "inferred",
                "detail_url": "https://www.stampinup.com/products/166881",
                "image_url": None,
                "description": "Official product detail description.",
            }) + "\n", encoding="utf-8")

            catalog = archive_images.build_archive_catalog_data(
                output_root=output_root,
                b2_base_url="https://stamps.charo.fun/archive/",
                myss_metadata_path=myss_path,
                official_metadata_path=official_path,
                now=lambda: "2026-06-04T21:00:00Z",
            )

            product = catalog["products"]["166881"]
            self.assertEqual(product["name"], "WOOLLY FRIENDS PHOTOPOLYMER STAMP SET (ENGLISH)")
            self.assertEqual(product["category"], "Stamps")
            self.assertEqual(product["status"], "Current")
            self.assertEqual(product["price"], "$19.00")
            self.assertNotIn("myss", product)
            self.assertNotIn("official", product)
            self.assertEqual(product["descriptions"][0]["source"], "official")
            self.assertEqual(product["descriptions"][1]["source"], "myss")
            self.assertEqual(len(product["descriptions"]), 2)
            self.assertNotIn("primary_image", product)
            self.assertNotIn("search_text", product)

    def test_build_archive_catalog_data_uses_myss_category_when_official_category_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            product_dir = output_root / "166882"
            product_dir.mkdir(parents=True)
            (product_dir / "166882.png").write_bytes(b"image")
            (product_dir / "index.json").write_text(json.dumps({
                "product_id": "166882",
                "updated_at": "2026-06-04T20:00:00Z",
                "extension": "png",
                "images": {
                    "166882": {"filename": "166882.png", "description": "", "full_text": ""},
                },
            }), encoding="utf-8")
            myss_path = Path(tmp) / "myss.jsonl"
            official_path = Path(tmp) / "official.jsonl"
            myss_path.write_text(json.dumps({
                "item_number": "166882",
                "name": "MYSS Name",
                "category": "Paper",
                "status": "Retired",
            }) + "\n", encoding="utf-8")
            official_path.write_text(json.dumps({
                "item_number": "166882",
                "name": "Official Name",
                "category": None,
                "category_confidence": "unknown",
                "status": "Current",
            }) + "\n", encoding="utf-8")

            catalog = archive_images.build_archive_catalog_data(
                output_root=output_root,
                myss_metadata_path=myss_path,
                official_metadata_path=official_path,
                now=lambda: "now",
            )

            product = catalog["products"]["166882"]
            self.assertEqual(product["name"], "Official Name")
            self.assertEqual(product["category"], "Paper")
            self.assertEqual(product["status"], "Current")

    def test_build_archive_catalog_data_marks_unmatched_products_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            product_dir = output_root / "166883"
            product_dir.mkdir(parents=True)
            (product_dir / "166883.png").write_bytes(b"image")
            (product_dir / "index.json").write_text(json.dumps({
                "product_id": "166883",
                "updated_at": "2026-06-04T20:00:00Z",
                "extension": "png",
                "images": {
                    "166883": {"filename": "166883.png", "description": "", "full_text": ""},
                },
            }), encoding="utf-8")
            myss_path = Path(tmp) / "myss.jsonl"
            official_path = Path(tmp) / "official.jsonl"
            myss_path.write_text("", encoding="utf-8")
            official_path.write_text("", encoding="utf-8")

            catalog = archive_images.build_archive_catalog_data(
                output_root=output_root,
                myss_metadata_path=myss_path,
                official_metadata_path=official_path,
                now=lambda: "now",
            )

            product = catalog["products"]["166883"]
            self.assertNotIn("name", product)
            self.assertEqual(product["status"], "unknown")

    def test_build_archive_catalog_data_merges_additional_metadata_first_match_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            for product_id in ("167583", "167585"):
                product_dir = output_root / product_id
                product_dir.mkdir(parents=True)
                (product_dir / f"{product_id}.png").write_bytes(b"image")
                (product_dir / "index.json").write_text(json.dumps({
                    "product_id": product_id,
                    "updated_at": "2026-06-04T20:00:00Z",
                    "extension": "png",
                    "images": {
                        product_id: {"filename": f"{product_id}.png", "description": "", "full_text": ""},
                    },
                }), encoding="utf-8")
            first_metadata_path = Path(tmp) / "first.jsonl"
            second_metadata_path = Path(tmp) / "second.jsonl"
            first_metadata_path.write_text(
                json.dumps({"item_number": "167583", "name": "First Name"}) + "\n",
                encoding="utf-8",
            )
            second_metadata_path.write_text(
                "\n".join([
                    json.dumps({"item_number": "167583", "name": "Second Name"}),
                    json.dumps({"item_number": "167585", "name": "Only In Second"}),
                ]) + "\n",
                encoding="utf-8",
            )

            catalog = archive_images.build_archive_catalog_data(
                output_root=output_root,
                additional_metadata_paths=[first_metadata_path, second_metadata_path],
                now=lambda: "now",
            )

            self.assertEqual(catalog["products"]["167583"]["name"], "First Name")
            self.assertEqual(catalog["products"]["167585"]["name"], "Only In Second")
            self.assertNotIn("status", catalog["products"]["167583"])

    def test_build_archive_catalog_data_excludes_duplicate_marked_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            product_dir = output_root / "166884"
            product_dir.mkdir(parents=True)
            (product_dir / "166884.png").write_bytes(b"image")
            (product_dir / "index.json").write_text(json.dumps({
                "product_id": "166884",
                "updated_at": "2026-06-04T20:00:00Z",
                "extension": "png",
                "catalog_exclusion": {
                    "reason": "duplicate",
                    "duplicate_of": "166000",
                    "matched_sha256": ["knownhash"],
                },
                "images": {
                    "166884": {"filename": "166884.png", "description": "", "full_text": ""},
                },
            }), encoding="utf-8")

            catalog = archive_images.build_archive_catalog_data(
                output_root=output_root,
                now=lambda: "now",
            )

            self.assertNotIn("166884", catalog["products"])

    def test_write_archive_catalog_assets_writes_versioned_relative_manifest_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            details_dir = output_dir / "catalog-details"
            details_dir.mkdir()
            stale_path = details_dir / "999.json"
            stale_path.write_text('{"stale":true}')
            events: list[Path] = []
            original_atomic_write = archive_images._atomic_write_json
            original_unlink = Path.unlink

            def recording_atomic_write(path: Path, data) -> None:
                events.append(path)
                original_atomic_write(path, data)

            def recording_unlink(path: Path, *args, **kwargs) -> None:
                events.append(path)
                original_unlink(path, *args, **kwargs)

            with patch.object(archive_images, "_atomic_write_json", side_effect=recording_atomic_write):
                with patch.object(Path, "unlink", autospec=True, side_effect=recording_unlink):
                    archive_images.write_archive_catalog_assets(
                        output_dir,
                        {"updated_at": "2026-07-20T20:00:00Z", "products": {}},
                    )

            manifest = json.loads((output_dir / "catalog-manifest.json").read_text())
            self.assertEqual(manifest, {
                "schema_version": 1,
                "version": "2026-07-20T20:00:00Z",
                "updated_at": "2026-07-20T20:00:00Z",
                "summary_url": "catalog-summary.json",
                "search_url": "catalog-search.json",
                "details_url": "catalog-details/",
                "viewer_index_url": "viewer-index.json",
            })
            self.assertEqual(events[-1], output_dir / "catalog-manifest.json")

    def test_write_archive_catalog_assets_cleanup_failure_preserves_previous_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            details_dir = output_dir / "catalog-details"
            details_dir.mkdir()
            stale_path = details_dir / "999.json"
            stale_path.write_text('{"stale":true}')
            manifest_path = output_dir / "catalog-manifest.json"
            previous_manifest = b'{"version":"previous"}'
            manifest_path.write_bytes(previous_manifest)
            original_unlink = Path.unlink

            def fail_stale_cleanup(path: Path, *args, **kwargs) -> None:
                if path == stale_path:
                    raise OSError("cleanup failed")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", autospec=True, side_effect=fail_stale_cleanup):
                with self.assertRaisesRegex(OSError, "cleanup failed"):
                    archive_images.write_archive_catalog_assets(
                        output_dir,
                        {"updated_at": "new", "products": {}},
                    )

            self.assertEqual(manifest_path.read_bytes(), previous_manifest)

    def test_write_archive_catalog_assets_summary_has_exact_thin_fields_and_display_name_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            catalog = {
                "updated_at": "now",
                "products": {
                    "166881": {
                        "product_id": "166881",
                        "language": "english",
                        "name": "Woolly Friends",
                        "category": "Stamps",
                        "status": "Current",
                        "price": "$19.00",
                        "tags": ["cards", "sheep"],
                        "updated_at": "earlier",
                        "images": [
                            {
                                "image_id": "166881",
                                "filename": "166881.png",
                                "b2_url": "https://example.test/166881/166881.png",
                                "thumbnail_url": "https://example.test/166881/166881.thumb.webp",
                                "description": "A sheep stamp set.",
                                "full_text": "Hooray",
                                "bytes": 100,
                            },
                            {
                                "image_id": "166881o01",
                                "filename": "166881o01.png",
                                "b2_url": "https://example.test/166881/166881o01.png",
                                "description": "Outline image.",
                                "full_text": "",
                                "bytes": 50,
                            },
                        ],
                        "descriptions": [{"source": "official", "text": "Official text."}],
                    },
                    "166882": {
                        "product_id": "166882",
                        "language": "german",
                        "tags": [],
                        "images": [{
                            "image_id": "166882",
                            "filename": "166882.png",
                            "b2_url": "https://example.test/166882/166882.png",
                            "description": "German floral stamps.",
                            "full_text": "",
                            "bytes": 25,
                        }],
                    },
                    "166883": {
                        "product_id": "166883",
                        "language": "english",
                        "tags": [],
                        "images": [{
                            "image_id": "166883",
                            "filename": "166883.png",
                            "b2_url": "https://example.test/166883/166883.png",
                            "description": "",
                            "full_text": "",
                            "bytes": 25,
                        }],
                    },
                },
            }

            archive_images.write_archive_catalog_assets(output_dir, catalog)

            summary = json.loads((output_dir / "catalog-summary.json").read_text())
            self.assertEqual(summary[0], {
                "product_id": "166881",
                "language": "english",
                "name": "Woolly Friends",
                "display_name": "Woolly Friends",
                "category": "Stamps",
                "status": "Current",
                "tags": ["cards", "sheep"],
                "image_count": 2,
                "primary_image": {
                    "b2_url": "https://example.test/166881/166881.png",
                    "thumbnail_url": "https://example.test/166881/166881.thumb.webp",
                },
                "detail_shard": "166",
            })
            self.assertEqual(summary[1]["display_name"], "German floral stamps.")
            self.assertNotIn("name", summary[1])
            self.assertEqual(summary[2]["display_name"], "No description yet")

    def test_write_archive_catalog_assets_search_contains_only_omitted_deep_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            archive_images.write_archive_catalog_assets(output_dir, {
                "updated_at": "now",
                "products": {
                    "166881": {
                        "product_id": "166881",
                        "language": "english",
                        "name": "Summary Name",
                        "category": "Summary Category",
                        "status": "Current",
                        "tags": ["summary tag"],
                        "images": [{
                            "filename": "Special-Filename.PNG",
                            "b2_url": "https://example.test/166881.png",
                            "description": "Visible image description",
                            "full_text": "OCR WORDS",
                        }],
                        "descriptions": [
                            {"source": "official", "text": "Official product description"},
                            {"source": "myss", "text": "MYSS product copy"},
                        ],
                    },
                },
            })

            search = json.loads((output_dir / "catalog-search.json").read_text())
            self.assertEqual(
                search,
                {
                    "166881": (
                        "special-filename.png visible image description ocr words "
                        "official product description myss product copy"
                    ),
                },
            )
            self.assertNotIn("summary name", search["166881"])
            self.assertNotIn("summary category", search["166881"])
            self.assertNotIn("summary tag", search["166881"])

    def test_write_archive_catalog_assets_shards_complete_records_deterministically(self):
        products = {
            "168001": {
                "product_id": "168001",
                "language": "english",
                "tags": [],
                "images": [{"filename": "168001.png", "b2_url": "https://example.test/168001.png"}],
            },
            "167999": {
                "product_id": "167999",
                "language": "english",
                "name": "Merged Name",
                "category": "Stamps",
                "status": "Current",
                "price": "$20.00",
                "tags": ["merged"],
                "images": [{"filename": "167999.png", "b2_url": "https://example.test/167999.png"}],
                "descriptions": [{"source": "official", "text": "Merged description."}],
            },
            "167001": {
                "product_id": "167001",
                "language": "french",
                "tags": [],
                "images": [{"filename": "167001.png", "b2_url": "https://example.test/167001.png"}],
            },
        }

        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_dir = Path(first_tmp)
            second_dir = Path(second_tmp)
            archive_images.write_archive_catalog_assets(
                first_dir,
                {"updated_at": "now", "products": products},
            )
            archive_images.write_archive_catalog_assets(
                second_dir,
                {"products": dict(reversed(list(products.items()))), "updated_at": "now"},
            )

            self.assertEqual(
                sorted(path.name for path in (first_dir / "catalog-details").glob("*.json")),
                ["167.json", "168.json"],
            )
            detail_text = (first_dir / "catalog-details" / "167.json").read_text()
            self.assertEqual(
                json.loads(detail_text),
                {"167001": products["167001"], "167999": products["167999"]},
            )
            self.assertEqual(
                detail_text,
                (second_dir / "catalog-details" / "167.json").read_text(),
            )

    def viewer_index_catalog(self, products: dict, updated_at: str = "now") -> dict:
        return {"updated_at": updated_at, "products": products}

    def viewer_index_product(self, product_id: str, image_ids: list[str], **overrides) -> dict:
        extension = overrides.pop("extension", "png")
        return {
            "product_id": product_id,
            "language": "english",
            "tags": [],
            "images": [
                {
                    "image_id": image_id,
                    "filename": f"{image_id}.{extension}",
                    "b2_url": f"https://example.test/{product_id}/{image_id}.{extension}",
                }
                for image_id in image_ids
            ],
            **overrides,
        }

    def test_build_viewer_index_counts_contiguous_variants_and_keeps_names(self):
        catalog = self.viewer_index_catalog({
            "167001": self.viewer_index_product(
                "167001",
                ["167001", "167001o01"],
                name="Woolly Friends",
            ),
            "167002": self.viewer_index_product("167002", ["167002"]),
        })

        index = archive_images._build_viewer_index(catalog)

        self.assertEqual(index["schema_version"], 1)
        self.assertEqual(index["version"], "now")
        self.assertEqual(index["b2_base_url"], archive_images.DEFAULT_B2_BASE_URL)
        self.assertEqual(index["products"], {
            "167001": [2, "Woolly Friends"],
            "167002": [1],
        })

    def test_build_viewer_index_lists_variants_explicitly_when_numbering_has_gaps(self):
        catalog = self.viewer_index_catalog({
            "167001": self.viewer_index_product("167001", ["167001", "167001o02"]),
            "167002": self.viewer_index_product("167002", ["167002o01"], name="Outline Only"),
        })

        index = archive_images._build_viewer_index(catalog)

        self.assertEqual(index["products"], {
            "167001": [[0, 2]],
            "167002": [[1], "Outline Only"],
        })

    def test_build_viewer_index_records_only_non_png_extensions(self):
        catalog = self.viewer_index_catalog({
            "167001": self.viewer_index_product("167001", ["167001"], extension="jpg"),
            "167002": self.viewer_index_product(
                "167002",
                ["167002"],
                extension="jpg",
                name="Named Jpg",
            ),
            "167003": self.viewer_index_product("167003", ["167003"]),
        })

        index = archive_images._build_viewer_index(catalog)

        self.assertEqual(index["products"], {
            "167001": [1, "", "jpg"],
            "167002": [1, "Named Jpg", "jpg"],
            "167003": [1],
        })

    def test_build_viewer_index_skips_products_without_addressable_variants(self):
        catalog = self.viewer_index_catalog({
            "167001": self.viewer_index_product("167001", ["mystery-image"]),
            "167002": self.viewer_index_product("167002", ["167002", "167002-extra"]),
            "167003": {"product_id": "167003", "language": "english", "tags": [], "images": []},
        })

        index = archive_images._build_viewer_index(catalog)

        self.assertEqual(index["products"], {"167002": [1]})

    def test_build_viewer_index_carries_the_catalog_b2_base_url_with_a_trailing_slash(self):
        catalog = self.viewer_index_catalog({})
        catalog["b2_base_url"] = "https://images.example.test/archive"

        index = archive_images._build_viewer_index(catalog)

        self.assertEqual(index["b2_base_url"], "https://images.example.test/archive/")

    def test_write_archive_catalog_assets_writes_the_viewer_index_before_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            events: list[Path] = []
            original_atomic_write = archive_images._atomic_write_json

            def recording_atomic_write(path: Path, data) -> None:
                events.append(path)
                original_atomic_write(path, data)

            catalog = self.viewer_index_catalog(
                {
                    "167001": self.viewer_index_product(
                        "167001",
                        ["167001", "167001o01"],
                        name="Woolly Friends",
                    ),
                },
                updated_at="2026-07-20T20:00:00Z",
            )

            with patch.object(archive_images, "_atomic_write_json", side_effect=recording_atomic_write):
                manifest = archive_images.write_archive_catalog_assets(output_dir, catalog)

            index_path = output_dir / "viewer-index.json"
            self.assertEqual(manifest["viewer_index_url"], "viewer-index.json")
            self.assertIn(index_path, events)
            self.assertLess(events.index(index_path), events.index(output_dir / "catalog-manifest.json"))
            self.assertEqual(json.loads(index_path.read_text()), {
                "schema_version": 1,
                "version": "2026-07-20T20:00:00Z",
                "b2_base_url": archive_images.DEFAULT_B2_BASE_URL,
                "products": {"167001": [2, "Woolly Friends"]},
            })

    def test_write_archive_catalog_assets_minifies_every_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            archive_images.write_archive_catalog_assets(output_dir, {
                "updated_at": "now",
                "products": {
                    "167001": {
                        "product_id": "167001",
                        "language": "english",
                        "tags": [],
                        "images": [{"filename": "167001.png", "b2_url": "https://example.test/167001.png"}],
                    },
                },
            })

            generated_paths = [
                output_dir / "catalog-manifest.json",
                output_dir / "catalog-summary.json",
                output_dir / "catalog-search.json",
                output_dir / "viewer-index.json",
                output_dir / "catalog-details" / "167.json",
            ]
            for path in generated_paths:
                with self.subTest(path=path.name):
                    text = path.read_text()
                    self.assertNotIn("\n", text)
                    self.assertNotIn(": ", text)
                    self.assertNotIn(", ", text)

    def test_write_archive_catalog_assets_removes_stale_shards_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            details_dir = output_dir / "catalog-details"
            details_dir.mkdir()
            stale_path = details_dir / "999.json"
            stale_path.write_text('{"stale":true}')
            preserved_path = details_dir / "notes.txt"
            preserved_path.write_text("keep")
            catalog = {"updated_at": "now", "products": {}}

            with patch.object(
                archive_images,
                "_atomic_write_json",
                side_effect=OSError("write failed"),
            ):
                with self.assertRaisesRegex(OSError, "write failed"):
                    archive_images.write_archive_catalog_assets(output_dir, catalog)

            self.assertTrue(stale_path.exists())

            archive_images.write_archive_catalog_assets(output_dir, catalog)

            self.assertFalse(stale_path.exists())
            self.assertTrue(preserved_path.exists())

    def test_default_catalog_output_dir_lives_at_archive_root(self):
        self.assertEqual(
            archive_images.DEFAULT_CATALOG_OUTPUT_DIR,
            Path(archive_images.__file__).parent,
        )
        args = archive_images.parse_args(["--build-catalog-assets"])
        self.assertEqual(args.catalog_output_dir, archive_images.DEFAULT_CATALOG_OUTPUT_DIR)

    def test_main_build_catalog_assets_does_not_archive_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            output_root.mkdir()
            catalog_output_dir = Path(tmp) / "catalog"

            with patch.object(archive_images, "archive_products") as archive_products:
                with patch("sys.stdout", new_callable=io.StringIO):
                    exit_code = archive_images.main([
                        "--build-catalog-assets",
                        "--output",
                        str(output_root),
                        "--catalog-output-dir",
                        str(catalog_output_dir),
                        "--b2-base-url",
                        "https://stamps.charo.fun/archive/",
                    ])

            self.assertEqual(exit_code, 0)
            archive_products.assert_not_called()
            self.assertTrue((catalog_output_dir / "catalog-manifest.json").exists())

    def test_main_build_catalog_data_compatibility_alias_builds_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            output_root.mkdir()
            catalog_output_dir = Path(tmp) / "catalog"

            with patch("sys.stdout", new_callable=io.StringIO):
                exit_code = archive_images.main([
                    "--build-catalog-data",
                    "--output",
                    str(output_root),
                    "--catalog-output-dir",
                    str(catalog_output_dir),
                ])

            self.assertEqual(exit_code, 0)
            self.assertTrue((catalog_output_dir / "catalog-manifest.json").exists())

    def test_main_build_catalog_data_passes_metadata_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            output_root.mkdir()
            catalog_output_dir = Path(tmp) / "catalog"
            myss_path = Path(tmp) / "myss.jsonl"
            official_path = Path(tmp) / "official.jsonl"
            myss_path.write_text("", encoding="utf-8")
            official_path.write_text("", encoding="utf-8")

            with patch.object(archive_images, "build_archive_catalog_data", return_value={"products": {}}) as build_catalog:
                with patch.object(archive_images, "write_archive_catalog_assets"):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        exit_code = archive_images.main([
                            "--build-catalog-assets",
                            "--output",
                            str(output_root),
                            "--catalog-output-dir",
                            str(catalog_output_dir),
                            "--myss-metadata",
                            str(myss_path),
                            "--official-metadata",
                            str(official_path),
                        ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(build_catalog.call_args.kwargs["myss_metadata_path"], myss_path)
            self.assertEqual(build_catalog.call_args.kwargs["official_metadata_path"], official_path)

    def test_main_build_catalog_data_accepts_repeated_add_metadata_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "output"
            output_root.mkdir()
            catalog_output_dir = Path(tmp) / "catalog"
            first_metadata_path = Path(tmp) / "first.jsonl"
            second_metadata_path = Path(tmp) / "second.jsonl"
            first_metadata_path.write_text("", encoding="utf-8")
            second_metadata_path.write_text("", encoding="utf-8")

            with patch.object(archive_images, "build_archive_catalog_data", return_value={"products": {}}) as build_catalog:
                with patch.object(archive_images, "write_archive_catalog_assets"):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        exit_code = archive_images.main([
                            "--build-catalog-data",
                            "--output",
                            str(output_root),
                            "--catalog-output-dir",
                            str(catalog_output_dir),
                            "--add-metadata",
                            str(first_metadata_path),
                            "--add-metadata",
                            str(second_metadata_path),
                        ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(build_catalog.call_args.kwargs["additional_metadata_paths"], [first_metadata_path, second_metadata_path])


if __name__ == "__main__":
    unittest.main()
