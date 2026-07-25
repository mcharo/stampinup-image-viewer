# Image Archive

Archive Stampin' Up CDN product images by explicit product ID. This tool is
generic: it downloads product images as-is and does not convert SVGs or scrape
Stampin' Up product pages. It can also generate the static JSON assets used by
the archive catalog.

## Usage

Run from the repository root:

```bash
uv run --project archive python archive/archive_images.py --product-id 166635
```

Multiple bounded input forms are supported:

```bash
uv run --project archive python archive/archive_images.py \
  --product-id 166635 \
  --product-id 166636

uv run --project archive python archive/archive_images.py \
  --ids-file product_ids.txt

uv run --project archive python archive/archive_images.py \
  --start-id 166000 \
  --end-id 166100
```

Ranges are capped at 10,000 product IDs per run to keep CDN access bounded.

The default extension is `png`. Use `--extension jpg` to fetch JPG assets.

## CDN Behavior

For each product ID, the script fetches the base image and then zero-padded
suffix images:

```text
166635.png
166635o01.png
166635o02.png
```

Suffix enumeration stops after the first 404 by default. Change that with
`--max-missing-suffixes` if a product appears to have gaps. The script uses one
CDN request at a time and treats `--delay` as the minimum spacing between CDN
request starts. The default is `1` second with small jitter, but time spent
downloading, saving, or describing an image counts toward that spacing. It skips
already downloaded files and retries 429, 503, connection, and timeout errors
with exponential backoff.

Add `--debug` to print progress messages to stderr. Each debug line is prefixed
with an ISO timestamp and the current product ID, for example
`2026-06-03T21:41:00Z [157928] fetching 157928.png`.

## Output

The default output root is `archive/output/`. Each product ID gets its own
directory:

```text
archive/output/166635/
  166635.png
  166635o01.png
  166635.thumb.webp
  index.json
```

`index.json` is keyed by image ID and stores filename, URL, byte count, SHA-256,
download time, and optional description metadata. Product indexes include
top-level `language` and `tags` fields. `language` defaults to `english` and is
updated when any image detects non-English text. Each image entry includes
`full_text`, which stores only text directly visible in that image and may be an
empty string.

Normal archive runs generate or refresh a WebP thumbnail for the primary image.
The thumbnail is at most 512 by 512 pixels and is named
`<product-id>.thumb.webp`. Its primary-image entry in `index.json` stores
`thumbnail_filename`, `thumbnail_width`, `thumbnail_height`,
`thumbnail_bytes`, `thumbnail_sha256`, and `thumbnail_source_sha256`. Suffix
images do not get card thumbnails. A thumbnail error is reported with `--debug`
but does not abort the archive run.

The output root also contains `scan-index.json`, which tracks scanned product
IDs separately as `seen` and `missing`, plus compressed ranges for scanned,
seen, and missing IDs. Normal archive runs update this file after each product
ID.

If you run multiple archive processes against the same output folder, use
`--skip-scan-index` to avoid concurrent read/write races on `scan-index.json`.
After those runs finish, rebuild the scan index over the combined range with
`--backfill-scan-index`.

To rebuild `scan-index.json` from existing local output without CDN requests:

```bash
uv run --project archive python archive/archive_images.py \
  --backfill-scan-index \
  --start-id 156900 \
  --end-id 157600
```

## Descriptions

Descriptions are disabled by default. To use Claude Haiku, set
`ANTHROPIC_API_KEY` and pass `--describe`:

```bash
ANTHROPIC_API_KEY=... uv run --project archive python archive/archive_images.py \
  --product-id 166635 \
  --describe
```

The default model is `claude-haiku-4-5-20251001`. Override it with `--model`.
Existing descriptions are preserved unless `--force-descriptions` is provided.

Large images are downsampled and re-encoded only for the Anthropic request. The
original archived image file is left unchanged. If a description request fails,
the script records `description_error` and `description_error_at` for that image
and continues archiving later product IDs.

To rerun only descriptions against already downloaded output without rescanning
the CDN, use `--describe-existing`:

```bash
ANTHROPIC_API_KEY=... uv run --project archive python archive/archive_images.py \
  --describe-existing \
  --output archive/output
```

Add `--product-id`, `--ids-file`, or `--start-id`/`--end-id` to limit which
existing product directories are described. Add `--force-descriptions` to refresh
entries that already have descriptions.

## Catalog View

The generated catalog is a static site rooted at `archive/`:

```text
archive/
  index.html
  catalog-ui.js
  catalog-manifest.json
  catalog-summary.json
  catalog-search.json
  viewer-index.json
  catalog-details/
    100.json
    101.json
    ...
```

`catalog-manifest.json` contains the schema version, generation version, and
relative asset URLs. The page fetches it without a browser cache, then loads the
thin `catalog-summary.json` needed for the product cards. Cards are rendered in
batches of 96. The larger `catalog-search.json` deep-search index is fetched
only after the first non-empty search. Complete product records are grouped into
detail shards by the first three characters of the product ID; a shard is
fetched and cached only when a product modal needs it.

Card images prefer `thumbnail_url` and retry the original `b2_url` if the
thumbnail cannot load. Catalog generation omits `thumbnail_url` when the local
primary-image index has no thumbnail metadata, so older products fall back
directly to the original image. Published thumbnail URLs retain the stable
`<product-id>.thumb.webp` object name and append the full stored thumbnail hash
as `?v=<sha256>` so immutable browser caches refresh when the object changes.

`viewer-index.json` is for the SVG viewer at the repository root rather than the
catalog page. It is the whole archive in the smallest form the viewer needs —
about 350 KB, versus 1.6 MB for the largest single detail shard — mapping each
product ID to `[variants, name?, extension?]`:

```json
{
  "schema_version": 1,
  "version": "2026-07-20T22:30:35Z",
  "b2_base_url": "https://stamps.charo.fun/archive/",
  "products": {
    "100531": [2, "Old Olive Classic Stampin' Ink Refill"],
    "150306": [1]
  }
}
```

`variants` is the image count when the variant numbers run `0..count-1`, which is
true for every product today, and an explicit list such as `[0, 2]` when they do
not. Variant 0 is the main image and variant *n* is the `oNN` suffix. The name is
omitted when the archive has none, and the extension only appears when the images
are not PNG. The viewer needs this because it otherwise only knows to request the
main image and `o01`, while 1,532 products have three or more images.

Each product modal ends with an "Open in viewer" link that opens the SVG viewer
at `index.html?id=<product-id>` in a new tab, matching the catalog view.

GitHub Pages serves these files as ordinary static assets under the repository's
`archive/` path. The manifest version is appended to summary, search, and detail
requests for cache busting. Generate and publish the data assets before the
manifest; the generator writes the manifest last. No server-side application or
database is required.

The catalog now uses `fetch()`, so opening `archive/index.html` through a
`file://` URL is unsupported. Serve the repository over HTTP from its root:

```bash
python3 -m http.server 8000
```

Then open <http://localhost:8000/archive/>.

### Generate catalog assets

The default B2 base URL is `https://stamps.charo.fun/archive/`. Regenerate the
catalog from local output with the merged MYSS metadata that is available in
this repository:

```bash
uv run --project archive python archive/archive_images.py \
  --build-catalog-assets \
  --output archive/output \
  --catalog-output-dir archive \
  --b2-base-url https://stamps.charo.fun/archive/ \
  --myss-metadata myss-scraper/myss-products-with-details-merged.jsonl
```

Catalog generation reads local indexes and does not make CDN, B2, or metadata
site requests. Optional metadata inputs are:

- `--myss-metadata PATH` for MYSS JSONL records.
- `--official-metadata PATH` for official Stampin' Up JSONL records.
- `--add-metadata PATH` for other JSONL records; this option is repeatable, and
  the first matching additional record wins.

The older `--build-catalog-data` flag remains as a compatibility alias for
`--build-catalog-assets`. New commands should use `--build-catalog-assets` and
`--catalog-output-dir`.

## Thumbnail Backfill to B2 S3

`archive/archive_thumbnails.py` backfills primary-image thumbnails and uploads
them through Backblaze B2's S3-compatible API. It processes each product
independently, continues after per-product errors, and returns a nonzero status
when any product fails. Re-running the full command is resumable: current local
thumbnails and remote objects whose `source-sha256` metadata matches are
skipped.

The script uses boto3's standard AWS credential chain. For a one-off shell,
export the Backblaze application key ID and application key:

```bash
export AWS_ACCESS_KEY_ID='<Backblaze application key ID>'
export AWS_SECRET_ACCESS_KEY='<Backblaze application key>'
export B2_S3_ENDPOINT='https://s3.<region>.backblazeb2.com'
export B2_S3_BUCKET='stampinup-full-backup'
```

Alternatively, store the credentials in the standard AWS shared credentials
file outside the repository and select it with `AWS_PROFILE`. Never put keys in
tracked files, shell scripts, command arguments, or documentation.

Use product `167002` for a read-only single-product check:

```bash
uv run --project archive python archive/archive_thumbnails.py \
  --endpoint-url "$B2_S3_ENDPOINT" \
  --bucket "$B2_S3_BUCKET" \
  --key-prefix archive \
  --output-root archive/output \
  --product-id 167002 \
  --dry-run
```

After checking the dry-run summary, upload that one canary:

```bash
uv run --project archive python archive/archive_thumbnails.py \
  --endpoint-url "$B2_S3_ENDPOINT" \
  --bucket "$B2_S3_BUCKET" \
  --key-prefix archive \
  --output-root archive/output \
  --product-id 167002
```

Then run the resumable full backfill:

```bash
uv run --project archive python archive/archive_thumbnails.py \
  --endpoint-url "$B2_S3_ENDPOINT" \
  --bucket "$B2_S3_BUCKET" \
  --key-prefix archive \
  --output-root archive/output
```

`--dry-run` performs no local or remote writes, but it still checks B2 and
therefore requires working endpoint and credentials. The canary and full runs
write thumbnail files and metadata to local output as well as uploading the
WebP objects.

The normal rollout order is:

1. Run the archive job; new and revisited primary images get local thumbnails.
2. Run the thumbnail dry-run, canary upload, and resumable full B2 backfill.
3. Regenerate catalog assets after thumbnail metadata has been written.
4. Run the tests and serve `archive/` locally over HTTP.
5. Publish the archive site only after those checks pass.

## Cross-Origin Reads for the Viewer

The SVG viewer at the repository root falls back to archive-hosted images when
Cloudinary has nothing for a product ID. It reads image bytes with `fetch()` and
inspects pixels on a canvas, so those responses need an
`Access-Control-Allow-Origin` header. The viewer's request is a simple
cross-origin `GET` with no custom headers, so no preflight is involved and that
one response header is all it needs.

**That header comes from a Cloudflare Transform Rule, not from the bucket.**
`stamps.charo.fun` is fronted onto B2's *native* download API — its responses
carry `x-bz-file-name` and `x-bz-file-id` rather than S3 headers — and bucket CORS
rules written through the S3-compatible `PutBucketCors` only cover the `s3_get`
and `s3_head` operations, so they never match that path. Measured on the same
object, the S3 endpoint returns the header while `stamps.charo.fun` returns none.

The rule lives in the `charo.fun` zone under Rules → Create rule → Response Header
Transform Rule (labeled "Modify Response Header" in older dashboards):

- Name: `archive-cors`
- Expression: `(http.host eq "stamps.charo.fun")`
- Action: Set static, `Access-Control-Allow-Origin: *`

**Do not add a `URI Path starts with /archive/` condition.** The same zone has URL
Rewrite Rules (`stamps-archive-b2`, `stamps-b2`) that rewrite the path to the B2
`/file/<bucket>/...` form, and rewrites run in an earlier phase than the response
header transform, which therefore evaluates its expression against the *rewritten*
path. A `/archive/` condition silently never matches — verified: with the path
condition the header was absent even on `/archive/nope.png`, and adding it back is
the one way to break this rule while leaving it showing as Active. Matching on the
hostname alone is fine, since that host serves nothing but archive images.

A static value at the edge is deliberate. B2 reflects the *caller's* origin (plus
`Access-Control-Allow-Credentials: true` and `Vary: Origin`) even when its own rule
says `*`, and Cloudflare does not honor `Vary: Origin` in its cache key, so an
origin-specific header would be cached and then served to a different origin. A
constant `*` is identical for every caller and therefore safe to cache, and it
also covers copies warmed by ordinary `<img>` loads, which send no `Origin` at all
and so would never receive a header from B2 anyway. Response header transform
rules run on cache hits as well as misses, so adding this rule needs no cache
purge.

Verify with a cache-busting query string, which gives the CDN a distinct cache key
and so reveals what the origin itself returns:

```bash
URL='https://stamps.charo.fun/archive/166635/166635o01.png'
curl -sSI -H 'Origin: https://mcharo.github.io' "$URL?cachebust=$RANDOM" | grep -i access-control
curl -sSI "$URL?cachebust=$RANDOM" | grep -i access-control
```

Both must show `access-control-allow-origin`. Until they do, the viewer still
works: it silently keeps using Cloudinary and skips variants Cloudinary cannot
serve.

To exercise the fallback end to end, open the viewer at `index.html?id=170012`
over HTTP. Cloudinary serves that product's main image but returns 404 for
`170012o01.jpg`, while the archive has the outline, so the outline card appearing
at all means the fallback worked. The substitution is safe for the conversion
path: archive PNGs carry no alpha channel, and the viewer's black-and-white metric
scores the PNG and the Cloudinary JPG within 0.05 percentage points of each other,
so a fallback load reaches the same vectorize-or-not decision.

## Bucket CORS

Bucket-level CORS rules do not affect the viewer, which reads through the CDN, but
they are worth having for anything that addresses the bucket's S3 endpoint
directly. `archive/configure_cors.py` manages them through the same
S3-compatible API and credential chain as the thumbnail backfill. Check the
current state first:

```bash
uv run --project archive python archive/configure_cors.py \
  --endpoint-url "$B2_S3_ENDPOINT" \
  --bucket "$B2_S3_BUCKET" \
  --dry-run
```

Then apply them:

```bash
uv run --project archive python archive/configure_cors.py \
  --endpoint-url "$B2_S3_ENDPOINT" \
  --bucket "$B2_S3_BUCKET"
```

The default rule allows `GET` and `HEAD` from any origin with a preflight lifetime
of 86400 seconds, which is the maximum B2 accepts. Pass `--allowed-origin`
(repeatable) to narrow it; B2 answers with the caller's origin rather than the
literal `*`, so a narrower list mainly changes who gets refused, not what a
permitted caller sees.

Bucket keys do not include the `archive/` path segment that appears in the public
URL: `https://stamps.charo.fun/archive/166635/166635o01.png` is the object
`166635/166635o01.png`. The CDN adds that segment, so a direct S3-endpoint request
has to drop it.

## Tests

Run all archive Python tests:

```bash
uv run --project archive python -m unittest discover -s archive -p 'test_*.py'
```

Run the Node archive UI tests:

```bash
node --test archive/test_catalog_ui.js
```

Syntax, lockfile, and whitespace checks:

```bash
uv run --project archive python -m py_compile archive/*.py
node --check archive/catalog-ui.js
node --check archive/test_catalog_ui.js
uv lock --project archive --check
git diff --check
```
