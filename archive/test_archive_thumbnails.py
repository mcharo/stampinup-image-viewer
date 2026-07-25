# pylint: disable=import-error

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

import archive_thumbnails


def image_bytes(
    size: tuple[int, int] = (800, 400),
    mode: str = "RGB",
    color=(20, 80, 140),
    image_format: str = "PNG",
    exif=None,
) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color=color).save(buffer, format=image_format, exif=exif)
    return buffer.getvalue()


def client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}},
        operation,
    )


class FakeS3Client:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.head_errors: dict[str, ClientError] = {}
        self.get_calls: list[dict] = []
        self.head_calls: list[dict] = []
        self.upload_calls: list[dict] = []

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        key = kwargs["Key"]
        if key in self.head_errors:
            raise self.head_errors[key]
        if key not in self.objects:
            raise client_error("404", "HeadObject")
        return {"Metadata": self.objects[key].get("Metadata", {})}

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        key = kwargs["Key"]
        if key not in self.objects:
            raise client_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[key]["Body"])}

    def put_object(self, **kwargs):
        self.upload_calls.append(kwargs)
        self.objects[kwargs["Key"]] = {
            "Body": kwargs["Body"],
            "Metadata": kwargs.get("Metadata", {}),
        }
        return {}


class ArchiveThumbnailTransformTest(unittest.TestCase):
    def test_create_thumbnail_limits_dimensions_and_returns_webp_metadata(self):
        source = image_bytes((1024, 256))

        thumbnail = archive_thumbnails.create_thumbnail(source, "166635")

        self.assertEqual(thumbnail.filename, "166635.thumb.webp")
        self.assertEqual((thumbnail.width, thumbnail.height), (512, 128))
        self.assertEqual(thumbnail.byte_count, len(thumbnail.data))
        self.assertEqual(thumbnail.sha256, hashlib.sha256(thumbnail.data).hexdigest())
        self.assertEqual(thumbnail.source_sha256, hashlib.sha256(source).hexdigest())
        with Image.open(io.BytesIO(thumbnail.data)) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(image.size, (512, 128))

    def test_create_thumbnail_applies_exif_orientation(self):
        exif = Image.Exif()
        exif[274] = 6
        source = image_bytes((120, 40), image_format="JPEG", exif=exif)

        thumbnail = archive_thumbnails.create_thumbnail(source, "166635")

        self.assertEqual((thumbnail.width, thumbnail.height), (40, 120))

    def test_create_thumbnail_preserves_transparency(self):
        source = image_bytes((32, 32), mode="RGBA", color=(50, 100, 150, 0))

        thumbnail = archive_thumbnails.create_thumbnail(source, "166635")

        with Image.open(io.BytesIO(thumbnail.data)) as image:
            self.assertIn("A", image.getbands())
            self.assertEqual(image.convert("RGBA").getpixel((0, 0))[3], 0)

    def test_create_thumbnail_rejects_malformed_input(self):
        with self.assertRaises(OSError):
            archive_thumbnails.create_thumbnail(b"not an image", "166635")


class ArchiveThumbnailSyncTest(unittest.TestCase):
    def write_product(
        self,
        output_root: Path,
        product_id: str,
        source: bytes,
        *,
        write_local_source: bool = True,
    ) -> tuple[Path, Path]:
        product_dir = output_root / product_id
        product_dir.mkdir(parents=True)
        filename = f"{product_id}.png"
        source_path = product_dir / filename
        if write_local_source:
            source_path.write_bytes(source)
        index_path = product_dir / "index.json"
        index_path.write_text(
            json.dumps({
                "product_id": product_id,
                "extension": "png",
                "images": {
                    product_id: {
                        "filename": filename,
                        "sha256": hashlib.sha256(source).hexdigest(),
                    }
                },
            }),
            encoding="utf-8",
        )
        return source_path, index_path

    def test_sync_uses_local_source_and_uploads_expected_headers_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            source = image_bytes()
            _, index_path = self.write_product(output_root, "166635", source)
            s3 = FakeS3Client()

            summary = archive_thumbnails.sync_thumbnails(
                output_root=output_root,
                s3_client=s3,
                bucket="catalog",
            )

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(s3.get_calls, [])
            self.assertEqual(len(s3.upload_calls), 1)
            upload = s3.upload_calls[0]
            self.assertEqual(upload["Bucket"], "catalog")
            self.assertEqual(upload["Key"], "archive/166635/166635.thumb.webp")
            self.assertEqual(upload["ContentType"], "image/webp")
            self.assertEqual(upload["CacheControl"], "public, max-age=31536000, immutable")
            self.assertEqual(
                upload["Metadata"],
                {"source-sha256": hashlib.sha256(source).hexdigest()},
            )
            thumbnail_path = output_root / "166635" / "166635.thumb.webp"
            self.assertTrue(thumbnail_path.is_file())
            entry = json.loads(index_path.read_text())["images"]["166635"]
            self.assertEqual(entry["thumbnail_filename"], "166635.thumb.webp")
            self.assertEqual(entry["thumbnail_width"], 512)
            self.assertEqual(entry["thumbnail_height"], 256)
            self.assertEqual(entry["thumbnail_bytes"], thumbnail_path.stat().st_size)
            self.assertEqual(
                entry["thumbnail_sha256"],
                hashlib.sha256(thumbnail_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                entry["thumbnail_source_sha256"],
                hashlib.sha256(source).hexdigest(),
            )

    def test_sync_downloads_original_from_s3_when_local_source_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            source = image_bytes((300, 600))
            source_path, _ = self.write_product(
                output_root,
                "166635",
                source,
                write_local_source=False,
            )
            s3 = FakeS3Client()
            s3.objects["archive/166635/166635.png"] = {"Body": source, "Metadata": {}}

            summary = archive_thumbnails.sync_thumbnails(
                output_root=output_root,
                s3_client=s3,
                bucket="catalog",
            )

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(
                s3.get_calls,
                [{"Bucket": "catalog", "Key": "archive/166635/166635.png"}],
            )
            self.assertFalse(source_path.exists())
            self.assertTrue((output_root / "166635" / "166635.thumb.webp").exists())

    def test_sync_dry_run_performs_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            source = image_bytes()
            _, index_path = self.write_product(output_root, "166635", source)
            original_index = index_path.read_bytes()
            s3 = FakeS3Client()

            summary = archive_thumbnails.sync_thumbnails(
                output_root=output_root,
                s3_client=s3,
                bucket="catalog",
                dry_run=True,
            )

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(s3.upload_calls, [])
            self.assertEqual(index_path.read_bytes(), original_index)
            self.assertFalse((output_root / "166635" / "166635.thumb.webp").exists())

    def test_sync_skips_matching_remote_source_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            source = image_bytes()
            _, index_path = self.write_product(output_root, "166635", source)
            thumbnail = archive_thumbnails.create_thumbnail(source, "166635")
            thumbnail_path = output_root / "166635" / thumbnail.filename
            thumbnail_path.write_bytes(thumbnail.data)
            index = json.loads(index_path.read_text())
            index["images"]["166635"].update(thumbnail.index_metadata())
            index_path.write_text(json.dumps(index), encoding="utf-8")
            s3 = FakeS3Client()
            s3.objects["archive/166635/166635.thumb.webp"] = {
                "Body": thumbnail.data,
                "Metadata": {"source-sha256": thumbnail.source_sha256},
            }

            with patch.object(archive_thumbnails, "_atomic_write_bytes") as write_bytes:
                with patch.object(archive_thumbnails, "_atomic_write_json") as write_json:
                    summary = archive_thumbnails.sync_thumbnails(
                        output_root=output_root,
                        s3_client=s3,
                        bucket="catalog",
                    )

            self.assertEqual(summary.skipped, 1)
            self.assertEqual(summary.uploaded, 0)
            self.assertEqual(s3.upload_calls, [])
            write_bytes.assert_not_called()
            write_json.assert_not_called()

    def test_sync_force_uploads_even_when_remote_source_sha_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            source = image_bytes()
            self.write_product(output_root, "166635", source)
            s3 = FakeS3Client()
            s3.objects["archive/166635/166635.thumb.webp"] = {
                "Body": b"old",
                "Metadata": {"source-sha256": hashlib.sha256(source).hexdigest()},
            }

            summary = archive_thumbnails.sync_thumbnails(
                output_root=output_root,
                s3_client=s3,
                bucket="catalog",
                force=True,
            )

            self.assertEqual(summary.uploaded, 1)
            self.assertEqual(s3.head_calls, [])
            self.assertEqual(len(s3.upload_calls), 1)

    def test_sync_treats_real_head_error_as_failure_not_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            self.write_product(output_root, "166635", image_bytes())
            s3 = FakeS3Client()
            thumbnail_key = "archive/166635/166635.thumb.webp"
            s3.head_errors[thumbnail_key] = client_error("AccessDenied", "HeadObject")

            summary = archive_thumbnails.sync_thumbnails(
                output_root=output_root,
                s3_client=s3,
                bucket="catalog",
            )

            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.succeeded, 0)
            self.assertEqual(s3.upload_calls, [])
            self.assertIn("AccessDenied", summary.errors["166635"])

    def test_main_continues_after_product_failure_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            self.write_product(output_root, "166635", b"malformed")
            self.write_product(output_root, "166636", image_bytes())
            s3 = FakeS3Client()
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                exit_code = archive_thumbnails.main(
                    [
                        "--endpoint-url",
                        "https://s3.example.test",
                        "--bucket",
                        "catalog",
                        "--output-root",
                        str(output_root),
                    ],
                    s3_client=s3,
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(len(s3.upload_calls), 1)
            self.assertIn("failed=1", stdout.getvalue())
            self.assertIn("166635", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
