'use strict';

const $ = (id) => document.getElementById(id);
// The build appends ?v=<build hash> to this script; reuse it for everything
// else the page fetches, so cached copies from an older deploy are not used.
const VERSION = new URL(document.currentScript.src).search;
let files = [];
let run = null;
let py = null;
let mode = 'stitch'; // what the last result is: 'stitch' or 'trim'
let downloadUrl = null;
let lastResult = null;
let lastData = null;
let state = 'loading'; // loading | ready | unavailable

// Native names, in the order shown in the language menu.
const LANGUAGES = {
  en: 'English',
  es: 'Español (España)',
  'es-419': 'Español (Latinoamérica)',
  ca: 'Català',
  gl: 'Galego',
  eu: 'Euskara',
  pt: 'Português (Portugal)',
  'pt-br': 'Português (Brasil)',
  ast: 'Asturianu',
  an: 'Aragonés',
  'oc-aranes': 'Aranés',
  de: 'Deutsch',
  fr: 'Français',
  it: 'Italiano',
  nl: 'Nederlands',
  pl: 'Polski',
  cs: 'Čeština',
  da: 'Dansk',
  nb: 'Norsk bokmål',
  sv: 'Svenska',
  ru: 'Русский',
  zh: '中文（简体）',
  'zh-hant': '中文（繁體）',
  ja: '日本語',
  ko: '한국어',
  vi: 'Tiếng Việt',
  th: 'ไทย',
  id: 'Bahasa Indonesia',
  ms: 'Bahasa Melayu',
  fil: 'Filipino',
  hi: 'हिन्दी',
  bn: 'বাংলা',
  ta: 'தமிழ்',
  ur: 'اردو',
  ar: 'العربية',
  fa: 'فارسی',
  tr: 'Türkçe',
  sw: 'Kiswahili',
};
const RTL = new Set(['ar', 'fa', 'ur']);

// Browser language tags to use for a language without its own entry.
const ALIASES = {
  oc: 'oc-aranes',
  tl: 'fil',
  no: 'nb',
  nn: 'nb',
  'zh-tw': 'zh-hant',
  'zh-hk': 'zh-hant',
  'zh-mo': 'zh-hant',
  'zh-hant-tw': 'zh-hant',
  'zh-hant-hk': 'zh-hant',
  'zh-hant-mo': 'zh-hant',
};

let english = null;
let locale = null;

function format(text, args) {
  // In right-to-left languages, keep each value (numbers, file names,
  // durations) and a unit written after it as one left-to-right run.
  const rtl = RTL.has(document.documentElement.lang);
  return text.replace(/\{(\d+)\}(?: (m|s|kcal)\b)?/g, (m, i, unit) => {
    if (args[i] === undefined) return m;
    const value = unit ? `${args[i]}\u00a0${unit}` : `${args[i]}`;
    return rtl ? `\u2066${value}\u2069` : value;
  });
}

function t(key, ...args) {
  const text = (locale && locale.ui[key]) || english.ui[key] || key;
  return format(text, args);
}

// Messages produced by fitstitch itself (table labels, checks, errors) are
// English; translate them by matching the templates in the locale files.
const templateCache = new Map();
function templateRegex(template) {
  if (!templateCache.has(template)) {
    const escaped = template.replace(/[.*+?^$()|[\]\\]/g, '\\$&');
    const parts = escaped.split(/\\?\{(\d+)\\?\}/);
    let src = '';
    for (let i = 0; i < parts.length; i++) src += i % 2 ? '(.+?)' : parts[i].replace(/[{}]/g, '\\$&');
    templateCache.set(template, new RegExp(`^${src}$`, 's'));
  }
  return templateCache.get(template);
}

function tm(message) {
  for (const table of [locale && locale.messages, english.messages]) {
    if (!table) continue;
    if (table[message] !== undefined) return table[message];
    for (const [template, text] of Object.entries(table)) {
      if (!template.includes('{')) continue;
      const m = templateRegex(template).exec(message);
      if (m) return format(text, m.slice(1));
    }
  }
  return message;
}

async function loadLocale(lang) {
  const res = await fetch(`locales/${lang}.json${VERSION}`);
  if (!res.ok) throw new Error(`locales/${lang}.json: ${res.status}`);
  return res.json();
}

function pickLanguage() {
  const wanted = [new URLSearchParams(location.search).get('lang')];
  try { wanted.push(localStorage.getItem('fitstitch-lang')); } catch (e) { /* no storage */ }
  wanted.push(...(navigator.languages || [navigator.language]));
  for (const w of wanted) {
    if (!w) continue;
    const tag = w.toLowerCase();
    const base = tag.split('-')[0];
    // Spanish from anywhere but Spain gets the Latin American variant
    if (base === 'es' && tag !== 'es' && tag !== 'es-es') return 'es-419';
    for (const code of [tag, ALIASES[tag], base, ALIASES[base]]) if (code && LANGUAGES[code]) return code;
  }
  return 'en';
}

async function setLanguage(lang) {
  if (!english) english = await loadLocale('en');
  try {
    locale = lang === 'en' ? english : await loadLocale(lang);
  } catch (e) {
    console.error(e);
    lang = 'en';
    locale = english;
  }
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL.has(lang) ? 'rtl' : 'ltr';
  $('lang').value = lang;
  for (const el of document.querySelectorAll('[data-i18n]')) el.innerHTML = t(el.dataset.i18n);
  for (const el of document.querySelectorAll('[data-i18n-label]')) {
    el.setAttribute('aria-label', t(el.dataset.i18nLabel));
  }
  document.querySelector('meta[name=description]').content = t('description');
  document.title = `fitstitch – ${t('tagline')}`;
  const canonical = new URL(location.pathname, location.origin);
  if (lang !== 'en') canonical.searchParams.set('lang', lang);
  document.querySelector('link[rel=canonical]').href = canonical.href;
  update();
  if (lastResult) show(lastResult, lastData, false);
  if (!$('trim').hidden) { trimHint(); drawTrim(); }
}

async function init() {
  const menu = $('lang');
  for (const [code, name] of Object.entries(LANGUAGES)) menu.append(new Option(name, code));
  menu.addEventListener('change', () => {
    try { localStorage.setItem('fitstitch-lang', menu.value); } catch (e) { /* no storage */ }
    // keep the address shareable in the chosen language
    const url = new URL(location.href);
    url.searchParams.set('lang', menu.value);
    history.replaceState(null, '', url);
    setLanguage(menu.value);
  });
  await setLanguage(pickLanguage());

  try {
    const pyodide = await loadPyodide();
    await pyodide.loadPackage('micropip');
    const wheels = await (await fetch(`wheels.json${VERSION}`)).json();
    const base = new URL('wheels/', location.href);
    await pyodide.pyimport('micropip').install(wheels.map((w) => new URL(w, base).href));
    pyodide.runPython(await (await fetch(`app.py${VERSION}`)).text());
    const g = pyodide.globals;
    run = (paths, force) => JSON.parse(g.get('run')(pyodide.toPy(paths), force));
    py = {
      prepare: (path) => JSON.parse(g.get('prepare')(path)),
      trim: (path, start, end) => JSON.parse(g.get('run_trim')(path, start, end)),
    };
    $('version').textContent = g.get('version')();
    window.fitstitch = { pyodide, ready: true };
    document.documentElement.dataset.ready = '';
    state = 'ready';
  } catch (e) {
    state = 'unavailable';
    $('status').textContent = t('load_failed', e);
    console.error(e);
  }
  update();
}

function update() {
  const list = $('filelist');
  list.replaceChildren(...files.map((f) => {
    const li = document.createElement('li');
    li.dir = 'ltr';
    li.textContent = `${f.name} (${(f.size / 1024).toFixed(0)} KB)`;
    return li;
  }));
  $('clear').hidden = files.length === 0;
  // only stitching has anything to force
  $('force').parentElement.hidden = files.length === 1;
  const btn = $('stitch');
  if (state !== 'ready') {
    btn.textContent = t(state);
    return;
  }
  btn.disabled = files.length === 0;
  btn.textContent = t(files.length === 1 ? 'trim_open' : 'stitch');
  $('status').textContent = files.length === 1 ? t('one_file') : '';
}

function addFiles(list) {
  for (const f of list) {
    if (!files.some((g) => g.name === f.name && g.size === f.size)) files.push(f);
  }
  $('result').hidden = true;
  $('trim').hidden = true;
  lastResult = null;
  update();
}

// let the browser repaint before (synchronous) Python work starts
const repaint = () => new Promise((r) => setTimeout(r, 30));

async function writeInputs() {
  const FS = window.fitstitch.pyodide.FS;
  const dir = '/tmp/in';
  FS.mkdirTree(dir);
  for (const name of FS.readdir(dir)) if (name !== '.' && name !== '..') FS.unlink(`${dir}/${name}`);
  const paths = [];
  for (const [i, f] of files.entries()) {
    const path = `${dir}/${i}-${f.name}`;
    FS.writeFile(path, new Uint8Array(await f.arrayBuffer()));
    paths.push(path);
  }
  return paths;
}

async function stitch() {
  if (files.length === 1) {
    const [path] = await writeInputs();
    openTrim(path);
    return;
  }
  const btn = $('stitch');
  btn.disabled = true;
  btn.textContent = t('stitching');
  await repaint();
  const FS = window.fitstitch.pyodide.FS;
  try {
    const res = run(await writeInputs(), $('force').checked);
    mode = 'stitch';
    $('trim').hidden = true;
    show(res, res.ok ? FS.readFile('/tmp/stitched.fit') : null);
  } catch (e) {
    show({ error: t('unexpected', e), translated: true });
    console.error(e);
  }
  update();
}

function cell(tag, text) {
  const el = document.createElement(tag);
  el.textContent = text;
  return el;
}

function show(res, data, scroll = true) {
  lastResult = res;
  lastData = data;
  $('result').hidden = false;
  $('error').hidden = !res.error;
  $('report').hidden = !!res.error;
  if (res.error) {
    $('error').textContent = res.translated ? res.error : tm(res.error);
    return;
  }
  const head = document.createElement('tr');
  const cols = res.removed ? [t('original'), t('trimmed')]
    : [...res.parts.map((_, i) => t('part', i + 1)), t('stitched')];
  head.append(cell('th', ''), ...cols.map((c) => cell('th', c)));
  const rows = res.rows.map(([label, ...vals]) => {
    const tr = document.createElement('tr');
    tr.append(cell('th', tm(label)), ...vals.map((v) => cell('td', v)));
    return tr;
  });
  $('table').replaceChildren(head, ...rows);

  let notes;
  if (res.removed) {
    const [a, b] = res.removed;
    const zero = (d) => /^0:00:0[0-9]$/.test(d);
    notes = [zero(a) ? t('removed_end', b) : zero(b) ? t('removed_start', a) : t('removed', a, b)];
  } else {
    notes = res.gaps.map((g, i) => t('pause', i + 1, i + 2, g));
    if (res.reordered) notes.unshift(t('sorted', res.parts.map((p, i) => `${i + 1}. ${p}`).join(', ')));
  }
  $('notes').textContent = notes.join(' ');

  $('checks').replaceChildren(...res.checks.map(([ok, desc]) => {
    const li = cell('li', tm(desc));
    li.className = ok ? 'ok' : 'fail';
    return li;
  }));
  $('warnings').replaceChildren(...res.warnings.map((w) => cell('li', tm(w))));

  const a = $('download');
  if (data) {
    if (!downloadUrl || scroll) {
      if (downloadUrl) URL.revokeObjectURL(downloadUrl);
      downloadUrl = URL.createObjectURL(new Blob([data], { type: 'application/octet-stream' }));
    }
    a.href = downloadUrl;
    a.download = res.filename;
    a.textContent = t('download', res.filename);
    a.hidden = false;
    $('trim-result').hidden = !!res.removed;
  } else {
    a.hidden = true;
    $('trim-result').hidden = true;
    const failed = cell('li', t('verify_failed'));
    failed.className = 'fail';
    $('warnings').prepend(failed);
  }
  if (scroll) $('result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---- trimming ----------------------------------------------------------

const trimState = { path: null, data: null, start: 0, end: 0, drag: null, map: false };
const tiles = new Map();

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function dur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

function parseDur(text) {
  const parts = text.trim().split(':').map(Number);
  if (!parts.length || parts.length > 3 || parts.some((x) => !Number.isFinite(x) || x < 0)) return null;
  return parts.reduce((acc, x) => acc * 60 + x, 0);
}

function openTrim(path) {
  const data = py.prepare(path);
  mode = 'trim';
  $('result').hidden = true;
  if (data.error) {
    show({ error: data.error });
    return;
  }
  Object.assign(trimState, { path, data, start: data.suggest[0] ?? 0, end: data.suggest[1] ?? data.duration });
  $('trim').hidden = false;
  $('track-box').hidden = !data.track;
  trimHint();
  drawTrim();
  $('trim').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function trimHint() {
  const { data } = trimState;
  if (!data) return;
  const [s, e] = data.suggest;
  let hint = t('trim_hint_none');
  if (s !== null && e !== null) hint = t('trim_hint_both', dur(s), dur(data.duration - e));
  else if (s !== null) hint = t('trim_hint_start', dur(s));
  else if (e !== null) hint = t('trim_hint_end', dur(data.duration - e));
  $('trim-hint').textContent = hint;
}

function valueAt(series, time) {
  const { t: ts } = trimState.data;
  let best = null;
  for (let i = 0; i < ts.length && ts[i] <= time; i++) if (series[i] !== null) best = series[i];
  return best;
}

function fitCanvas(c) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(c.clientWidth * dpr);
  const h = Math.round(c.clientHeight * dpr);
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  return { ctx: c.getContext('2d'), w, h, dpr };
}

function drawTrim() {
  const { data, start, end } = trimState;
  if (!data) return;
  $('t-start').value = dur(start);
  $('t-end').value = dur(end);
  const d0 = valueAt(data.distance, start);
  const d1 = valueAt(data.distance, end);
  $('keep').textContent = d1 !== null
    ? t('keep', dur(end - start), ((d1 - (d0 || 0)) / 1000).toFixed(2))
    : t('keep_time', dur(end - start));
  // nothing to do until a handle has moved
  $('do-trim').disabled = !(start > 0.5 || end < data.duration - 0.5);
  drawChart();
  if (data.track) drawTrack();
}

function drawChart() {
  const { data, start, end } = trimState;
  const { ctx, w, h, dpr } = fitCanvas($('chart'));
  const pad = 6 * dpr;
  const x = (time) => pad + (time / data.duration) * (w - 2 * pad);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = css('--border');
  for (const [a, b] of data.pauses) ctx.fillRect(x(a), 0, Math.max(1, x(b) - x(a)), h);
  const line = (series, color) => {
    const vals = series.filter((v) => v !== null);
    if (!vals.length) return;
    const sorted = [...vals].sort((a, b) => a - b);
    const lo = sorted[0];
    const hi = sorted[Math.floor(sorted.length * 0.99)] || 1;
    const y = (v) => h - pad - ((Math.min(v, hi) - lo) / ((hi - lo) || 1)) * (h - 2 * pad);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath();
    let pen = false;
    series.forEach((v, i) => {
      if (v === null) { pen = false; return; }
      if (pen) ctx.lineTo(x(data.t[i]), y(v)); else ctx.moveTo(x(data.t[i]), y(v));
      pen = true;
    });
    ctx.stroke();
  };
  line(data.hr, css('--hr'));
  line(data.speed, css('--accent'));
  // dim what is trimmed away
  ctx.fillStyle = css('--bg');
  ctx.globalAlpha = 0.7;
  ctx.fillRect(0, 0, x(start), h);
  ctx.fillRect(x(end), 0, w - x(end), h);
  ctx.globalAlpha = 1;
  for (const time of [start, end]) {
    ctx.fillStyle = css('--fg');
    ctx.fillRect(x(time) - dpr, 0, 2 * dpr, h);
    ctx.beginPath();
    ctx.arc(x(time), h / 2, 6 * dpr, 0, 2 * Math.PI);
    ctx.fill();
  }
}

function mercator([lat, lon]) {
  const s = Math.sin((lat * Math.PI) / 180);
  return [(lon + 180) / 360, 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)];
}

function drawTrack() {
  const { data, start, end } = trimState;
  const canvas = $('track');
  const { ctx, w, h, dpr } = fitCanvas(canvas);
  const pts = data.track.map((p) => (p ? mercator(p) : null));
  const valid = pts.filter(Boolean);
  const xs = valid.map((p) => p[0]);
  const ys = valid.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const pad = 16 * dpr;
  let scale = Math.min((w - 2 * pad) / ((x1 - x0) || 1e-9), (h - 2 * pad) / ((y1 - y0) || 1e-9));
  let zoom = null;
  if (trimState.map) {
    // the largest tile zoom at which the track still fits
    zoom = Math.max(0, Math.min(17, Math.floor(Math.log2(scale / 256))));
    scale = 256 * 2 ** zoom;
  }
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  const px = (p) => [w / 2 + (p[0] - cx) * scale, h / 2 + (p[1] - cy) * scale];
  ctx.clearRect(0, 0, w, h);
  if (zoom !== null) {
    const n = 2 ** zoom;
    const tx0 = Math.floor((cx - w / 2 / scale) * n);
    const tx1 = Math.floor((cx + w / 2 / scale) * n);
    const ty0 = Math.floor((cy - h / 2 / scale) * n);
    const ty1 = Math.floor((cy + h / 2 / scale) * n);
    for (let tx = tx0; tx <= tx1; tx++) {
      for (let ty = Math.max(0, ty0); ty <= Math.min(n - 1, ty1); ty++) {
        const key = `${zoom}/${((tx % n) + n) % n}/${ty}`;
        let img = tiles.get(key);
        if (!img) {
          img = new Image();
          img.onload = () => drawTrack();
          img.src = `https://tile.openstreetmap.org/${key}.png`;
          tiles.set(key, img);
        }
        if (img.complete && img.naturalWidth) {
          const [ix, iy] = px([tx / n, ty / n]);
          ctx.drawImage(img, ix, iy, scale / n + 1, scale / n + 1);
        }
      }
    }
  }
  const path = (from, to, color, width) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width * dpr;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    let pen = false;
    pts.forEach((p, i) => {
      const time = data.t[i];
      if (time < from || time > to) { pen = false; return; }
      if (!p) return; // no fix in this record: join the ones around it
      const [a, b] = px(p);
      if (pen) ctx.lineTo(a, b); else ctx.moveTo(a, b);
      pen = true;
    });
    ctx.stroke();
  };
  path(0, data.duration, css('--muted'), 2);
  path(start, end, css('--accent'), 4);
  // orange start, teal end, as in the icon
  for (const [time, color] of [[start, '#ff8a3d'], [end, '#3ccfb4']]) {
    let i = data.t.findIndex((tt, j) => tt >= time && pts[j]);
    if (i < 0) i = pts.length - 1;
    while (i > 0 && !pts[i]) i--;
    if (!pts[i]) continue;
    const [a, b] = px(pts[i]);
    ctx.fillStyle = color;
    ctx.strokeStyle = css('--card');
    ctx.lineWidth = 2 * dpr;
    ctx.beginPath();
    ctx.arc(a, b, 7 * dpr, 0, 2 * Math.PI);
    ctx.fill();
    ctx.stroke();
  }
}

function chartTime(e) {
  const c = $('chart');
  const r = c.getBoundingClientRect();
  const pad = 6;
  const f = (e.clientX - r.left - pad) / (r.width - 2 * pad);
  return Math.max(0, Math.min(1, f)) * trimState.data.duration;
}

function setBound(which, time) {
  const { data } = trimState;
  if (which === 'start') trimState.start = Math.max(0, Math.min(time, trimState.end - 1));
  else trimState.end = Math.min(data.duration, Math.max(time, trimState.start + 1));
  drawTrim();
}

$('chart').addEventListener('pointerdown', (e) => {
  if (!trimState.data) return;
  const time = chartTime(e);
  trimState.drag = Math.abs(time - trimState.start) <= Math.abs(time - trimState.end) ? 'start' : 'end';
  $('chart').setPointerCapture(e.pointerId);
  setBound(trimState.drag, time);
});
$('chart').addEventListener('pointermove', (e) => {
  if (trimState.drag) setBound(trimState.drag, chartTime(e));
});
for (const ev of ['pointerup', 'pointercancel']) {
  $('chart').addEventListener(ev, () => { trimState.drag = null; });
}
for (const [id, which] of [['t-start', 'start'], ['t-end', 'end']]) {
  $(id).addEventListener('change', (e) => {
    const time = parseDur(e.target.value);
    if (time === null) drawTrim(); else setBound(which, time);
  });
}
$('map').addEventListener('change', (e) => {
  trimState.map = e.target.checked;
  $('map-credit').hidden = !trimState.map;
  if (trimState.data) drawTrack();
});
window.addEventListener('resize', () => { if (!$('trim').hidden) drawTrim(); });

$('do-trim').addEventListener('click', async () => {
  const btn = $('do-trim');
  const { data, path } = trimState;
  btn.disabled = true;
  btn.textContent = t('trimming');
  await repaint();
  try {
    // undefined (not null) reaches Python as None
    const start = trimState.start > 0.5 ? trimState.start : undefined;
    const end = trimState.end < data.duration - 0.5 ? trimState.end : undefined;
    const res = py.trim(path, start, end);
    mode = 'trim';
    show(res, res.ok ? window.fitstitch.pyodide.FS.readFile('/tmp/trimmed.fit') : null);
  } catch (e) {
    show({ error: t('unexpected', e), translated: true });
    console.error(e);
  }
  btn.textContent = t('trim');
  drawTrim();
});
$('trim-result').addEventListener('click', () => openTrim('/tmp/stitched.fit'));

$('files').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });
$('stitch').addEventListener('click', stitch);
$('clear').addEventListener('click', () => {
  files = [];
  lastResult = null;
  $('result').hidden = true;
  $('trim').hidden = true;
  update();
});
const drop = $('drop');
for (const ev of ['dragenter', 'dragover']) {
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); });
}
for (const ev of ['dragleave', 'drop']) {
  drop.addEventListener(ev, () => drop.classList.remove('over'));
}
drop.addEventListener('drop', (e) => { e.preventDefault(); addFiles(e.dataTransfer.files); });

init();
