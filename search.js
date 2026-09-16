/* Search for Recompete Radar. Written to search.js by build_site.py.

   Two tiers, both searched in the browser over files the build writes. Nothing
   a reader types leaves the page.

   1. The box in the header of every page searches search-index.json: every
      supplier, department, category and province with a page of its own. The file loads on the first focus or keystroke, not with the page.
   2. search.html searches search-contracts.json: every live contract. That
      file loads only when search.html is opened.

   Both index files are built from the rows AFTER suppress_individuals() has
   run, so a name the site withholds on its pages is not in either file.
   audit.py fails the deploy if one is.

   With no JavaScript this file does nothing, the box is never drawn and the
   browse links in the header keep working. */
(function () {
  'use strict';
  if (!window.fetch || !document.querySelector || !window.JSON) return;

  var header = document.getElementById('rr-search');
  var contracts = document.getElementById('rr-contracts');
  if (!header && !contracts) return;
  var root = (header || contracts).getAttribute('data-root') || '';

  // Case, accents and punctuation are ignored. "Egalite" finds "Égalité", and
  // "C-2025-2026" finds the reference number "C-2025-2026-Q1-00127".
  var hasNormalize = typeof ''.normalize === 'function';
  function norm(s) {
    s = String(s || '');
    if (hasNormalize) s = s.normalize('NFD').replace(/[̀-ͯ]/g, '');
    return s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/^ | $/g, '');
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;   // never innerHTML: names are data
    return e;
  }

  function getJSON(file) {
    return fetch(root + file, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error(file + ' ' + r.status);
      return r.json();
    });
  }

  // The same URL build_site.source_record_url() writes: both halves quoted
  // with nothing left safe. encodeURIComponent leaves ! ' ( ) * alone and
  // Python's quote does not, so those five are encoded by hand.
  function quote(s) {
    return encodeURIComponent(String(s || '').trim()).replace(/[!'()*]/g, function (ch) {
      return '%' + ch.charCodeAt(0).toString(16).toUpperCase();
    });
  }
  function sourceUrl(org, ref) {
    org = quote(org); ref = quote(ref);
    return org && ref ? 'https://search.open.canada.ca/contracts/record/' + org + '%2C' + ref : '';
  }

  function english(name) { return String(name || '').split(' | ')[0]; }

  function money(v) {
    v = v || 0;
    function g(n) { return n.toLocaleString('en-CA'); }
    if (v >= 1e9) return '$' + (v / 1e9).toLocaleString('en-CA', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + 'B';
    if (v >= 1e6) return '$' + g(Math.round(v / 1e6)) + 'M';
    if (v >= 1e3) return '$' + g(Math.round(v / 1e3)) + 'K';
    return '$' + g(Math.round(v));
  }

  /* ------------------------------------------------------------ tier 1 */
  var TYPE = { d: 'Department', i: 'Supplier', c: 'Category', p: 'Province' };
  var entities = null, entityLoad = null;

  function loadEntities() {
    if (!entityLoad) {
      entityLoad = getJSON('search-index.json').then(function (j) {
        entities = j.entities.map(function (e) {
          return { t: e[0], name: e[1], href: e[2], n: e[3], k: ' ' + norm(e[1]) };
        });
        return entities;
      }, function (err) { entityLoad = null; throw err; });
    }
    return entityLoad;
  }

  // Rank: whole-string prefix, then the start of any word, then anywhere.
  // Ties go to the entity with more contracts, so "National Defence" beats a
  // one-contract namesake.
  function rankEntities(query, limit) {
    var q = norm(query);
    if (!q || !entities) return [];
    var hits = [];
    for (var i = 0; i < entities.length; i++) {
      var e = entities[i], at = e.k.indexOf(' ' + q), tier;
      if (at === 0) tier = 0;
      else if (at > 0) tier = 1;
      else if (e.k.indexOf(q) >= 0) tier = 2;
      else continue;
      hits.push({ e: e, tier: tier });
    }
    hits.sort(function (a, b) {
      return (a.tier - b.tier) || (b.e.n - a.e.n) || (a.e.name < b.e.name ? -1 : a.e.name > b.e.name ? 1 : 0);
    });
    return hits.slice(0, limit).map(function (h) { return h.e; });
  }

  function drawHeaderBox() {
    var form = el('form', 'srch');
    form.setAttribute('role', 'search');
    form.action = root + 'search.html';
    form.method = 'get';

    var label = el('label', 'srch-l', 'Search suppliers, departments and categories');
    label.htmlFor = 'rr-q';
    var input = el('input');
    input.id = 'rr-q';
    input.name = 'q';
    input.type = 'search';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.placeholder = 'Search suppliers, departments, categories';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', 'rr-list');

    var list = el('ul', 'srch-list');
    list.id = 'rr-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;

    form.appendChild(label);
    form.appendChild(input);
    form.appendChild(list);
    header.appendChild(form);
    header.hidden = false;

    var active = -1, items = [];

    function close() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function setActive(i) {
      if (items[active]) items[active].classList.remove('on');
      active = i;
      if (items[active]) {
        items[active].classList.add('on');
        input.setAttribute('aria-activedescendant', items[active].id);
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function option(i, href, main, side) {
      var li = el('li');
      li.id = 'rr-opt-' + i;
      li.setAttribute('role', 'option');
      var a = el('a');
      a.href = href;
      a.tabIndex = -1;
      a.appendChild(el('span', 'srch-n', main));
      if (side) a.appendChild(el('span', 'srch-t', side));
      li.appendChild(a);
      return li;
    }

    function render() {
      var q = input.value;
      list.textContent = '';
      items = [];
      active = -1;
      if (!norm(q)) { close(); return; }
      var hits = rankEntities(q, 8);
      hits.forEach(function (e, i) {
        var side = TYPE[e.t] + ' · ' + e.n.toLocaleString('en-CA') + (e.n === 1 ? ' contract' : ' contracts');
        var li = option(i, root + e.href, english(e.name), side);
        if (e.t === 'd') li.firstChild.title = e.name;
        items.push(li);
        list.appendChild(li);
      });
      if (entities && !hits.length) {
        var none = el('li', 'srch-none', 'No supplier, department or category matches.');
        list.appendChild(none);
      }
      var all = option(hits.length, root + 'search.html?q=' + encodeURIComponent(q.trim()),
                       'Search every contract for “' + q.trim() + '”', '');
      all.className = 'srch-all';
      items.push(all);
      list.appendChild(all);
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    function warm() {
      loadEntities().then(function () { if (norm(input.value)) render(); }, function () {});
    }

    input.addEventListener('focus', warm);
    input.addEventListener('input', function () { warm(); render(); });
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowDown' || ev.key === 'Down') {
        if (list.hidden) render();
        ev.preventDefault();
        setActive(items.length ? (active + 1) % items.length : -1);
      } else if (ev.key === 'ArrowUp' || ev.key === 'Up') {
        ev.preventDefault();
        setActive(items.length ? (active <= 0 ? items.length - 1 : active - 1) : -1);
      } else if (ev.key === 'Escape' || ev.key === 'Esc') {
        close();
      } else if (ev.key === 'Enter' && items[active]) {
        ev.preventDefault();
        window.location.href = items[active].firstChild.href;
      }
    });
    // Enter with nothing highlighted submits the form, which opens the
    // contract search for the same words.
    form.addEventListener('submit', function (ev) {
      if (!norm(input.value)) ev.preventDefault();
    });
    document.addEventListener('click', function (ev) {
      if (!form.contains(ev.target)) close();
    });
  }

  /* ------------------------------------------------------------ tier 2 */
  var LIMIT = 200;

  function drawContractSearch() {
    var form = el('form', 'srch srch-page');
    form.setAttribute('role', 'search');
    var label = el('label', 'srch-l', 'Search every live contract');
    label.htmlFor = 'rr-cq';
    var input = el('input');
    input.id = 'rr-cq';
    input.name = 'q';
    input.type = 'search';
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.placeholder = 'Supplier, department, category, reference or keyword';
    var button = el('button', null, 'Search');
    button.type = 'submit';
    form.appendChild(label);
    form.appendChild(input);
    form.appendChild(button);

    var panel = el('div', 'flt');
    var chips = el('p', 'sb flt-chips');
    chips.hidden = true;
    var status = el('p', 'sb srch-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    var out = el('div');

    contracts.appendChild(form);
    contracts.appendChild(panel);
    contracts.appendChild(chips);
    contracts.appendChild(status);
    contracts.appendChild(out);

    var rows = null, loading = null, S = [], D = [], C = [];
    function load() {
      if (!loading) {
        status.textContent = 'Loading contracts…';
        loading = getJSON('search-contracts.json').then(function (j) {
          // Row: ref, supplier index, department index, category index,
          // value, days to expiry, buyer org code, description, bidders,
          // how contested it was last time, supplier country, resource-based.
          // Lookups: suppliers [text, link, key], departments [name, link],
          // categories [name, link, key].
          S = j.suppliers; D = j.departments; C = j.categories;
          rows = j.contracts.map(function (c) {
            var v = S[c[1]], d = D[c[2]], k = C[c[3]];
            return { ref: c[0], v: v[0], vh: v[1], vk: v[2] || '', di: c[2], d: d[0], dh: d[1],
                     ci: c[3], c: k[0], ch: k[1], ck: k[2] || '',
                     val: c[4], days: c[5], src: sourceUrl(c[6], c[0]), sc: c[7] || '',
                     bids: c[8], dens: c[9] || '', cc: c[10] || '', res: c[11] === 1,
                     k: ' ' + norm([c[0], v[0], d[0], k[0], c[7] || ''].join(' ')) };
          });
          return rows;
        }, function (err) { loading = null; throw err; });
      }
      return loading;
    }

    function link(href, text) {
      if (!href) return document.createTextNode(text);
      var a = el('a', null, text);
      a.href = root + href;
      return a;
    }

    /* ---------------------------------------------------------- filters */
    // Every filter narrows the same match set, and they all combine with the
    // search words. All state lives in the address, so a filtered search can
    // be bookmarked or sent to someone. The grey names on the index pages
    // link here with ?vendor= or ?cat= set.
    var state = { q: '', cats: [], depts: [], vmin: null, vmax: null, bmin: null, bmax: null,
                  within: 0, country: '', res: '', vendor: '' };

    function field(labelText, control, cls) {
      var wrap = el('div', 'flt-f' + (cls ? ' ' + cls : ''));
      var id = 'flt-' + Math.random().toString(36).slice(2, 8);
      var lab = el('label', 'flt-l', labelText);
      lab.htmlFor = id;
      control.id = id;
      wrap.appendChild(lab);
      wrap.appendChild(control);
      return wrap;
    }

    function numberBox(placeholder) {
      var i = el('input');
      i.type = 'number';
      i.min = '0';
      i.inputMode = 'numeric';
      i.placeholder = placeholder;
      return i;
    }

    function select(options) {
      var s = el('select');
      options.forEach(function (o) {
        var opt = el('option', null, o[1]);
        opt.value = o[0];
        s.appendChild(opt);
      });
      return s;
    }

    // A checkbox list with its own "find" box, for categories and departments.
    function pickList(title, key) {
      var box = el('details', 'flt-pick');
      var sum = el('summary', null, title);
      box.appendChild(sum);
      var find = el('input');
      find.type = 'search';
      find.placeholder = 'Find a ' + title.toLowerCase().replace(/ies$/, 'y').replace(/s$/, '');
      find.setAttribute('aria-label', 'Find in ' + title.toLowerCase());
      box.appendChild(find);
      var list = el('ul', 'flt-list');
      box.appendChild(list);
      find.addEventListener('input', function () {
        var q = norm(find.value);
        Array.prototype.forEach.call(list.children, function (li) {
          li.hidden = q ? li.getAttribute('data-n').indexOf(q) < 0 : false;
        });
      });
      var api = {
        box: box,
        fill: function (items) {
          list.textContent = '';
          items.forEach(function (it) {
            var li = el('li');
            li.setAttribute('data-n', ' ' + norm(it.label));
            var lab = el('label');
            var cb = el('input');
            cb.type = 'checkbox';
            cb.value = it.value;
            cb.checked = state[key].indexOf(it.value) >= 0;
            cb.addEventListener('change', function () {
              var at = state[key].indexOf(it.value);
              if (cb.checked && at < 0) state[key].push(it.value);
              if (!cb.checked && at >= 0) state[key].splice(at, 1);
              api.count();
              apply();
            });
            lab.appendChild(cb);
            lab.appendChild(document.createTextNode(' ' + it.label + ' '));
            lab.appendChild(el('span', 'd', '(' + it.n.toLocaleString('en-CA') + ')'));
            li.appendChild(lab);
            list.appendChild(li);
          });
          // A list with something already ticked (from the address) opens, so
          // the reader can see what is narrowing the results.
          if (state[key].length) box.open = true;
          api.count();
        },
        count: function () {
          sum.textContent = title + (state[key].length ? ' (' + state[key].length + ' chosen)' : '');
        },
        sync: function () {
          Array.prototype.forEach.call(list.querySelectorAll('input[type=checkbox]'), function (cb) {
            cb.checked = state[key].indexOf(cb.value) >= 0;
          });
          api.count();
        }
      };
      return api;
    }

    var catPick = pickList('Categories', 'cats');
    var deptPick = pickList('Departments', 'depts');
    var vmin = numberBox('Min $'), vmax = numberBox('Max $');
    var bmin = numberBox('Min'), bmax = numberBox('Max');
    var within = select([['0', 'Any time'], ['6', 'Within 6 months'], ['12', 'Within 12 months'],
                         ['24', 'Within 24 months']]);
    var country = select([['', 'Anywhere'], ['ca', 'Canada'], ['intl', 'Outside Canada']]);
    var resSel = select([['', 'Show all'], ['only', 'Only resource-based'], ['hide', 'Hide resource-based']]);
    var clear = el('button', 'flt-clear', 'Clear filters');
    clear.type = 'button';

    var pair = function (a, b) {
      var w = el('span', 'flt-pair');
      w.appendChild(a);
      w.appendChild(el('span', 'd', '–'));
      w.appendChild(b);
      return w;
    };
    var valueWrap = el('div', 'flt-f');
    valueWrap.appendChild(el('span', 'flt-l', 'Contract value ($)'));
    vmin.setAttribute('aria-label', 'Minimum contract value in dollars');
    vmax.setAttribute('aria-label', 'Maximum contract value in dollars');
    valueWrap.appendChild(pair(vmin, vmax));
    var bidWrap = el('div', 'flt-f');
    bidWrap.appendChild(el('span', 'flt-l', 'Bidders last time'));
    bmin.setAttribute('aria-label', 'Minimum number of bidders');
    bmax.setAttribute('aria-label', 'Maximum number of bidders');
    bidWrap.appendChild(pair(bmin, bmax));

    var grid = el('div', 'flt-grid');
    grid.appendChild(catPick.box);
    grid.appendChild(deptPick.box);
    grid.appendChild(valueWrap);
    grid.appendChild(bidWrap);
    grid.appendChild(field('Expires', within));
    grid.appendChild(field('Supplier based', country));
    var resField = field('Staffing', resSel);
    resField.appendChild(el('span', 'flt-note',
      'Marked only where the description names TBIPS, TSPS, ProServices or a role level. ' +
      'Most staffing contracts do not say so and are not marked.'));
    grid.appendChild(resField);
    var foot = el('div', 'flt-foot');
    foot.appendChild(clear);
    panel.appendChild(grid);
    panel.appendChild(foot);

    function numOrNull(i) {
      var v = i.value.trim();
      if (v === '') return null;
      var n = parseFloat(v);
      return isNaN(n) || n < 0 ? null : n;
    }

    [vmin, vmax, bmin, bmax].forEach(function (i) {
      i.addEventListener('input', function () {
        state.vmin = numOrNull(vmin); state.vmax = numOrNull(vmax);
        state.bmin = numOrNull(bmin); state.bmax = numOrNull(bmax);
        apply();
      });
    });
    within.addEventListener('change', function () { state.within = +within.value || 0; apply(); });
    country.addEventListener('change', function () { state.country = country.value; apply(); });
    resSel.addEventListener('change', function () { state.res = resSel.value; apply(); });
    clear.addEventListener('click', function () {
      state.cats = []; state.depts = []; state.vmin = state.vmax = state.bmin = state.bmax = null;
      state.within = 0; state.country = ''; state.res = ''; state.vendor = '';
      syncControls();
      apply();
    });

    function syncControls() {
      input.value = state.q;
      vmin.value = state.vmin === null ? '' : state.vmin;
      vmax.value = state.vmax === null ? '' : state.vmax;
      bmin.value = state.bmin === null ? '' : state.bmin;
      bmax.value = state.bmax === null ? '' : state.bmax;
      within.value = String(state.within || 0);
      country.value = state.country;
      resSel.value = state.res;
      catPick.sync();
      deptPick.sync();
    }

    function anyFilter() {
      return !!(state.cats.length || state.depts.length || state.vmin !== null || state.vmax !== null ||
                state.bmin !== null || state.bmax !== null || state.within || state.country ||
                state.res || state.vendor);
    }

    // The option lists are built from the data once it loads, with the number
    // of live contracts in each, largest first.
    function fillPickers() {
      var cn = {}, dn = {};
      rows.forEach(function (r) {
        if (r.ck) cn[r.ck] = (cn[r.ck] || 0) + 1;
        dn[r.di] = (dn[r.di] || 0) + 1;
      });
      var cats = [];
      C.forEach(function (e) {
        if (e[2] && cn[e[2]]) cats.push({ value: e[2], label: e[0], n: cn[e[2]] });
      });
      cats.sort(function (a, b) { return b.n - a.n || (a.label < b.label ? -1 : 1); });
      catPick.fill(cats);
      var depts = [];
      D.forEach(function (e, i) {
        if (e[0] && dn[i]) depts.push({ value: e[0], label: english(e[0]), n: dn[i] });
      });
      depts.sort(function (a, b) { return b.n - a.n || (a.label < b.label ? -1 : 1); });
      deptPick.fill(depts);
    }

    function passes(r, words) {
      for (var i = 0; i < words.length; i++) {
        if (r.k.indexOf(' ' + words[i]) < 0) return false;
      }
      if (state.vendor && r.vk !== state.vendor) return false;
      if (state.cats.length && state.cats.indexOf(r.ck) < 0) return false;
      if (state.depts.length && state.depts.indexOf(r.d) < 0) return false;
      if (state.vmin !== null && r.val < state.vmin) return false;
      if (state.vmax !== null && r.val > state.vmax) return false;
      if (state.bmin !== null || state.bmax !== null) {
        if (r.bids === null || r.bids === undefined) return false;
        if (state.bmin !== null && r.bids < state.bmin) return false;
        if (state.bmax !== null && r.bids > state.bmax) return false;
      }
      if (state.within && r.days > Math.round(state.within * 365 / 12)) return false;
      if (state.country === 'ca' && r.cc !== 'CA') return false;
      if (state.country === 'intl' && (r.cc === 'CA' || r.cc === '')) return false;
      if (state.res === 'only' && !r.res) return false;
      if (state.res === 'hide' && r.res) return false;
      return true;
    }

    function writeAddress() {
      if (!(window.history && history.replaceState)) return;
      var p = [];
      function add(k, v) { p.push(k + '=' + encodeURIComponent(v)); }
      if (state.q) add('q', state.q);
      if (state.vendor) add('vendor', state.vendor);
      state.cats.forEach(function (c) { add('cat', c); });
      state.depts.forEach(function (d) { add('dept', d); });
      if (state.vmin !== null) add('vmin', state.vmin);
      if (state.vmax !== null) add('vmax', state.vmax);
      if (state.bmin !== null) add('bmin', state.bmin);
      if (state.bmax !== null) add('bmax', state.bmax);
      if (state.within) add('within', state.within);
      if (state.country) add('country', state.country);
      if (state.res) add('res', state.res);
      history.replaceState(null, '', p.length ? '?' + p.join('&') : location.pathname);
    }

    function readAddress() {
      var parts = (location.search || '').replace(/^\?/, '').split('&');
      parts.forEach(function (kv) {
        if (!kv) return;
        var at = kv.indexOf('=');
        var k = at < 0 ? kv : kv.slice(0, at);
        var v = '';
        try { v = decodeURIComponent((at < 0 ? '' : kv.slice(at + 1)).replace(/\+/g, ' ')); } catch (e) { return; }
        var n = parseFloat(v);
        if (k === 'q') state.q = v;
        else if (k === 'vendor') state.vendor = v;
        else if (k === 'cat' && v && state.cats.indexOf(v) < 0) state.cats.push(v);
        else if (k === 'dept' && v && state.depts.indexOf(v) < 0) state.depts.push(v);
        else if ((k === 'vmin' || k === 'vmax' || k === 'bmin' || k === 'bmax') && !isNaN(n) && n >= 0) state[k] = n;
        else if (k === 'within' && [6, 12, 24].indexOf(n) >= 0) state.within = n;
        else if (k === 'country' && (v === 'ca' || v === 'intl')) state.country = v;
        else if (k === 'res' && (v === 'only' || v === 'hide')) state.res = v;
      });
    }

    function drawChips() {
      chips.textContent = '';
      if (!state.vendor) { chips.hidden = true; return; }
      var name = state.vendor;
      for (var i = 0; i < S.length; i++) {
        if (S[i][2] === state.vendor) { name = S[i][0]; break; }
      }
      chips.appendChild(document.createTextNode('Supplier: '));
      chips.appendChild(el('strong', null, name));
      var x = el('button', 'flt-x', 'Remove');
      x.type = 'button';
      x.setAttribute('aria-label', 'Remove the supplier filter');
      x.addEventListener('click', function () { state.vendor = ''; apply(); });
      chips.appendChild(document.createTextNode(' '));
      chips.appendChild(x);
      chips.hidden = false;
    }

    /* ---------------------------------------------------------- sorting */
    // Same as the tables on every other page: Expires, Value, Bidders and
    // Last time. A first press sorts low to high, the next high to low. Rows
    // with no value stay at the bottom both ways. The WHOLE match set is
    // sorted, then the first LIMIT rows are drawn.
    var DENSITY_RANK = { uncontested: 0, low: 1, moderate: 2, high: 3 };
    var DENSITY_CLASS = { uncontested: 'hot', low: 'warn', moderate: 'good', high: 'dim' };
    var COLS = [
      { label: 'Expires', key: function (r) { return r.days; } },
      { label: 'Value', cls: 'n', key: function (r) { return r.val; } },
      { label: 'Incumbent' },
      { label: 'Department' },
      { label: 'Category' },
      { label: 'Bidders', cls: 'n', key: function (r) { return r.bids; } },
      { label: 'Last time', key: function (r) { return DENSITY_RANK[r.dens]; } }
    ];
    var hits = [], sortCol = -1, sortDir = 1;

    function num(v) { return (v === null || v === undefined || isNaN(v)) ? null : +v; }

    function ordered() {
      var list = hits.slice();
      if (sortCol < 0) {
        list.sort(function (a, b) { return a.days - b.days; });
        return list;
      }
      var key = COLS[sortCol].key;
      list.sort(function (a, b) {
        var x = num(key(a)), y = num(key(b));
        if (x === null && y === null) return a.days - b.days;
        if (x === null) return 1;
        if (y === null) return -1;
        return (x - y) * sortDir || (a.days - b.days);
      });
      return list;
    }

    function setStatus() {
      var n = hits.length;
      var order = sortCol < 0 ? 'expiring soonest'
        : COLS[sortCol].label.toLowerCase() + (sortDir === 1 ? ', low to high' : ', high to low');
      status.textContent = n === 0 ? 'No live contract matches.'
        : (n === 1 ? '1 live contract matches.' : n.toLocaleString('en-CA') + ' live contracts match.')
          + (n > LIMIT ? ' Showing the first ' + LIMIT + ', ' + order + '. Add a word or a filter to narrow it.'
                       : ' Sorted by ' + order + '.');
    }

    function draw() {
      out.textContent = '';
      setStatus();
      if (!hits.length) return;

      var wrap = el('div', 'tw');
      var table = el('table');
      var head = el('tr');
      COLS.forEach(function (c, i) {
        var th = el('th', c.cls || null, c.label);
        if (c.key) {
          th.setAttribute('data-s', '');
          th.tabIndex = 0;
          th.setAttribute('role', 'button');
          if (i === sortCol) th.setAttribute('aria-sort', sortDir === 1 ? 'ascending' : 'descending');
          var go = function () {
            if (sortCol === i) { sortDir = -sortDir; } else { sortCol = i; sortDir = 1; }
            draw();
            var again = out.querySelectorAll('th')[i];
            if (again) again.focus();
          };
          th.addEventListener('click', go);
          th.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
          });
        }
        head.appendChild(th);
      });
      table.appendChild(head);

      ordered().slice(0, LIMIT).forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', 'd', r.days + 'd'));
        tr.appendChild(el('td', 'n', money(r.val)));
        var inc = el('td');
        inc.appendChild(link(r.vh, r.v || '—'));
        if (r.ref) {
          inc.appendChild(el('br'));
          var span = el('span', 'ref');
          if (r.src) {
            var s = el('a', null, r.ref);
            s.href = r.src;
            s.title = 'This contract on the Government of Canada contract search';
            span.appendChild(s);
          } else {
            span.textContent = r.ref;
          }
          inc.appendChild(span);
        }
        if (r.res) {
          inc.appendChild(el('br'));
          var f = el('span', 'flag', 'Resource-based');
          f.title = 'The description names a staffing arrangement (TBIPS, TSPS, ProServices or a role level). ' +
                    'Most staffing contracts do not say so and are not marked.';
          inc.appendChild(f);
        }
        if (r.sc) inc.appendChild(el('span', 'scope', r.sc));
        tr.appendChild(inc);
        var dept = el('td', 'd');
        dept.appendChild(link(r.dh, english(r.d)));
        tr.appendChild(dept);
        var cat = el('td', 'd');
        cat.appendChild(link(r.ch, r.c || '—'));
        tr.appendChild(cat);
        tr.appendChild(el('td', 'n d', r.bids === null || r.bids === undefined ? '—' : String(r.bids)));
        var last = el('td');
        last.appendChild(el('span', 'p ' + (DENSITY_CLASS[r.dens] || 'dim'), r.dens || '—'));
        tr.appendChild(last);
        table.appendChild(tr);
      });
      wrap.appendChild(table);
      out.appendChild(wrap);
    }

    function apply() {
      writeAddress();
      var words = norm(state.q).split(' ').filter(Boolean);
      out.textContent = '';
      if (!words.length && !anyFilter()) {
        hits = [];
        chips.hidden = true;
        status.textContent = rows ? 'Type a word or choose a filter.' : '';
        return;
      }
      load().then(function () {
        drawChips();
        hits = rows.filter(function (r) { return passes(r, words); });
        draw();
      }, function () {
        status.textContent = 'The contract list did not load. Try again, or use the browse links above.';
      });
    }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      state.q = input.value.trim();
      apply();
    });

    readAddress();
    syncControls();
    // The option lists need the data, so the file loads with the page here.
    // This is the search page, the one place that file is meant to load.
    load().then(function () {
      fillPickers();
      if (state.q || anyFilter()) apply();
      else status.textContent = 'Type a word or choose a filter.';
    }, function () {
      status.textContent = 'The contract list did not load. Try again, or use the browse links above.';
    });
    if (!state.q && !anyFilter()) input.focus();
  }

  if (header) drawHeaderBox();
  if (contracts) drawContractSearch();
})();
