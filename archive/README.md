# Image Archive

Archive Stampin' Up CDN product images by explicit product ID. This tool is
generic: it downloads product images as-is and does not convert SVGs, update the
browser catalog, or scrape Stampin' Up product pages.

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

Ranges are capped at 500 product IDs per run to keep CDN access bounded.

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
request at a time, waits `1.5` seconds between CDN requests by default, skips
already downloaded files, and retries 429, 503, connection, and timeout errors
with exponential backoff.

Add `--debug` to print progress messages to stderr. Each debug line is prefixed
with the current product ID, for example `[157928] fetching 157928.png`.

## Output

The default output root is `archive/output/`. Each product ID gets its own
directory:

```text
archive/output/166635/
  166635.png
  166635o01.png
  index.json
```

`index.json` is keyed by image ID and stores filename, URL, byte count, SHA-256,
download time, and optional description metadata. Product indexes include
top-level `language` and `tags` fields. `language` defaults to `english` and is
updated when any image detects non-English text. Each image entry includes
`full_text`, which stores only text directly visible in that image and may be an
empty string.

The output root also contains `scan-index.json`, which tracks scanned product
IDs separately as `seen` and `missing`, plus compressed ranges for scanned,
seen, and missing IDs. Normal archive runs update this file after each product
ID.

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

The archive catalog view lives at `archive/index.html`. It reads
`archive/catalog-data.js`, filters by language with English as the
default, searches product IDs, tags, descriptions, and per-image `full_text`, and
lazy-loads B2 image links only.

The default B2 base URL is `https://stamps.charo.fun/archive/`.
Regenerate the catalog data from local output when the archive run is at a safe
point:

```bash
uv run --project archive python archive/archive_images.py \
  --build-catalog-data \
  --output archive/output \
  --catalog-data archive/catalog-data.js \
  --b2-base-url https://stamps.charo.fun/archive/
```

## Tests

```bash
uv run --project archive python -m unittest archive/test_archive_images.py
```
