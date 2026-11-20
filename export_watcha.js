/*
 * Watcha Pedia ratings -> Clean CSV (Title, Year, Rating only)
 */
(async () => {
  const captured = [];
  let notifyCapture = () => {};

  const looksRelevant = (url) => /users\/[^/]+\/contents\/[^/]+\/ratings/i.test(url);

  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    try {
      const req = args[0];
      const url = typeof req === 'string' ? req : req.url;
      if (looksRelevant(url)) {
        let headers = (args[1] && args[1].headers) || (req instanceof Request ? req.headers : {});
        if (headers instanceof Headers) headers = Object.fromEntries(headers.entries());
        captured.push({ url, headers, data: await res.clone().json() });
        notifyCapture();
      }
    } catch (e) { /* ignore */ }
    return res;
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._url = String(url);
    this._headers = {};
    this.addEventListener('load', () => {
      try {
        if (looksRelevant(this._url)) {
          captured.push({ url: this._url, headers: this._headers, data: JSON.parse(this.responseText) });
          notifyCapture();
        }
      } catch (e) { /* ignore */ }
    });
    return origOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    if (this._headers) this._headers[k] = v;
    return origSetHeader.call(this, k, v);
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitForNextCapture = (timeoutMs) =>
    new Promise((resolve) => {
      const timer = setTimeout(resolve, timeoutMs);
      notifyCapture = () => {
        clearTimeout(timer);
        resolve();
      };
    });

  // Trigger 1 scroll to capture the ratings endpoint
  window.scrollTo(0, 0);
  await sleep(150);
  for (let i = 0; i < 4 && !captured.length; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await waitForNextCapture(1000);
  }

  if (!captured.length) {
    console.error('Could not capture ratings. Reload the page, run the script, and scroll down once.');
    return;
  }

  const template = captured[0];
  const seenUrls = new Set(captured.map((c) => c.url));
  let nextUri = template.data?.result?.next_uri || template.data?.next_uri;

  if (nextUri) {
    while (nextUri) {
      const fullUrl = new URL(nextUri, template.url).toString();
      if (seenUrls.has(fullUrl)) break;
      seenUrls.add(fullUrl);

      const res = await origFetch(fullUrl, { headers: template.headers, credentials: 'include' });
      if (!res.ok) break;
      const json = await res.json();
      captured.push({ url: fullUrl, headers: template.headers, data: json });

      nextUri = json?.result?.next_uri || json?.next_uri;
      await sleep(100);
    }
  } else {
    const baseUrl = new URL(template.url, location.origin);
    if (baseUrl.searchParams.has('page')) {
      if (baseUrl.searchParams.has('size')) baseUrl.searchParams.set('size', '100');
      let page = 1;
      captured.length = 0;
      while (true) {
        baseUrl.searchParams.set('page', String(page));
        const res = await origFetch(baseUrl.toString(), { headers: template.headers, credentials: 'include' });
        if (!res.ok) break;
        const json = await res.json();
        const list = json?.result?.result || json?.result || json?.data || [];
        if (!Array.isArray(list) || !list.length) break;
        captured.push({ url: baseUrl.toString(), headers: template.headers, data: json });
        page++;
        await sleep(100);
      }
    }
  }

  // Grab page 1 if initial scroll started on page 2
  try {
    const firstUrl = new URL(template.url, location.origin);
    if (firstUrl.searchParams.get('page') && firstUrl.searchParams.get('page') !== '1') {
      firstUrl.searchParams.set('page', '1');
      const r = await origFetch(firstUrl.toString(), { headers: template.headers, credentials: 'include' });
      if (r.ok) captured.unshift({ url: firstUrl.toString(), headers: template.headers, data: await r.json() });
    }
  } catch (e) { /* ignore */ }

  window.fetch = origFetch;
  XMLHttpRequest.prototype.open = origOpen;
  XMLHttpRequest.prototype.setRequestHeader = origSetHeader;

  // Extract ONLY rated movies (title, year, rating)
  const items = new Map();
  const pick = (o, ...keys) => {
    for (const k of keys) if (o && o[k] != null && o[k] !== '') return o[k];
    return '';
  };

  const addItem = (wrapper, content) => {
    const action = wrapper.user_content_action || wrapper.userContentAction || {};
    const rating = pick(action, 'rating', 'score') || pick(wrapper, 'rating', 'score', 'user_rating');
    const title = pick(content, 'title');
    if (!title || rating === '') return; // Skip unrated/related junk

    const key = pick(content, 'code', 'id') || `${title}_${pick(content, 'year')}`;
    items.set(key, {
      title,
      year: pick(content, 'year'),
      rating,
    });
  };

  for (const c of captured) {
    const list = c.data?.result?.result || c.data?.result || c.data?.data || [];
    if (Array.isArray(list)) {
      for (const entry of list) {
        if (entry?.content) addItem(entry, entry.content);
        else if (entry?.title) addItem(entry, entry);
      }
    }
  }

  const rows = [...items.values()];
  console.log(`Found ${rows.length} rated movies.`);

  if (!rows.length) {
    console.warn('No rated movies found.');
    return;
  }

  const esc = (v) => {
    const s = String(v ?? '');
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const csv = [
    'title,year,rating10',
    ...rows.map((r) => [esc(r.title), esc(r.year), esc(r.rating)].join(',')),
  ].join('\r\n');

  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' }));
  a.download = 'watcha_ratings.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();

  console.log('Done: watcha_ratings.csv downloaded.');
})();