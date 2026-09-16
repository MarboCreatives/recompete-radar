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

    var status = el('p', 'sb srch-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    var out = el('div');

    contracts.appendChild(form);
    contracts.appendChild(status);
    contracts.appendChild(out);

    var rows = null, loading = null;
    function load() {
      if (!loading) {
        status.textContent = 'Loading contracts…';
        loading = getJSON('search-contracts.json').then(function (j) {
          // Row: ref, supplier index, department index, category index,
          // value, days to expiry, buyer org code, description, bidders,
          // how contested it was last time.
          // Lookup entry: [text, link].
          var S = j.suppliers, D = j.departments, C = j.categories;
          rows = j.contracts.map(function (c) {
            var v = S[c[1]], d = D[c[2]], k = C[c[3]];
            return { ref: c[0], v: v[0], vh: v[1], d: d[0], dh: d[1], c: k[0], ch: k[1],
                     val: c[4], days: c[5], src: sourceUrl(c[6], c[0]), sc: c[7] || '',
                     bids: c[8], dens: c[9] || '',
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

    // Sorting, the same as the tables on every other page: Expires, Value,
    // Bidders and Last time. A first press sorts low to high, the next high to
    // low. Rows with no value stay at the bottom both ways. The WHOLE match
    // set is sorted, then the first LIMIT rows are drawn, so "high to low by
    // value" shows the largest matches and not the largest of the soonest 200.
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
          + (n > LIMIT ? ' Showing the first ' + LIMIT + ', ' + order + '. Add a word to narrow it.'
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

    function run(q) {
      var words = norm(q).split(' ').filter(Boolean);
      out.textContent = '';
      if (!words.length) { status.textContent = ''; hits = []; return; }
      load().then(function () {
        // Every word must start a word somewhere in the reference number,
        // supplier, department, category or description.
        hits = rows.filter(function (r) {
          for (var i = 0; i < words.length; i++) {
            if (r.k.indexOf(' ' + words[i]) < 0) return false;
          }
          return true;
        });
        draw();
      }, function () {
        status.textContent = 'The contract list did not load. Try again, or use the browse links above.';
      });
    }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var q = input.value.trim();
      if (window.history && history.replaceState) {
        history.replaceState(null, '', q ? '?q=' + encodeURIComponent(q) : location.pathname);
      }
      run(q);
    });

    var start = '';
    var m = /[?&]q=([^&#]*)/.exec(location.search);
    if (m) {
      try { start = decodeURIComponent(m[1].replace(/\+/g, ' ')); } catch (e) { start = ''; }
    }
    if (start) {
      input.value = start;
      run(start);
    } else {
      input.focus();
    }
  }

  if (header) drawHeaderBox();
  if (contracts) drawContractSearch();
})();
