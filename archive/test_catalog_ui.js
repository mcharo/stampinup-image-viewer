const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const {
  PAGE_SIZE,
  buildFacetCounts,
  createCachedLoader,
  createLatestRequestGate,
  createShardLoader,
  filterAndSort,
  nextBatchEnd,
  versionedUrl,
  viewerUrl,
} = require('./catalog-ui.js');

const ARCHIVE_DIR = __dirname;
const INDEX_HTML = fs.readFileSync(path.join(ARCHIVE_DIR, 'index.html'), 'utf8');
const INLINE_SCRIPT_MATCHES = [...INDEX_HTML.matchAll(
  /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g,
)];
const PAGE_SCRIPT = INLINE_SCRIPT_MATCHES.at(-1)[1];

const PRODUCTS = [
  {
    product_id: '167002',
    language: 'english',
    display_name: 'Woolly Friends',
    name: 'Woolly Friends',
    category: 'Stamps',
    status: 'Current',
    tags: ['animals', 'sheep'],
    image_count: 2,
  },
  {
    product_id: '167001',
    language: 'french',
    display_name: 'Fleurs magnifiques',
    category: 'Bundles',
    status: 'Retired',
    tags: ['flowers'],
    image_count: 1,
  },
  {
    product_id: '168010',
    language: 'english',
    display_name: 'Simple Shapes',
    category: 'Stamps',
    status: 'Unknown',
    tags: [],
    image_count: 1,
  },
];

function state(overrides = {}) {
  return {
    language: 'all',
    category: 'all',
    status: 'all',
    query: '',
    sortOrder: 'desc',
    ...overrides,
  };
}

test('buildFacetCounts creates global language, category, and status counts', () => {
  assert.deepEqual(buildFacetCounts(PRODUCTS), {
    languages: { all: 3, english: 2, french: 1 },
    categories: { all: 3, Bundles: 1, Stamps: 2 },
    statuses: { all: 3, current: 1, retired: 1, unknown: 1 },
  });
});

test('filterAndSort searches summary fields and applies facets and numeric sort', () => {
  assert.deepEqual(
    filterAndSort(PRODUCTS, state({ query: '"woolly friends" sheep' })).map(product => product.product_id),
    ['167002'],
  );
  assert.deepEqual(
    filterAndSort(PRODUCTS, state({
      language: 'english',
      category: 'Stamps',
      status: 'unknown',
      sortOrder: 'asc',
    })).map(product => product.product_id),
    ['168010'],
  );
  assert.deepEqual(
    filterAndSort(PRODUCTS, state({ sortOrder: 'asc' })).map(product => product.product_id),
    ['167001', '167002', '168010'],
  );
  assert.deepEqual(PRODUCTS.map(product => product.product_id), ['167002', '167001', '168010']);
});

test('filterAndSort combines deep search text with summary text', () => {
  const deepSearch = {
    '167001': 'texte caché aquarelle',
    '167002': 'outline image',
  };

  assert.deepEqual(
    filterAndSort(PRODUCTS, state({ query: 'aquarelle fleurs' }), deepSearch)
      .map(product => product.product_id),
    ['167001'],
  );
});

test('nextBatchEnd caps each append at 96 without exceeding the result count', () => {
  assert.equal(PAGE_SIZE, 96);
  assert.equal(nextBatchEnd(0, 250), 96);
  assert.equal(nextBatchEnd(96, 250), 192);
  assert.equal(nextBatchEnd(192, 250), 250);
  assert.equal(nextBatchEnd(250, 250), 250);
});

test('createCachedLoader starts one request and reuses its Promise', async () => {
  let calls = 0;
  let resolveRequest;
  const loader = createCachedLoader(() => {
    calls += 1;
    return new Promise(resolve => {
      resolveRequest = resolve;
    });
  });

  const first = loader();
  const second = loader();

  assert.strictEqual(first, second);
  assert.equal(calls, 1);
  resolveRequest({ loaded: true });
  assert.deepEqual(await first, { loaded: true });
  assert.strictEqual(loader(), first);
  assert.equal(calls, 1);
});

test('createCachedLoader evicts a rejected Promise and caches the retry', async () => {
  let calls = 0;
  const loader = createCachedLoader(() => {
    calls += 1;
    if (calls === 1) return Promise.reject(new Error('network failed'));
    return Promise.resolve({ loaded: true });
  });

  const failed = loader();
  assert.strictEqual(loader(), failed);
  await assert.rejects(failed, /network failed/);

  const retry = loader();
  assert.notStrictEqual(retry, failed);
  assert.deepEqual(await retry, { loaded: true });
  assert.strictEqual(loader(), retry);
  assert.equal(calls, 2);
});

test('createShardLoader deduplicates shard requests and retries failures', async () => {
  const calls = [];
  let rejectFirst;
  const loader = createShardLoader(shard => {
    calls.push(shard);
    if (calls.length === 1) {
      return new Promise((resolve, reject) => {
        rejectFirst = reject;
      });
    }
    return Promise.resolve({ shard });
  });

  const first = loader.load('167');
  assert.strictEqual(loader.load('167'), first);
  assert.deepEqual(calls, ['167']);

  rejectFirst(new Error('network failed'));
  await assert.rejects(first, /network failed/);

  assert.deepEqual(await loader.load('167'), { shard: '167' });
  assert.deepEqual(calls, ['167', '167']);
  assert.strictEqual(loader.load('167'), loader.load('167'));
});

test('latest request gate prevents stale async search and detail results from applying', async () => {
  const gate = createLatestRequestGate();
  const applied = [];
  let resolveFirst;
  let resolveSecond;

  const firstToken = gate.begin();
  const first = new Promise(resolve => {
    resolveFirst = resolve;
  }).then(value => {
    if (gate.isCurrent(firstToken)) applied.push(value);
  });

  const secondToken = gate.begin();
  const second = new Promise(resolve => {
    resolveSecond = resolve;
  }).then(value => {
    if (gate.isCurrent(secondToken)) applied.push(value);
  });

  resolveFirst('stale');
  resolveSecond('current');
  await Promise.all([first, second]);

  assert.deepEqual(applied, ['current']);
  gate.invalidate();
  assert.equal(gate.isCurrent(secondToken), false);
});

test('versionedUrl appends an encoded manifest version', () => {
  assert.equal(
    versionedUrl('catalog-summary.json', '2026-07-20T20:00:00Z'),
    'catalog-summary.json?v=2026-07-20T20%3A00%3A00Z',
  );
  assert.equal(versionedUrl('catalog.json?kind=search', 'v 1'), 'catalog.json?kind=search&v=v%201');
});

test('viewerUrl points at the parent viewer with an encoded product id', () => {
  assert.equal(viewerUrl('167002'), '../index.html?id=167002');
  assert.equal(viewerUrl('150691 o02'), '../index.html?id=150691%20o02');
});

class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  _classes() {
    return this.element.className.split(/\s+/).filter(Boolean);
  }

  add(...names) {
    this.element.className = [...new Set([...this._classes(), ...names])].join(' ');
  }

  remove(...names) {
    const removed = new Set(names);
    this.element.className = this._classes().filter(name => !removed.has(name)).join(' ');
  }

  contains(name) {
    return this._classes().includes(name);
  }
}

class FakeElement {
  constructor(tagName = 'div', { fragment = false } = {}) {
    this.tagName = tagName.toUpperCase();
    this.isFragment = fragment;
    this.children = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.attributes = new Map();
    this.className = '';
    this.classList = new FakeClassList(this);
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.type = '';
    this.src = '';
    this.alt = '';
    this.href = '';
    this.target = '';
    this.rel = '';
    this.loading = '';
    this.decoding = '';
    this._textContent = '';
    this._innerHTML = '';
  }

  appendChild(child) {
    if (child.isFragment) {
      for (const fragmentChild of [...child.children]) this.appendChild(fragmentChild);
      child.children = [];
      return child;
    }
    this.children.push(child);
    child.parentNode = this;
    return child;
  }

  append(...children) {
    for (const child of children) {
      this.appendChild(typeof child === 'string' ? new FakeTextNode(child) : child);
    }
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  dispatch(type, extra = {}) {
    const event = { target: this, ...extra };
    for (const listener of this.listeners.get(type) || []) listener(event);
  }

  click() {
    this.dispatch('click');
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  set textContent(value) {
    this._textContent = String(value ?? '');
    this._innerHTML = '';
    this.children = [];
  }

  get textContent() {
    return this._textContent + this.children.map(child => child.textContent).join('');
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this._textContent = '';
    this.children = [];
  }

  get innerHTML() {
    return this._innerHTML;
  }
}

class FakeTextNode extends FakeElement {
  constructor(text) {
    super('#text');
    this._textContent = String(text);
  }
}

const PAGE_ELEMENT_IDS = [
  'total-count',
  'search',
  'search-status',
  'filters',
  'status-toggle',
  'category-filter',
  'sort',
  'result-count',
  'grid',
  'grid-footer',
  'load-more',
  'modal-backdrop',
  'modal',
  'modal-title',
  'modal-close',
  'modal-imgs',
  'modal-actions',
  'modal-meta',
  'modal-state',
  'modal-product-desc-section',
  'modal-product-descriptions',
  'modal-tags-section',
  'modal-tags',
  'modal-desc-section',
  'modal-descriptions',
  'modal-fulltext-section',
  'modal-full-text',
  'lightbox',
  'lightbox-close',
  'lightbox-img',
];

class FakeDocument {
  constructor() {
    this.elements = new Map(PAGE_ELEMENT_IDS.map(id => [id, new FakeElement()]));
    this.body = new FakeElement('body');
    this.listeners = new Map();
  }

  getElementById(id) {
    return this.elements.get(id);
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }

  createTextNode(text) {
    return new FakeTextNode(text);
  }

  createDocumentFragment() {
    return new FakeElement('#fragment', { fragment: true });
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
}

function createTimerQueue() {
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeout(callback) {
      const id = nextId++;
      pending.set(id, callback);
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    runAll() {
      const callbacks = [...pending.values()];
      pending.clear();
      for (const callback of callbacks) callback();
    },
  };
}

function jsonResponse(value) {
  return {
    ok: true,
    status: 200,
    json: async () => value,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function bootPage(fetchImpl) {
  const document = new FakeDocument();
  const timers = createTimerQueue();
  const window = { ArchiveCatalog: require('./catalog-ui.js') };
  const context = vm.createContext({
    window,
    document,
    fetch: fetchImpl,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    console,
  });

  new vm.Script(PAGE_SCRIPT, { filename: 'archive/index.html' }).runInContext(context);
  return {
    document,
    elements: Object.fromEntries(document.elements),
    timers,
  };
}

async function flushMicrotasks(rounds = 20) {
  for (let index = 0; index < rounds; index += 1) await Promise.resolve();
}

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  assert.fail(message);
}

function cardNodes(elements) {
  return elements.grid.children.filter(child => child.classList.contains('card'));
}

function findCard(elements, displayName) {
  return cardNodes(elements).find(card => card.textContent.includes(displayName));
}

function makeSummary(productId, overrides = {}) {
  return {
    product_id: productId,
    language: 'english',
    display_name: `Summary ${productId}`,
    category: 'Stamps',
    status: 'Current',
    tags: [],
    image_count: 1,
    primary_image: {
      b2_url: `https://stamps.example/archive/${productId}/${productId}.png`,
    },
    detail_shard: productId.slice(0, 3),
    ...overrides,
  };
}

function makeSummaries(count, firstId = 167000) {
  return Array.from({ length: count }, (_, index) => makeSummary(String(firstId + index)));
}

function makeDetail(summary, name = `Detailed ${summary.product_id}`) {
  return {
    product_id: summary.product_id,
    language: summary.language,
    name,
    category: summary.category,
    status: summary.status,
    tags: summary.tags,
    images: [{
      image_id: summary.product_id,
      filename: `${summary.product_id}.png`,
      b2_url: summary.primary_image.b2_url,
      description: `${name} image`,
      full_text: '',
    }],
    descriptions: [],
  };
}

function createCatalogFetch(summary, handlers = {}) {
  const manifest = {
    schema_version: 1,
    version: 'test-version',
    updated_at: 'test-version',
    summary_url: 'catalog-summary.json',
    search_url: 'catalog-search.json',
    details_url: 'catalog-details/',
  };
  const calls = [];
  const fetch = (url, options) => {
    calls.push({ url, options });
    if (url === 'catalog-manifest.json') return Promise.resolve(jsonResponse(manifest));
    if (url === 'catalog-summary.json?v=test-version') return Promise.resolve(jsonResponse(summary));
    if (url === 'catalog-search.json?v=test-version') {
      return handlers.search ? handlers.search() : Promise.resolve(jsonResponse({}));
    }
    if (url.startsWith('catalog-details/')) {
      return handlers.details
        ? handlers.details(url)
        : Promise.resolve(jsonResponse({}));
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  };
  return { fetch, calls };
}

test('generated assets boot the actual page with 96 cards', async () => {
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({ url, options });
    const relativePath = String(url).split('?')[0];
    const assetPath = path.join(ARCHIVE_DIR, relativePath);
    if (!fs.existsSync(assetPath)) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    return jsonResponse(JSON.parse(fs.readFileSync(assetPath, 'utf8')));
  };

  const { elements } = bootPage(fetch);
  await flushMicrotasks();

  assert.equal(cardNodes(elements).length, 96);
  assert.equal(calls[0].url, 'catalog-manifest.json');
  assert.equal(calls[0].options.cache, 'no-cache');
});

test('actual page batches cards, appends nodes, and resets on filter, search, and sort', async () => {
  const summary = makeSummaries(250);
  const { fetch, calls } = createCatalogFetch(summary);
  const { elements, timers } = bootPage(fetch);
  await waitFor(() => cardNodes(elements).length === 96, 'page did not render its first batch');

  assert.equal(elements['result-count'].textContent, 'Showing 96 of 250 results');
  assert.equal(elements['grid-footer'].hidden, false);
  assert.equal(calls[0].options.cache, 'no-cache');
  assert.equal(calls[1].url, 'catalog-summary.json?v=test-version');

  const initialCards = cardNodes(elements);
  elements['load-more'].click();
  const appendedCards = cardNodes(elements);
  assert.equal(appendedCards.length, 192);
  assert.equal(elements['result-count'].textContent, 'Showing 192 of 250 results');
  assert.strictEqual(appendedCards[0], initialCards[0]);
  assert.strictEqual(appendedCards[95], initialCards[95]);

  elements.sort.value = 'asc';
  elements.sort.dispatch('change');
  assert.equal(cardNodes(elements).length, 96);
  assert.equal(elements['result-count'].textContent, 'Showing 96 of 250 results');

  elements['load-more'].click();
  elements['category-filter'].value = 'Stamps';
  elements['category-filter'].dispatch('change');
  assert.equal(cardNodes(elements).length, 96);

  elements['load-more'].click();
  elements.search.value = 'Summary';
  elements.search.dispatch('input');
  timers.runAll();
  await flushMicrotasks();
  assert.equal(cardNodes(elements).length, 96);
  assert.equal(elements['result-count'].textContent, 'Showing 96 of 250 results');
});

test('card prefers thumbnail, retries original once, and keeps modal and lightbox original', async () => {
  const originalUrl = 'https://stamps.example/archive/167001/167001.png';
  const thumbnailUrl = 'https://stamps.example/archive/167001/167001.thumb.webp';
  const product = makeSummary('167001', {
    primary_image: {
      b2_url: originalUrl,
      thumbnail_url: thumbnailUrl,
    },
  });
  const { fetch } = createCatalogFetch([product]);
  const { elements } = bootPage(fetch);
  await waitFor(() => cardNodes(elements).length === 1, 'page did not render thumbnail fixture');

  const card = cardNodes(elements)[0];
  const imageWrap = card.children[0];
  const cardImage = imageWrap.children[0];
  assert.equal(cardImage.src, thumbnailUrl);

  cardImage.onerror();
  assert.equal(cardImage.src, originalUrl);
  assert.strictEqual(imageWrap.children[0], cardImage);

  cardImage.onerror();
  assert.match(imageWrap.innerHTML, /card-img-placeholder/);
  assert.equal(imageWrap.children.length, 0);

  card.click();
  const modalFigure = elements['modal-imgs'].children[0].children[0];
  const modalImage = modalFigure.children[0];
  assert.equal(modalImage.src, originalUrl);
  modalFigure.click();
  assert.equal(elements['lightbox-img'].src, originalUrl);
});

test('modal offers a single viewer link that survives the detail render', async () => {
  const product = makeSummary('167001');
  const detailRequest = deferred();
  const { fetch } = createCatalogFetch([product], {
    details: () => detailRequest.promise,
  });
  const { elements } = bootPage(fetch);
  await waitFor(() => cardNodes(elements).length === 1, 'page did not render viewer link fixture');

  cardNodes(elements)[0].click();
  const summaryLink = elements['modal-actions'].children[0];
  assert.equal(elements['modal-actions'].children.length, 1);
  assert.equal(summaryLink.tagName, 'A');
  assert.equal(summaryLink.href, '../index.html?id=167001');
  assert.equal(summaryLink.target, '_blank');
  assert.equal(summaryLink.rel, 'noopener');

  detailRequest.resolve(jsonResponse({
    '167001': makeDetail(product, 'Detailed 167001'),
  }));
  await waitFor(
    () => elements['modal-title'].textContent.includes('Detailed 167001'),
    'modal detail did not render',
  );

  assert.equal(elements['modal-actions'].children.length, 1);
  assert.equal(elements['modal-actions'].children[0].href, '../index.html?id=167001');
});

test('actual page loads deep search once and only applies the current query', async () => {
  const oldProduct = makeSummary('167001');
  const currentProduct = makeSummary('167002');
  const searchRequest = deferred();
  const { fetch, calls } = createCatalogFetch([oldProduct, currentProduct], {
    search: () => searchRequest.promise,
  });
  const { elements, timers } = bootPage(fetch);
  await waitFor(() => cardNodes(elements).length === 2, 'page did not render search fixtures');

  elements.search.value = 'old-only';
  elements.search.dispatch('input');
  timers.runAll();
  await flushMicrotasks();
  assert.match(elements['search-status'].textContent, /Loading full-text search/);
  assert.equal(calls.filter(call => call.url.startsWith('catalog-search.json')).length, 1);

  elements.search.value = 'current-only';
  elements.search.dispatch('input');
  timers.runAll();
  await flushMicrotasks();
  assert.equal(calls.filter(call => call.url.startsWith('catalog-search.json')).length, 1);

  searchRequest.resolve(jsonResponse({
    '167001': 'old-only',
    '167002': 'current-only',
  }));
  await waitFor(
    () => cardNodes(elements).length === 1,
    'deep search did not render the current query result',
  );

  assert.match(cardNodes(elements)[0].textContent, /Summary 167002/);
  assert.doesNotMatch(cardNodes(elements)[0].textContent, /Summary 167001/);
  assert.equal(elements['search-status'].textContent, '');
  assert.equal(elements['result-count'].textContent, 'Showing 1 of 1 results');
});

test('actual page caches a detail shard and prevents a stale modal response', async () => {
  const productA = makeSummary('167001');
  const productB = makeSummary('168001');
  const productC = makeSummary('167002');
  const shardRequests = {
    167: deferred(),
    168: deferred(),
  };
  const detailCalls = [];
  const { fetch } = createCatalogFetch([productA, productB, productC], {
    details: url => {
      const shard = url.match(/catalog-details\/(\d+)\.json/)[1];
      detailCalls.push(shard);
      return shardRequests[shard].promise;
    },
  });
  const { elements } = bootPage(fetch);
  await waitFor(() => cardNodes(elements).length === 3, 'page did not render modal fixtures');

  findCard(elements, 'Summary 167001').click();
  assert.match(elements['modal-title'].textContent, /Summary 167001/);
  assert.match(elements['modal-state'].textContent, /Loading full product details/);

  findCard(elements, 'Summary 168001').click();
  shardRequests[168].resolve(jsonResponse({
    '168001': makeDetail(productB, 'Detailed B'),
  }));
  await waitFor(
    () => elements['modal-title'].textContent.includes('Detailed B'),
    'current modal detail did not render',
  );

  shardRequests[167].resolve(jsonResponse({
    '167001': makeDetail(productA, 'Detailed A'),
    '167002': makeDetail(productC, 'Detailed C'),
  }));
  await flushMicrotasks();
  assert.match(elements['modal-title'].textContent, /Detailed B/);
  assert.doesNotMatch(elements['modal-title'].textContent, /Detailed A/);

  findCard(elements, 'Summary 167002').click();
  await waitFor(
    () => elements['modal-title'].textContent.includes('Detailed C'),
    'cached shard detail did not render',
  );
  assert.deepEqual(detailCalls.sort(), ['167', '168']);
});

test('actual page keeps modal summary on failure and retry renders details', async () => {
  const product = makeSummary('169001');
  const firstRequest = deferred();
  const retryRequest = deferred();
  let detailAttempts = 0;
  const { fetch } = createCatalogFetch([product], {
    details: () => {
      detailAttempts += 1;
      return detailAttempts === 1 ? firstRequest.promise : retryRequest.promise;
    },
  });
  const { elements } = bootPage(fetch);
  await waitFor(() => cardNodes(elements).length === 1, 'page did not render retry fixture');

  findCard(elements, 'Summary 169001').click();
  const summaryTitle = elements['modal-title'].textContent;
  firstRequest.reject(new Error('network failed'));
  await waitFor(
    () => /could not be loaded/.test(elements['modal-state'].textContent),
    'modal did not render its detail error',
  );

  assert.equal(elements['modal-title'].textContent, summaryTitle);
  const retryButton = elements['modal-state'].children.find(
    child => child.classList.contains('retry-button'),
  );
  assert.ok(retryButton);
  retryButton.click();
  assert.equal(detailAttempts, 2);
  assert.match(elements['modal-state'].textContent, /Loading full product details/);

  retryRequest.resolve(jsonResponse({
    '169001': makeDetail(product, 'Retried Detail'),
  }));
  await waitFor(
    () => elements['modal-title'].textContent.includes('Retried Detail'),
    'retried modal detail did not render',
  );
  assert.equal(elements['modal-state'].hidden, true);
});
