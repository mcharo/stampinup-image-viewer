# Stampin' Up Image Converter

A web-based tool to convert Stampin' Up product images to SVG format. This tool allows you to:
- Load product images by ID
- Automatically convert black and white images to SVG
- Adjust SVG colors using a color picker
- Download the resulting SVG files

## Usage

1. Enter a product ID in the input field
2. The tool will load both the regular and outline (o01) images
3. If the outline image is black and white, it will be automatically converted to SVG
4. Use the color picker to change the SVG color
5. Click "Download SVG" to save the converted image

When the tool is served over HTTP alongside the `archive/` site, it also reads
`archive/viewer-index.json` to see every image a product has. Products with more
than two images offer the remainder behind a single "Load" button, and any image
the CDN no longer serves is retried against the archived copy.

### Editable projects

Use **Save Project** to download a self-contained `.stampinup-project` file containing the source images, vector settings, extracted objects, and composition. Use **Open Project** to restore that workspace and continue editing. Project files include their source images and can be reopened offline.

### Die sheet extraction (experimental, currently hidden)

This is turned off at the moment and no source card offers it; the description below is kept for when it comes back.

Upload a photograph of a metal die sheet and use **Extract Dies from Sheet** to split it into one object per die, each traced to its own SVG and ready to download or drop into a composition.

This works on a photo of the dies themselves. It separates steel from the backdrop by colour warmth rather than brightness, so a sheet shot on wood or paper works even where the metal catches highlights brighter than the background. **Mirror** is on by default because dies are photographed face-up and therefore read reversed; turn it off for a photo already shot in reading orientation.

Where a product offers a flat illustration of the finished cut shapes alongside the photo of the steel, prefer the illustration. Extraction handles both, but a photo of steel can only ever record the cutting edge, so its shapes come out coarse — outlines weld shut and letter counters can fill in — whereas the illustration already shows the shapes the die cuts and separates perfectly. Turn **Mirror** off for an illustration, which is drawn in reading orientation.

The feature is experimental and opt-in. Its code loads only when you click the button, and a saved project keeps the extracted results as ordinary images and vectors, so reopening one never depends on it.

## Live Demo

Visit [https://mcharo.github.io/stampinup-image-viewer](https://mcharo.github.io/stampinup-image-viewer) to try the tool.

## Local Development

1. Clone this repository
2. Open index.html in your browser
3. No build process required - it's all client-side JavaScript

## Catalog Maintenance

The root Makefile wraps the local catalog helper scripts. The default refresh is safe: it reconciles existing files in `test-output` with `catalog/catalog.json`, detects die sets from local product JPGs, and rebuilds `catalog/catalog-data.js`; OCR/API-backed work is kept behind explicit targets.

```sh
make catalog-refresh
make catalog-detect-dies
make catalog-lookup-products
make catalog-ocr
make convert START_ID=166635 DIRECTION=up
make convert-dies START_ID=167043 LIMIT=1
```

Useful variables include `INPUT_DIR`, `CATALOG`, `CATALOG_DATA`, `PATTERN`, `MAX_404S`, `DELAY`, and `LIMIT`.

Die detection is a local image-analysis pass that mirrors the browser viewer's neutral gray die-sheet detector. It rejects product photos with a stamp-set package/header so bundles are not treated as standalone dies. Detected entries are stored with `is_die_set: true` and appear as dies in the catalog UI/search.

Standalone die product photos do not contain product names, so product details come from an explicit network-backed lookup step. `make catalog-lookup-products` fetches official Stampin' Up product pages by product ID, fills missing die `display_name`, `stamp_set_name`, and product detail fields in the catalog, then rebuilds `catalog/catalog-data.js`.

### OCR/API Requirements

The safe `make catalog-refresh` target does not require API credentials or network-backed product-page lookup. API-backed targets such as `make catalog-ocr`, `make catalog-force-ocr`, `make catalog-redetect-names`, and `make convert START_ID=...` require:

- `uv` available on your PATH; the helper projects use their own `pyproject.toml` and `uv.lock` files.
- An Anthropic API key exported in your shell:

```sh
export ANTHROPIC_API_KEY="your-api-key"
```

- Network access and Anthropic billing enabled for the API calls.
- Existing PNG assets in `INPUT_DIR` for catalog OCR targets.
- Matching JPG assets in `INPUT_DIR` for `catalog-redetect-names`.
- The `potrace` command-line tool for `make convert` and `make convert-dies`, because conversion fetches product images and traces raster images to SVG.

## Credits

This project uses:
- [Potrace](http://potrace.sourceforge.net/) - For bitmap to SVG conversion
- Modified browser-based JavaScript implementation of Potrace
