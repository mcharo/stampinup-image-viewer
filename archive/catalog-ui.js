(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ArchiveCatalog = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const PAGE_SIZE = 96;

  function parseQuery(value) {
    const terms = [];
    const re = /"([^"]+)"|(\S+)/g;
    let match;
    while ((match = re.exec(String(value || '').toLowerCase())) !== null) {
      terms.push(match[1] !== undefined ? match[1] : match[2]);
    }
    return terms;
  }

  function buildSummarySearchText(product) {
    return [
      product.product_id,
      product.language,
      product.display_name,
      product.name,
      product.category,
      product.status,
      ...(product.tags || []),
      product.image_count,
    ].filter(value => value !== undefined && value !== null && value !== '')
      .join(' ')
      .toLowerCase();
  }

  function filterAndSort(products, state, deepSearch) {
    const terms = parseQuery(state.query);
    const results = products.filter(product => {
      if (state.language !== 'all' && (product.language || 'english') !== state.language) return false;
      if (state.category !== 'all' && (product.category || '') !== state.category) return false;
      if (state.status !== 'all' && (product.status || '').toLowerCase() !== state.status) return false;
      if (!terms.length) return true;

      const deepText = deepSearch && deepSearch[product.product_id]
        ? ` ${deepSearch[product.product_id]}`
        : '';
      const haystack = buildSummarySearchText(product) + deepText;
      return terms.every(term => haystack.includes(term));
    });

    const direction = state.sortOrder === 'asc' ? 1 : -1;
    results.sort((left, right) => {
      const numericDifference = Number(left.product_id) - Number(right.product_id);
      if (Number.isFinite(numericDifference) && numericDifference !== 0) {
        return numericDifference * direction;
      }
      return String(left.product_id).localeCompare(String(right.product_id)) * direction;
    });
    return results;
  }

  function incrementCount(counts, key) {
    counts[key] = (counts[key] || 0) + 1;
  }

  function withSortedKeys(counts) {
    const sorted = { all: counts.all };
    for (const key of Object.keys(counts).filter(key => key !== 'all').sort()) {
      sorted[key] = counts[key];
    }
    return sorted;
  }

  function buildFacetCounts(products) {
    const languages = { all: products.length };
    const categories = { all: products.length };
    const statuses = { all: products.length };

    for (const product of products) {
      incrementCount(languages, product.language || 'english');
      if (product.category) incrementCount(categories, product.category);
      if (product.status) incrementCount(statuses, product.status.toLowerCase());
    }

    return {
      languages: withSortedKeys(languages),
      categories: withSortedKeys(categories),
      statuses: withSortedKeys(statuses),
    };
  }

  function nextBatchEnd(currentCount, totalCount, pageSize = PAGE_SIZE) {
    return Math.min(totalCount, Math.max(0, currentCount) + pageSize);
  }

  function createCachedLoader(load) {
    let promise;
    return function cachedLoad() {
      if (!promise) {
        let request;
        try {
          request = Promise.resolve(load());
        } catch (error) {
          request = Promise.reject(error);
        }
        const cachedPromise = request.catch(error => {
          if (promise === cachedPromise) promise = undefined;
          throw error;
        });
        promise = cachedPromise;
      }
      return promise;
    };
  }

  function createShardLoader(loadShard) {
    const promises = new Map();

    function load(shard) {
      if (promises.has(shard)) return promises.get(shard);

      let request;
      try {
        request = Promise.resolve(loadShard(shard));
      } catch (error) {
        request = Promise.reject(error);
      }
      const cachedRequest = request.catch(error => {
        if (promises.get(shard) === cachedRequest) promises.delete(shard);
        throw error;
      });
      promises.set(shard, cachedRequest);
      return cachedRequest;
    }

    function retry(shard) {
      promises.delete(shard);
      return load(shard);
    }

    return { load, retry };
  }

  function createLatestRequestGate() {
    let latestToken = 0;
    return {
      begin() {
        latestToken += 1;
        return latestToken;
      },
      invalidate() {
        latestToken += 1;
      },
      isCurrent(token) {
        return token === latestToken;
      },
    };
  }

  function versionedUrl(url, version) {
    const separator = String(url).includes('?') ? '&' : '?';
    return `${url}${separator}v=${encodeURIComponent(version)}`;
  }

  return {
    PAGE_SIZE,
    buildFacetCounts,
    buildSummarySearchText,
    createCachedLoader,
    createLatestRequestGate,
    createShardLoader,
    filterAndSort,
    nextBatchEnd,
    parseQuery,
    versionedUrl,
  };
}));
