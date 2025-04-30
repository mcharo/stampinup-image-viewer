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

## Live Demo

Visit [https://mcharo.github.io/stampinup-image-viewer](https://mcharo.github.io/stampinup-image-viewer) to try the tool.

## Local Development

1. Clone this repository
2. Open index.html in your browser
3. No build process required - it's all client-side JavaScript

## Credits

This project uses:
- [Potrace](http://potrace.sourceforge.net/) - For bitmap to SVG conversion
- Modified browser-based JavaScript implementation of Potrace 