(function (root, factory) {
    const api = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.StampinUpProjectFile = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
    'use strict';

    const FORMAT = 'stampinup-image-viewer-project';
    const SCHEMA_VERSION = 1;
    const MIME_TYPE = 'application/vnd.stampinup-image-viewer.project+json';
    const MAX_PROJECT_BYTES = 100 * 1024 * 1024;
    const MAX_ASSET_BYTES = 75 * 1024 * 1024;
    const MAX_DIMENSION = 32768;
    const MAX_PIXELS = 100000000;
    const MAX_TRANSFORM = 1000000;
    const CRICUT_BORDER_MAX = 120;
    const CRICUT_FLOOD_MAX = 300;
    const RASTER_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
    const RENDER_MODES = new Set(['binary', 'color']);
    const SVG_ELEMENTS = new Set(['svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon']);
    const SVG_ATTRIBUTES = new Set([
        'xmlns', 'viewBox', 'version', 'width', 'height', 'x', 'y', 'rx', 'ry',
        'cx', 'cy', 'r', 'x1', 'x2', 'y1', 'y2', 'points', 'd', 'fill',
        'fill-rule', 'clip-rule', 'stroke', 'stroke-width', 'stroke-linecap',
        'stroke-linejoin', 'opacity', 'transform'
    ]);

    const MIGRATIONS = Object.create(null);

    function fail(message) {
        throw new Error(`Invalid Stampin' Up project: ${message}`);
    }

    function btoaCompat(binary) {
        if (typeof btoa === 'function') return btoa(binary);
        return Buffer.from(binary, 'binary').toString('base64');
    }

    function atobCompat(base64) {
        if (typeof atob === 'function') return atob(base64);
        return Buffer.from(base64, 'base64').toString('binary');
    }

    function requireObject(value, label) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object`);
        return value;
    }

    function requireArray(value, label) {
        if (!Array.isArray(value)) fail(`${label} must be an array`);
        return value;
    }

    function requireString(value, label, allowEmpty = false) {
        if (typeof value !== 'string' || (!allowEmpty && !value)) fail(`${label} must be a string`);
        return value;
    }

    function requireFinite(value, label, min, max) {
        if (!Number.isFinite(value) || value < min || value > max) fail(`${label} is out of range`);
        return value;
    }

    function requireInteger(value, label, min, max) {
        if (!Number.isInteger(value) || value < min || value > max) fail(`${label} is out of range`);
        return value;
    }

    function requireColorState(value, label) {
        const color = requireObject(value, label);
        if (typeof color.value !== 'string' || !/^#[0-9a-f]{6}$/i.test(color.value)) {
            fail(`${label}.value must be a six-digit hex color`);
        }
        requireString(color.palette, `${label}.palette`);
        return color;
    }

    function requireCricutState(value, label) {
        const cricut = requireObject(value, label);
        requireInteger(cricut.outerBufferPx, `${label}.outerBufferPx`, 0, CRICUT_BORDER_MAX);
        requireInteger(cricut.gapFloodPx, `${label}.gapFloodPx`, 0, CRICUT_FLOOD_MAX);
        return cricut;
    }

    function requireDimensions(value, label, widthKey = 'width', heightKey = 'height') {
        const width = requireInteger(value[widthKey], `${label} ${widthKey}`, 1, MAX_DIMENSION);
        const height = requireInteger(value[heightKey], `${label} ${heightKey}`, 1, MAX_DIMENSION);
        if (width * height > MAX_PIXELS) fail(`${label} exceeds the pixel limit`);
    }

    function decodedBase64Bytes(data) {
        const canonicalBase64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/][AQgw]==|[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]=)?$/;
        if (typeof data !== 'string' || !canonicalBase64.test(data)) {
            fail('asset data is malformed base64');
        }
        return data.length
            ? Math.floor(data.length * 3 / 4) - (data.endsWith('==') ? 2 : data.endsWith('=') ? 1 : 0)
            : 0;
    }

    function assertProjectTextSize(text, maxBytes = MAX_PROJECT_BYTES) {
        if (typeof text !== 'string') fail('project text must be a string');
        requireInteger(maxBytes, 'project byte limit', 0, Number.MAX_SAFE_INTEGER);
        let size;
        if (root && typeof root.TextEncoder === 'function') {
            size = new root.TextEncoder().encode(text).byteLength;
        } else if (typeof Buffer !== 'undefined') {
            size = Buffer.byteLength(text, 'utf8');
        } else {
            fail('UTF-8 encoder is unavailable');
        }
        if (size > maxBytes) fail('file exceeds the project size limit');
        return size;
    }

    function assertDecodedAssetSize(data, maxBytes = MAX_ASSET_BYTES) {
        requireInteger(maxBytes, 'decoded asset byte limit', 0, Number.MAX_SAFE_INTEGER);
        const size = decodedBase64Bytes(data);
        if (size > maxBytes) fail('asset exceeds the decoded asset limit');
        return size;
    }

    function applyMigrations(document, migrations = MIGRATIONS, targetVersion = SCHEMA_VERSION) {
        requireObject(document, 'document');
        if (!Number.isInteger(document.schemaVersion)) fail('schemaVersion must be an integer');
        requireObject(migrations, 'migrations');
        requireInteger(targetVersion, 'target schema version', 1, Number.MAX_SAFE_INTEGER);

        let current = document;
        while (current.schemaVersion < targetVersion) {
            const sourceVersion = current.schemaVersion;
            const migrate = migrations[sourceVersion];
            if (typeof migrate !== 'function') fail(`unsupported older project version ${sourceVersion}`);
            current = requireObject(migrate(current), `migration from version ${sourceVersion}`);
            if (!Number.isInteger(current.schemaVersion) || current.schemaVersion <= sourceVersion) {
                fail('migration did not advance schema version');
            }
            if (current.schemaVersion > targetVersion) fail('migration advanced beyond the target schema version');
        }
        return current;
    }

    function validateProject(document) {
        requireObject(document, 'document');
        if (document.format !== FORMAT) fail('format marker is missing or unsupported');
        if (!Number.isInteger(document.schemaVersion)) fail('schemaVersion must be an integer');
        if (document.schemaVersion > SCHEMA_VERSION) {
            fail(`newer project version ${document.schemaVersion}; this viewer supports version ${SCHEMA_VERSION}`);
        }
        if (document.schemaVersion < 1) fail(`unsupported older project version ${document.schemaVersion}`);

        const project = requireObject(document.project, 'project');
        requireString(project.name, 'project.name', true);
        requireString(project.savedAt, 'project.savedAt');
        if (!Number.isFinite(Date.parse(project.savedAt))) fail('project.savedAt must be a valid timestamp');
        requireString(project.activeProductId, 'project.activeProductId', true);

        const assets = requireObject(document.assets, 'assets');
        for (const [id, asset] of Object.entries(assets)) {
            requireString(id, 'asset id');
            requireObject(asset, `assets.${id}`);
            if (!RASTER_TYPES.has(asset.mimeType)) fail(`assets.${id}.mimeType is unsupported`);
            if (asset.encoding !== 'base64') fail(`assets.${id}.encoding must be base64`);
            try {
                assertDecodedAssetSize(asset.data);
            } catch (error) {
                if (/decoded asset limit/i.test(error.message)) fail(`assets.${id} exceeds the decoded asset limit`);
                throw error;
            }
        }

        const sourceIds = new Set();
        for (const source of requireArray(document.sources, 'sources')) {
            requireObject(source, 'source');
            requireString(source.id, 'source.id');
            if (sourceIds.has(source.id)) fail(`duplicate source id ${source.id}`);
            sourceIds.add(source.id);
            requireString(source.assetId, `source ${source.id} assetId`);
            if (!Object.prototype.hasOwnProperty.call(assets, source.assetId)) {
                fail(`source ${source.id} references missing asset ${source.assetId}`);
            }
            requireDimensions(source, `source ${source.id}`);

            const origin = requireObject(source.origin, `source ${source.id} origin`);
            if (!['catalog', 'upload'].includes(origin.kind)) fail(`source ${source.id} origin kind is unsupported`);
            for (const key of ['productId', 'variant', 'filename', 'url']) {
                requireString(origin[key], `source ${source.id} origin.${key}`, true);
            }
            if (Object.prototype.hasOwnProperty.call(source, 'preprocess') && typeof source.preprocess !== 'boolean') {
                fail(`source ${source.id} preprocess must be a boolean`);
            }

            if (source.render !== null) {
                const render = requireObject(source.render, `source ${source.id} render`);
                if (!RENDER_MODES.has(render.mode)) fail(`source ${source.id} render mode is unsupported`);
                const params = requireObject(render.traceParams, `source ${source.id} render.traceParams`);
                requireInteger(params.threshold, `source ${source.id} threshold`, 0, 255);
                requireInteger(params.turdsize, `source ${source.id} turdsize`, 0, 200);
                requireFinite(params.alphamax, `source ${source.id} alphamax`, 0, 1.33);
                requireFinite(params.opttolerance, `source ${source.id} opttolerance`, 0, 1);
                requireInteger(params.numColors, `source ${source.id} numColors`, 2, 16);
                requireColorState(render.color, `source ${source.id} render color`);
                requireString(render.svg, `source ${source.id} render.svg`);
                rejectUnsafeSvgMarkup(render.svg);
            }

            const detection = requireObject(source.detection, `source ${source.id} detection`);
            requireInteger(detection.minSize, `source ${source.id} detection.minSize`, 100, 5000);
            requireInteger(detection.gapTolerance, `source ${source.id} detection.gapTolerance`, 1, 40);
        }

        const objectIds = new Set();
        for (const object of requireArray(document.extractedObjects, 'extractedObjects')) {
            requireObject(object, 'extracted object');
            requireString(object.id, 'extracted object id');
            if (objectIds.has(object.id)) fail(`duplicate extracted object id ${object.id}`);
            objectIds.add(object.id);
            if (Object.prototype.hasOwnProperty.call(object, 'sourceId')) {
                requireString(object.sourceId, `object ${object.id} sourceId`, true);
                if (object.sourceId && !sourceIds.has(object.sourceId)) {
                    fail(`object ${object.id} references missing source ${object.sourceId}`);
                }
            }
            requireString(object.productId, `object ${object.id} productId`, true);
            requireString(object.label, `object ${object.id} label`);
            requireDimensions(object, `object ${object.id}`);
            requireDimensions(object, `object ${object.id} reference`, 'referenceWidth', 'referenceHeight');
            requireString(object.svg, `object ${object.id} svg`);
            rejectUnsafeSvgMarkup(object.svg);
            requireColorState(object.color, `object ${object.id} color`);
            requireCricutState(object.cricut, `object ${object.id} cricut`);
        }

        if (document.composer !== null) {
            const composer = requireObject(document.composer, 'composer');
            requireInteger(composer.width, 'composer.width', 1, MAX_DIMENSION);
            requireInteger(composer.height, 'composer.height', 1, MAX_DIMENSION);
            requireString(composer.productId, 'composer.productId', true);
            requireCricutState(composer.cricut, 'composer.cricut');
            const composerIds = new Set();
            for (const object of requireArray(composer.objects, 'composer.objects')) {
                requireObject(object, 'composer object');
                requireString(object.id, 'composer object id');
                if (composerIds.has(object.id)) fail(`duplicate composer object id ${object.id}`);
                composerIds.add(object.id);
                if (Object.prototype.hasOwnProperty.call(object, 'sourceObjectId')) {
                    requireString(object.sourceObjectId, `composer object ${object.id} sourceObjectId`, true);
                    if (object.sourceObjectId && !objectIds.has(object.sourceObjectId)) {
                        fail(`composer object ${object.id} references missing extracted object ${object.sourceObjectId}`);
                    }
                }
                requireDimensions(object, `composer object ${object.id}`, 'sourceWidth', 'sourceHeight');
                requireFinite(object.x, `composer object ${object.id} x`, -MAX_TRANSFORM, MAX_TRANSFORM);
                requireFinite(object.y, `composer object ${object.id} y`, -MAX_TRANSFORM, MAX_TRANSFORM);
                requireFinite(object.scale, `composer object ${object.id} scale`, 0.000001, MAX_TRANSFORM);
                requireFinite(object.baseScale, `composer object ${object.id} baseScale`, 0.000001, MAX_TRANSFORM);
                requireFinite(object.rotation, `composer object ${object.id} rotation`, -MAX_TRANSFORM, MAX_TRANSFORM);
                requireString(object.svg, `composer object ${object.id} svg`);
                rejectUnsafeSvgMarkup(object.svg);
            }
        }
        return document;
    }

    function rejectUnsafeSvgAttribute(name, value) {
        const normalizedName = name.toLowerCase();
        const localName = normalizedName.includes(':')
            ? normalizedName.slice(normalizedName.lastIndexOf(':') + 1)
            : normalizedName;

        if (/^on[a-z]/.test(localName) || localName === 'href' || localName === 'style') {
            fail('unsafe SVG markup');
        }
        if (normalizedName === 'xmlns') {
            if (value !== 'http://www.w3.org/2000/svg') fail('unsafe SVG markup');
            return;
        }

        const compactValue = value.replace(/[\u0000-\u0020]+/g, '');
        if (normalizedName.startsWith('xmlns:') ||
            /&(?:#\d+|#x[0-9a-f]+|[a-z][\w.-]*);/i.test(value) ||
            /\\|\/\*/.test(value) ||
            /[a-z][a-z0-9+.-]*:/i.test(compactValue) ||
            /(^|[("'=])\/\//.test(compactValue) ||
            /(?:url|expression)\s*\(/i.test(compactValue)) {
            fail('unsafe SVG markup');
        }
    }

    function rejectUnsafeSvgMarkup(svgText) {
        requireString(svgText, 'svg');
        if (/<\s*\/?\s*(?:[A-Za-z_][\w.-]*:)?(?:script|foreignObject|iframe|object|embed|image|use)\b/i.test(svgText) ||
            /<!\s*(?:DOCTYPE|ENTITY)\b/i.test(svgText)) {
            fail('unsafe SVG markup');
        }
        const attributePattern = /\s((?:[A-Za-z_][\w.-]*:)?[A-Za-z_][\w.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g;
        let match;
        while ((match = attributePattern.exec(svgText))) {
            const name = match[1].toLowerCase();
            const value = match[2] ?? match[3] ?? match[4] ?? '';
            rejectUnsafeSvgAttribute(name, value);
        }
        return svgText;
    }

    function sanitizeSvg(svgText, DOMParserCtor) {
        rejectUnsafeSvgMarkup(svgText);
        const Parser = DOMParserCtor || (root && root.DOMParser);
        if (!Parser) fail('SVG parser is unavailable');
        const parsed = new Parser().parseFromString(svgText, 'image/svg+xml');
        if (parsed.querySelector('parsererror') || !parsed.documentElement || parsed.documentElement.localName !== 'svg') {
            fail('SVG is malformed');
        }
        for (const element of Array.from(parsed.querySelectorAll('*'))) {
            if (!SVG_ELEMENTS.has(element.localName)) fail(`SVG element ${element.localName} is unsupported`);
            for (const attribute of Array.from(element.attributes)) {
                rejectUnsafeSvgAttribute(attribute.name, attribute.value);
                if (!SVG_ATTRIBUTES.has(attribute.name)) element.removeAttribute(attribute.name);
            }
        }
        const Serializer = root && root.XMLSerializer;
        if (!Serializer) fail('SVG serializer is unavailable');
        return new Serializer().serializeToString(parsed.documentElement);
    }

    // Returns a NEW document with only the SVG-bearing branches rewritten
    // through `sanitizer`, leaving the input untouched. Deliberately avoids
    // structuredClone(document): `assets` holds the base64 rasters (up to the
    // ~100 MiB project limit), and deep-cloning them just to rewrite a few SVG
    // strings would double peak memory. Non-SVG branches (assets, project) are
    // shared by reference; every object whose `svg` is reassigned is shallow-
    // copied first, so the original document is never mutated.
    function sanitizeDocumentSvgs(document, sanitizer) {
        const clean = { ...document };
        clean.sources = document.sources.map(source => (
            source.render
                ? { ...source, render: { ...source.render, svg: sanitizer(source.render.svg) } }
                : source
        ));
        clean.extractedObjects = document.extractedObjects.map(object => (
            { ...object, svg: sanitizer(object.svg) }
        ));
        if (document.composer) {
            clean.composer = {
                ...document.composer,
                objects: document.composer.objects.map(object => ({ ...object, svg: sanitizer(object.svg) }))
            };
        }
        return clean;
    }

    function parseProjectText(text, options = {}) {
        assertProjectTextSize(text);
        let parsed;
        try { parsed = JSON.parse(text); } catch (error) { fail(`JSON could not be parsed: ${error.message}`); }
        parsed = applyMigrations(parsed);
        validateProject(parsed);
        const sanitizer = options.sanitizeSvg || (value => sanitizeSvg(value, options.DOMParser));
        return sanitizeDocumentSvgs(parsed, value => {
            rejectUnsafeSvgMarkup(value);
            const sanitized = requireString(sanitizer(value), 'sanitized svg');
            return rejectUnsafeSvgMarkup(sanitized);
        });
    }

    function serializeProject(document) {
        validateProject(document);
        const text = JSON.stringify(document);
        assertProjectTextSize(text);
        return text;
    }

    async function encodeBlob(blob) {
        if (!(blob instanceof Blob)) fail('asset must be a Blob');
        if (!RASTER_TYPES.has(blob.type)) fail(`asset MIME type ${blob.type || '(empty)'} is unsupported`);
        if (blob.size > MAX_ASSET_BYTES) fail('asset exceeds the decoded asset limit');
        const bytes = new Uint8Array(await blob.arrayBuffer());
        let binary = '';
        for (let offset = 0; offset < bytes.length; offset += 0x8000) {
            binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
        }
        return { mimeType: blob.type, encoding: 'base64', data: btoaCompat(binary) };
    }

    function decodeAsset(asset) {
        requireObject(asset, 'asset');
        if (!RASTER_TYPES.has(asset.mimeType)) fail('asset is invalid');
        if (asset.encoding !== 'base64') fail('asset encoding must be base64');
        assertDecodedAssetSize(asset.data);
        const binary = atobCompat(asset.data);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
        return new Blob([bytes], { type: asset.mimeType });
    }

    function createEmptyProject(metadata = {}) {
        return {
            format: FORMAT,
            schemaVersion: SCHEMA_VERSION,
            project: {
                name: metadata.name || '',
                savedAt: metadata.savedAt || new Date().toISOString(),
                activeProductId: metadata.activeProductId || ''
            },
            assets: {},
            sources: [],
            extractedObjects: [],
            composer: null
        };
    }

    return {
        FORMAT, SCHEMA_VERSION, MIME_TYPE, MAX_PROJECT_BYTES,
        MAX_DIMENSION, MAX_PIXELS,
        validateProject, serializeProject, parseProjectText, sanitizeSvg,
        rejectUnsafeSvgMarkup, encodeBlob, decodeAsset, createEmptyProject,
        assertProjectTextSize, assertDecodedAssetSize, applyMigrations
    };
}));
