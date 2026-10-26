'use strict';

const $ = (id) => document.getElementById(id);
// The build appends ?v=<build hash> to this script; reuse it for everything
// else the page fetches, so cached copies from an older deploy are not used.
const VERSION = new URL(document.currentScript.src).search;
let files = [];
let run = null;
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
    const py = pyodide.globals;
    run = (paths, force) => JSON.parse(py.get('run')(pyodide.toPy(paths), force));
    $('version').textContent = py.get('version')();
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
  const btn = $('stitch');
  if (state !== 'ready') {
    btn.textContent = t(state);
    return;
  }
  btn.disabled = files.length < 2;
  btn.textContent = t('stitch');
  $('status').textContent = files.length === 1 ? t('one_more') : '';
}

function addFiles(list) {
  for (const f of list) {
    if (!files.some((g) => g.name === f.name && g.size === f.size)) files.push(f);
  }
  $('result').hidden = true;
  lastResult = null;
  update();
}

async function stitch() {
  const btn = $('stitch');
  btn.disabled = true;
  btn.textContent = t('stitching');
  // let the browser repaint before the (synchronous) Python work starts
  await new Promise((r) => setTimeout(r, 30));
  const FS = window.fitstitch.pyodide.FS;
  try {
    const dir = '/tmp/in';
    FS.mkdirTree(dir);
    for (const name of FS.readdir(dir)) if (name !== '.' && name !== '..') FS.unlink(`${dir}/${name}`);
    const paths = [];
    for (const [i, f] of files.entries()) {
      const path = `${dir}/${i}-${f.name}`;
      FS.writeFile(path, new Uint8Array(await f.arrayBuffer()));
      paths.push(path);
    }
    const res = run(paths, $('force').checked);
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
  head.append(cell('th', ''), ...res.parts.map((_, i) => cell('th', t('part', i + 1))),
    cell('th', t('stitched')));
  const rows = res.rows.map(([label, ...vals]) => {
    const tr = document.createElement('tr');
    tr.append(cell('th', tm(label)), ...vals.map((v) => cell('td', v)));
    return tr;
  });
  $('table').replaceChildren(head, ...rows);

  const notes = res.gaps.map((g, i) => t('pause', i + 1, i + 2, g));
  if (res.reordered) notes.unshift(t('sorted', res.parts.map((p, i) => `${i + 1}. ${p}`).join(', ')));
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
  } else {
    a.hidden = true;
    $('warnings').prepend(cell('li', t('verify_failed')));
  }
  if (scroll) $('result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('files').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });
$('stitch').addEventListener('click', stitch);
$('clear').addEventListener('click', () => {
  files = [];
  lastResult = null;
  $('result').hidden = true;
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
