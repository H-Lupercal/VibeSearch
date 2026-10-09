'use strict';
const byId = id => document.getElementById(id);
const status = message => { byId('status').textContent = message; };
async function getJSON(url) {
  const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail?.message || 'Request failed (' + response.status + ')');
  return data;
}
function node(tag, text) { const el = document.createElement(tag); el.textContent = text; return el; }
const reader = byId('reader');
if (reader) {
  const gid = Number(reader.dataset.gallery), count = Number(reader.dataset.pages);
  const image = byId('page-image'), input = byId('page-number');
  let current = 1, generation = 0, controller = null, blobURL = null;
  async function show(number) {
    if (!Number.isInteger(number) || number < 1 || number > count) { status('Choose a valid page.'); return; }
    current = number; input.value = String(number);
    byId('previous-page').disabled = number === 1; byId('next-page').disabled = number === count;
    controller?.abort(); controller = new AbortController(); const version = ++generation;
    image.removeAttribute('src'); if (blobURL) { URL.revokeObjectURL(blobURL); blobURL = null; }
    status('Loading page ' + number + '…');
    try {
      const response = await fetch('/api/gallery/' + gid + '/pages/' + number, {signal: controller.signal, cache: 'no-store'});
      if (!response.ok) { const data = await response.json(); throw new Error(data.detail?.message || 'Page unavailable'); }
      const blob = await response.blob(); if (version !== generation) return;
      blobURL = URL.createObjectURL(blob); image.src = blobURL; image.alt = 'Gallery ' + gid + ', page ' + number;
      status('Page ' + number + ' of ' + count);
    } catch (err) { if (err.name !== 'AbortError' && version === generation) status(err.message); }
  }
  byId('previous-page').onclick = () => show(current - 1);
  byId('next-page').onclick = () => show(current + 1);
  input.onchange = () => show(Number(input.value));
  window.addEventListener('pagehide', () => { controller?.abort(); if (blobURL) URL.revokeObjectURL(blobURL); });
  show(1);
} else {
  const selected = {}; let offset = 0, version = 0;
  function renderFilters() {
    const target = byId('filters'); target.replaceChildren();
    Object.entries(selected).forEach(([field, values]) => values.forEach((value, index) => {
      const li = node('li', field + ': ' + String(value) + ' '), remove = node('button', 'Remove');
      remove.type = 'button'; remove.onclick = () => { selected[field].splice(index, 1); if (!selected[field].length) delete selected[field]; renderFilters(); };
      li.append(remove); target.append(li);
    }));
  }
  function add(value) { const field = byId('field').value; (selected[field] ||= []).push(value); renderFilters(); byId('suggestions').replaceChildren(); }
  byId('add-filter').onclick = () => { const value = byId('selection').value.trim(); if (value) add(/^\d+$/.test(value) ? Number(value) : value); };
  byId('suggest').onclick = async () => {
    const field = byId('field').value; const sourceField = field === 'exclude_tags' ? 'tag' : field === 'exclude_artists' ? 'artist' : field;
    try {
      const params = new URLSearchParams({field: sourceField, prefix: byId('selection').value});
      const data = await getJSON('/api/suggest?' + params); const list = byId('suggestions'); list.replaceChildren();
      data.items.forEach(item => { const li = document.createElement('li'), button = node('button', item.name + ' (' + item.count + ')'); button.type = 'button'; button.onclick = () => add(item.id); li.append(button); list.append(li); });
    } catch (err) { status(err.message); }
  };
  async function search() {
    const mine = ++version, display = byId('display').value, query = byId('query').value.trim();
    const results = byId('results'); results.replaceChildren(); status('Loading…');
    const params = new URLSearchParams({display, filters: JSON.stringify(selected)});
    if (query) { params.set('q', query); params.set('top_k', '20'); } else { params.set('limit', '50'); params.set('offset', String(offset)); }
    try {
      const data = await getJSON((query ? '/api/search/vibe?' : '/api/galleries?') + params);
      if (mine !== version) return;
      const items = data.items || data.hits || [];
      items.forEach(item => {
        const id = Number(item.gallery_id), article = document.createElement('article'), link = node('a', 'Gallery ' + id);
        link.href = '/gallery/' + id + '?display=' + display; article.append(link);
        if (display === 'full') {
          if (item.title_display) article.append(node('h2', item.title_display));
          if (item.tags?.length) article.append(node('p', item.tags.map(tag => tag.name).join(', ')));
        }
        article.append(node('p', item.num_pages + ' pages'));
        if (typeof item.cosine_distance === 'number') {
          const distance = node('p', 'Cosine distance: ' + item.cosine_distance.toFixed(6) + ' (lower is closer)');
          distance.className = 'distance'; article.append(distance);
        }
        results.append(article);
      });
      status(query ? data.eligible_indexed + ' / ' + data.eligible_total + ' eligible records indexed' + (data.pending_warning ? '; index coverage is incomplete or pending.' : '') : data.total + ' local records');
      byId('older').disabled = Boolean(query) || offset === 0;
      byId('newer').disabled = Boolean(query) || offset + items.length >= data.total;
    } catch (err) { if (mine === version) status(err.message); }
  }
  byId('search-form').onsubmit = event => { event.preventDefault(); offset = 0; search(); };
  byId('display').onchange = () => { byId('suggestions').replaceChildren(); offset = 0; search(); };
  byId('older').onclick = () => { offset = Math.max(0, offset - 50); search(); };
  byId('newer').onclick = () => { offset += 50; search(); };
  byId('display').value = document.body.dataset.display;
  search();
}
