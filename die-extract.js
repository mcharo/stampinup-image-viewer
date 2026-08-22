/**
 * die-extract.js
 * ---------------------------------------------------------------------------
 * Extracts individual glyph silhouettes from a photograph of a sheet of
 * metal craft dies (Sizzix / Stampin' Up Framelits and similar) and emits
 * clean 1-bit masks ready to hand to a vectorizer.
 *
 * Pure JS with zero DOM dependency: takes {data:Uint8ClampedArray, width,
 * height} in RGBA order -- exactly an ImageData -- and returns plain objects.
 * That makes it unit-testable in node and reusable in a worker. The canvas
 * side (ImageData in, mask to Image out) lives in index.html.
 *
 * Loaded on demand rather than at page load, so the experimental extraction
 * path is absent from the app's baseline. Wrapped in the same UMD form as
 * project-file.js, NOT as an ES module, because module loads are blocked by
 * CORS when index.html is opened from file://.
 *
 * Validated against Large-Letters-Framelits-Dies-Stampin-Up-141712-768x858.png:
 * yields exactly 37 components (26 letters + & ? ! + 7 symbols) with all
 * counters (A B D O P Q R) preserved as holes.
 */
(function (root, factory) {
    const api = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.StampinUpDieExtract = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const DEFAULTS = {
        channel: 'auto',      // 'warmth' | 'luma' | 'auto'  -- see pickChannel()
        autoCrop: true,       // strip dark banner bands (title bar, footer)
        inset: 4,             // px trimmed from every edge after autoCrop
        flattenRadius: 0,     // >0 enables illumination flattening (see below)
        scale: 3,             // supersample factor before thresholding
        morphRadius: 1,       // in SOURCE pixels; scaled internally
        minArea: 350,         // in SOURCE pixels; components smaller are discarded
        minHoleArea: 40,      // in SOURCE pixels; holes smaller are filled (die nicks)
        mirror: true,         // dies are photographed reversed; flip to read normally
        mergeParts: true,     // reunite multi-part glyphs such as ! and ?
        pad: 6,               // px of white margin around each exported glyph
        rowTolerance: 0.55    // fraction of median glyph height for row grouping
    };

    const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

    /* --- 1. channel extraction --------------------------------------------
     * The whole trick. Bare metal is spectrally NEUTRAL (R==G==B); the cream
     * wood/paper backdrop is WARM (R>G>B). So "warmth" separates them regardless
     * of how the light falls, whereas brightness does not: the metal catches
     * specular highlights that are brighter than the background, and the wood
     * grain is darker than the metal in shadow. Measured on the reference image:
     *
     *            warmth      luminance
     *   backdrop  18..21       139..250   <- overlapping
     *   metal      0..2        156..245   <- overlapping
     *   Otsu separability  0.948          0.796
     *   blobs found          39            161
     */
    function warmthField(img) {
        const { data } = img;
        const f = new Float32Array(img.width * img.height);
        for (let i = 0, p = 0; i < f.length; i++, p += 4) {
            f[i] = (data[p] + data[p + 1]) * 0.5 - data[p + 2];
        }
        return f;
    }

    function lumaField(img) {
        const { data } = img;
        const f = new Float32Array(img.width * img.height);
        for (let i = 0, p = 0; i < f.length; i++, p += 4) {
            f[i] = 0.299 * data[p] + 0.587 * data[p + 1] + 0.114 * data[p + 2];
        }
        return f;
    }

    /* --- 2. normalisation + Otsu ------------------------------------------ */

    /** Rescale to 0..255 using 1st/99th percentiles so outliers don't crush it. */
    function normalize(field) {
        const s = Float32Array.from(field).sort();
        const lo = s[Math.floor(s.length * 0.01)];
        const hi = s[Math.floor(s.length * 0.99)];
        const span = Math.max(hi - lo, 1e-6);
        const out = new Uint8Array(field.length);
        for (let i = 0; i < field.length; i++) {
            out[i] = Math.max(0, Math.min(255, ((field[i] - lo) / span) * 255)) | 0;
        }
        return out;
    }

    function otsu(u8) {
        const hist = new Float64Array(256);
        for (let i = 0; i < u8.length; i++) hist[u8[i]]++;
        for (let i = 0; i < 256; i++) hist[i] /= u8.length;
        let mu = 0;
        for (let i = 0; i < 256; i++) mu += i * hist[i];
        let w0 = 0, sum0 = 0, best = -1, t = 128, bcvBest = 0;
        for (let i = 0; i < 256; i++) {
            w0 += hist[i]; sum0 += i * hist[i];
            const w1 = 1 - w0;
            if (w0 <= 0 || w1 <= 0) continue;
            const m0 = sum0 / w0, m1 = (mu - sum0) / w1;
            const bcv = w0 * w1 * (m0 - m1) * (m0 - m1);
            if (bcv > best) { best = bcv; t = i; bcvBest = bcv; }
        }
        let tot = 0;
        for (let i = 0; i < 256; i++) tot += (i - mu) * (i - mu) * hist[i];
        return { threshold: t, separability: tot > 0 ? bcvBest / tot : 0 };
    }

    /* Brightness has to win by this much before it is preferred. Otsu
     * separability is a poor judge of a near tie: on a metal-die photo shot
     * against a white backdrop the two channels score within 0.01 of each
     * other, yet warmth segments the sheet cleanly while brightness shatters
     * the thin steel outlines into fragments. Warmth is the structurally
     * correct cue whenever the subject is metal on paper, so only defer to
     * brightness when brightness is decisively better — which is what happens
     * on a grayscale or already-binarised source, where warmth is constant and
     * scores 0 against brightness's ~1. */
    const LUMA_MARGIN = 0.05;

    /** Try both channels, preferring warmth unless brightness clearly wins. */
    function pickChannel(img) {
        const w = normalize(warmthField(img));
        const l = normalize(lumaField(img));
        const sw = otsu(w).separability, sl = otsu(l).separability;
        return sl > sw + LUMA_MARGIN
            ? { name: 'luma', u8: l, separability: sl, rival: sw }
            : { name: 'warmth', u8: w, separability: sw, rival: sl };
    }

    /* --- 3. optional illumination flattening ------------------------------
     * Not needed for the reference image. Turn on (flattenRadius ~ 1/8 of the
     * short side) if a photo has heavy vignetting or a raking light gradient. */
    function flatten(u8, width, height, radius) {
        const bg = boxBlur(u8, width, height, radius);
        const out = new Uint8Array(u8.length);
        for (let i = 0; i < u8.length; i++) {
            out[i] = Math.max(0, Math.min(255, 128 + u8[i] - bg[i]));
        }
        return out;
    }

    function boxBlur(src, width, height, r) {
        const tmp = new Float32Array(src.length), out = new Uint8Array(src.length);
        for (let y = 0; y < height; y++) {
            let acc = 0;
            const row = y * width;
            for (let x = -r; x <= r; x++) acc += src[row + clamp(x, 0, width - 1)];
            for (let x = 0; x < width; x++) {
                tmp[row + x] = acc / (2 * r + 1);
                acc -= src[row + clamp(x - r, 0, width - 1)];
                acc += src[row + clamp(x + r + 1, 0, width - 1)];
            }
        }
        for (let x = 0; x < width; x++) {
            let acc = 0;
            for (let y = -r; y <= r; y++) acc += tmp[clamp(y, 0, height - 1) * width + x];
            for (let y = 0; y < height; y++) {
                out[y * width + x] = acc / (2 * r + 1);
                acc -= tmp[clamp(y - r, 0, height - 1) * width + x];
                acc += tmp[clamp(y + r + 1, 0, height - 1) * width + x];
            }
        }
        return out;
    }

    /* --- 4. supersampling -------------------------------------------------
     * Upsample the CONTINUOUS field, then threshold. Thresholding first and
     * upsampling the bitmap gives you staircase edges that the vectorizer will
     * faithfully reproduce as staircases. Doing it in this order recovers
     * sub-pixel edge placement from the anti-aliasing already in the photo, which
     * matters a lot here: glyphs are only ~150 px tall in the source. */
    function resampleBicubic(src, sw, sh, scale) {
        const dw = Math.round(sw * scale), dh = Math.round(sh * scale);
        const tmp = new Float32Array(dw * sh);
        const cr = (a, b, c, d, t) => {
            const t2 = t * t, t3 = t2 * t;
            return 0.5 * ((2 * b) + (-a + c) * t +
                (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3);
        };
        for (let y = 0; y < sh; y++) {
            for (let x = 0; x < dw; x++) {
                const fx = (x + 0.5) / scale - 0.5, ix = Math.floor(fx), t = fx - ix;
                const r = y * sw;
                tmp[y * dw + x] = cr(
                    src[r + clamp(ix - 1, 0, sw - 1)], src[r + clamp(ix, 0, sw - 1)],
                    src[r + clamp(ix + 1, 0, sw - 1)], src[r + clamp(ix + 2, 0, sw - 1)], t);
            }
        }
        const out = new Float32Array(dw * dh);
        for (let y = 0; y < dh; y++) {
            const fy = (y + 0.5) / scale - 0.5, iy = Math.floor(fy), t = fy - iy;
            for (let x = 0; x < dw; x++) {
                out[y * dw + x] = cr(
                    tmp[clamp(iy - 1, 0, sh - 1) * dw + x], tmp[clamp(iy, 0, sh - 1) * dw + x],
                    tmp[clamp(iy + 1, 0, sh - 1) * dw + x], tmp[clamp(iy + 2, 0, sh - 1) * dw + x], t);
            }
        }
        return { data: out, width: dw, height: dh };
    }

    /* --- 5. binary morphology (separable square SE) ----------------------- */
    function morph(bin, width, height, r, op) {
        // op: 'erode' | 'dilate'
        const pick = op === 'erode' ? Math.min : Math.max;
        const tmp = new Uint8Array(bin.length), out = new Uint8Array(bin.length);
        for (let y = 0; y < height; y++) {
            for (let x = 0; x < width; x++) {
                let v = bin[y * width + x];
                for (let k = -r; k <= r; k++) v = pick(v, bin[y * width + clamp(x + k, 0, width - 1)]);
                tmp[y * width + x] = v;
            }
        }
        for (let y = 0; y < height; y++) {
            for (let x = 0; x < width; x++) {
                let v = tmp[y * width + x];
                for (let k = -r; k <= r; k++) v = pick(v, tmp[clamp(y + k, 0, height - 1) * width + x]);
                out[y * width + x] = v;
            }
        }
        return out;
    }
    const open = (b, w, h, r) => morph(morph(b, w, h, r, 'erode'), w, h, r, 'dilate');
    const close = (b, w, h, r) => morph(morph(b, w, h, r, 'dilate'), w, h, r, 'erode');

    /* --- 6. connected components (8-connected, iterative flood fill) ------ */
    function connectedComponents(bin, width, height, minArea) {
        const labels = new Int32Array(bin.length).fill(0);
        const stack = new Int32Array(bin.length);
        const comps = [];
        let next = 0;
        for (let seed = 0; seed < bin.length; seed++) {
            if (!bin[seed] || labels[seed]) continue;
            next++;
            let sp = 0, area = 0;
            let minX = width, minY = height, maxX = -1, maxY = -1;
            stack[sp++] = seed; labels[seed] = next;
            while (sp > 0) {
                const p = stack[--sp];
                const y = (p / width) | 0, x = p - y * width;
                area++;
                if (x < minX) minX = x;
                if (x > maxX) maxX = x;
                if (y < minY) minY = y;
                if (y > maxY) maxY = y;
                for (let dy = -1; dy <= 1; dy++) {
                    const ny = y + dy;
                    if (ny < 0 || ny >= height) continue;
                    for (let dx = -1; dx <= 1; dx++) {
                        const nx = x + dx;
                        if (nx < 0 || nx >= width) continue;
                        const q = ny * width + nx;
                        if (bin[q] && !labels[q]) { labels[q] = next; stack[sp++] = q; }
                    }
                }
            }
            comps.push({ label: next, area, x: minX, y: minY, w: maxX - minX + 1, h: maxY - minY + 1 });
        }
        return { labels, comps: comps.filter(c => c.area >= minArea) };
    }

    /* --- 7. grouping ------------------------------------------------------ */

    /**
     * Reunite glyphs the die set splits into pieces -- '!' and '?' ship as a
     * stroke plus a detached dot. The rule has to be tight or it will happily
     * weld each letter to the one below it in the next row, since those also sit
     * in the same column with a small vertical gap. Three conditions together:
     * the pieces overlap horizontally, the gap between them is a small fraction
     * of a glyph height, and the UNION is still about one glyph tall.
     */
    function mergeParts(comps) {
        const heights = comps.map(c => c.h).sort((a, b) => a - b);
        const medH = heights[heights.length >> 1] || 1;
        const out = comps.map(c => ({ ...c, parts: [c.label] }));
        let changed = true;
        while (changed) {
            changed = false;
            outer:
            for (let i = 0; i < out.length; i++) {
                for (let j = i + 1; j < out.length; j++) {
                    const a = out[i], b = out[j];
                    const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
                    const narrow = Math.min(a.w, b.w);
                    const gapY = Math.max(a.y, b.y) - Math.min(a.y + a.h, b.y + b.h);
                    const unionH = Math.max(a.y + a.h, b.y + b.h) - Math.min(a.y, b.y);
                    if (ox > narrow * 0.5 && gapY >= 0 && gapY < medH * 0.30 && unionH < medH * 1.30) {
                        const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
                        out[i] = {
                            label: a.label, area: a.area + b.area, x, y,
                            w: Math.max(a.x + a.w, b.x + b.w) - x,
                            h: Math.max(a.y + a.h, b.y + b.h) - y,
                            parts: a.parts.concat(b.parts)
                        };
                        out.splice(j, 1); changed = true; break outer;
                    }
                }
            }
        }
        return out;
    }

    /** Reading order: group into rows by vertical position, then sort left-to-right. */
    function sortReadingOrder(comps, tolerance) {
        if (!comps.length) return [];
        const heights = comps.map(c => c.h).sort((a, b) => a - b);
        const med = heights[heights.length >> 1];
        const tol = med * tolerance;
        const byY = [...comps].sort((a, b) => a.y - b.y);
        const rows = [[byY[0]]];
        for (let i = 1; i < byY.length; i++) {
            const row = rows[rows.length - 1];
            if (byY[i].y - row[0].y < tol) row.push(byY[i]);
            else rows.push([byY[i]]);
        }
        rows.forEach(r => r.sort((a, b) => a.x - b.x));
        return rows;
    }

    /* --- 8. hole cleanup --------------------------------------------------
     * Framelits have small locating nicks in the cutting rim that survive as
     * pinholes. Fill background blobs that don't touch the tile border and are
     * smaller than minHoleArea; real counters are orders of magnitude larger. */
    function fillSmallHoles(bin, width, height, minHoleArea) {
        const seen = new Uint8Array(bin.length);
        const stack = new Int32Array(bin.length);
        for (let seed = 0; seed < bin.length; seed++) {
            if (bin[seed] || seen[seed]) continue;
            let sp = 0, touches = false;
            const pixels = [];
            stack[sp++] = seed; seen[seed] = 1;
            while (sp > 0) {
                const p = stack[--sp];
                const y = (p / width) | 0, x = p - y * width;
                pixels.push(p);
                if (x === 0 || y === 0 || x === width - 1 || y === height - 1) touches = true;
                const nb = [[1, 0], [-1, 0], [0, 1], [0, -1]];
                for (const [dx, dy] of nb) {
                    const nx = x + dx, ny = y + dy;
                    if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
                    const q = ny * width + nx;
                    if (!bin[q] && !seen[q]) { seen[q] = 1; stack[sp++] = q; }
                }
            }
            if (!touches && pixels.length < minHoleArea) for (const p of pixels) bin[p] = 1;
        }
        return bin;
    }

    /* --- 9. auto-crop ----------------------------------------------------- */
    /** Strips the dark title banner and footer by finding the tallest bright run. */
    function autoCropBounds(img, inset) {
        const { data, width, height } = img;
        const rowMean = new Float32Array(height);
        for (let y = 0; y < height; y++) {
            let s = 0;
            for (let x = 0; x < width; x++) {
                const p = (y * width + x) * 4;
                s += (data[p] + data[p + 1] + data[p + 2]) / 3;
            }
            rowMean[y] = s / width;
        }
        const bright = rowMean.map(v => (v > 110 ? 1 : 0));
        let best = { start: 0, len: 0 }, run = 0;
        for (let y = 0; y < height; y++) {
            if (bright[y]) { run++; if (run > best.len) best = { start: y - run + 1, len: run }; }
            else run = 0;
        }
        return {
            x: inset, y: best.start + inset,
            w: width - inset * 2, h: best.len - inset * 2
        };
    }

    function cropImage(img, box) {
        const out = new Uint8ClampedArray(box.w * box.h * 4);
        for (let y = 0; y < box.h; y++) {
            const src = ((y + box.y) * img.width + box.x) * 4;
            out.set(img.data.subarray(src, src + box.w * 4), y * box.w * 4);
        }
        return { data: out, width: box.w, height: box.h };
    }

    function mirrorImage(img) {
        const { data, width, height } = img;
        const out = new Uint8ClampedArray(data.length);
        for (let y = 0; y < height; y++) {
            for (let x = 0; x < width; x++) {
                const s = (y * width + x) * 4, d = (y * width + (width - 1 - x)) * 4;
                out[d] = data[s]; out[d + 1] = data[s + 1];
                out[d + 2] = data[s + 2]; out[d + 3] = data[s + 3];
            }
        }
        return { data: out, width, height };
    }

    /* --- 10. the pipeline ------------------------------------------------- */
    /**
     * @param {{data:Uint8ClampedArray,width:number,height:number}} imageData
     * @returns {{glyphs:Array, rows:Array, sheet:{mask:Uint8Array,width,height}, meta:Object}}
     *   Each glyph: { row, col, index, width, height, mask:Uint8Array (1 = ink),
     *                 source:{x,y,w,h} in supersampled coords }
     */
    function extractDies(imageData, options = {}) {
        const o = { ...DEFAULTS, ...options };

        let img = imageData;
        const box = o.autoCrop
            ? autoCropBounds(img, o.inset)
            : { x: o.inset, y: o.inset, w: img.width - o.inset * 2, h: img.height - o.inset * 2 };
        img = cropImage(img, box);
        if (o.mirror) img = mirrorImage(img);

        let chosen;
        if (o.channel === 'warmth') chosen = { name: 'warmth', u8: normalize(warmthField(img)) };
        else if (o.channel === 'luma') chosen = { name: 'luma', u8: normalize(lumaField(img)) };
        else chosen = pickChannel(img);

        let u8 = chosen.u8;
        if (o.flattenRadius > 0) u8 = flatten(u8, img.width, img.height, o.flattenRadius);

        const { threshold, separability } = otsu(u8);

        const field = Float32Array.from(u8);
        const up = resampleBicubic(field, img.width, img.height, o.scale);

        const bin = new Uint8Array(up.data.length);
        for (let i = 0; i < bin.length; i++) bin[i] = up.data[i] < threshold ? 1 : 0;

        const r = Math.max(1, Math.round(o.morphRadius * o.scale));
        const clean = close(open(bin, up.width, up.height, r), up.width, up.height, r);

        const s2 = o.scale * o.scale;
        const cc = connectedComponents(clean, up.width, up.height, o.minArea * s2);
        const { labels } = cc;
        // Drop artefacts of the crop itself: the shadow line along the edge of the
        // photographed sheet survives thresholding as a long thin sliver. No real
        // glyph spans most of the sheet or is 8x longer than it is wide.
        const comps = cc.comps.filter(c =>
            c.w < up.width * 0.9 && c.h < up.height * 0.9 &&
            Math.max(c.w, c.h) / Math.max(1, Math.min(c.w, c.h)) < 8);
        const grouped = o.mergeParts ? mergeParts(comps) : comps.map(c => ({ ...c, parts: [c.label] }));
        const rows = sortReadingOrder(grouped, o.rowTolerance);

        const pad = o.pad * o.scale;
        const glyphs = [];
        let index = 0;
        rows.forEach((row, ri) => row.forEach((c, ci) => {
            const W = c.w + pad * 2, H = c.h + pad * 2;
            const m = new Uint8Array(W * H);
            const set = new Set(c.parts);
            for (let y = 0; y < c.h; y++) {
                for (let x = 0; x < c.w; x++) {
                    const L = labels[(c.y + y) * up.width + (c.x + x)];
                    if (set.has(L)) m[(y + pad) * W + (x + pad)] = 1;
                }
            }
            fillSmallHoles(m, W, H, o.minHoleArea * s2);
            glyphs.push({
                index: index++, row: ri, col: ci, width: W, height: H, mask: m,
                source: { x: c.x, y: c.y, w: c.w, h: c.h }, area: c.area
            });
        }));

        return {
            glyphs, rows,
            sheet: { mask: clean, width: up.width, height: up.height },
            meta: {
                channel: chosen.name, threshold, separability, crop: box,
                scale: o.scale, componentCount: glyphs.length,
                rowCounts: rows.map(row => row.length)
            }
        };
    }

    return {
        DEFAULTS,
        warmthField, lumaField, normalize, otsu, pickChannel,
        flatten, resampleBicubic, morph, open, close,
        connectedComponents, mergeParts, sortReadingOrder, fillSmallHoles,
        autoCropBounds, cropImage, mirrorImage, extractDies
    };
}));
