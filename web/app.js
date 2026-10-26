'use strict';

const $ = (id) => document.getElementById(id);
let files = [];
let run = null;
let downloadUrl = null;

async function init() {
  try {
    const pyodide = await loadPyodide();
    await pyodide.loadPackage('micropip');
    const wheels = await (await fetch('wheels.json')).json();
    const base = new URL('wheels/', location.href);
    await pyodide.pyimport('micropip').install(wheels.map((w) => new URL(w, base).href));
    pyodide.runPython(await (await fetch('app.py')).text());
    const py = pyodide.globals;
    run = (paths, force) => JSON.parse(py.get('run')(pyodide.toPy(paths), force));
    $('version').textContent = py.get('version')();
    window.fitstitch = { pyodide, ready: true };
    update();
  } catch (e) {
    $('stitch').textContent = 'Unavailable';
    $('status').textContent = `Could not load the Python runtime: ${e}`;
    console.error(e);
  }
}

function update() {
  const list = $('filelist');
  list.replaceChildren(...files.map((f) => {
    const li = document.createElement('li');
    li.textContent = `${f.name} (${(f.size / 1024).toFixed(0)} KB)`;
    return li;
  }));
  $('clear').hidden = files.length === 0;
  const btn = $('stitch');
  if (!run) return;
  btn.disabled = files.length < 2;
  btn.textContent = 'Stitch';
  $('status').textContent = files.length === 1 ? 'Add at least one more part.' : '';
}

function addFiles(list) {
  for (const f of list) {
    if (!files.some((g) => g.name === f.name && g.size === f.size)) files.push(f);
  }
  $('result').hidden = true;
  update();
}

async function stitch() {
  const btn = $('stitch');
  btn.disabled = true;
  btn.textContent = 'Stitching…';
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
    show({ error: `Unexpected failure: ${e}` });
    console.error(e);
  }
  update();
}

function cell(tag, text) {
  const el = document.createElement(tag);
  el.textContent = text;
  return el;
}

function show(res, data) {
  $('result').hidden = false;
  $('error').hidden = !res.error;
  $('report').hidden = !!res.error;
  if (res.error) {
    $('error').textContent = res.error;
    return;
  }
  const head = document.createElement('tr');
  head.append(cell('th', ''), ...res.columns.map((c) => cell('th', c)));
  const rows = res.rows.map(([label, ...vals]) => {
    const tr = document.createElement('tr');
    tr.append(cell('th', label), ...vals.map((v) => cell('td', v)));
    return tr;
  });
  $('table').replaceChildren(head, ...rows);

  const notes = res.gaps.map((g, i) => `Pause between part ${i + 1} and ${i + 2}: ${g}.`);
  if (res.reordered) notes.unshift(`Parts sorted by start time: ${res.parts.map((p, i) => `${i + 1}. ${p}`).join(', ')}.`);
  $('notes').textContent = notes.join(' ');

  $('checks').replaceChildren(...res.checks.map(([ok, desc]) => {
    const li = cell('li', desc);
    li.className = ok ? 'ok' : 'fail';
    return li;
  }));
  $('warnings').replaceChildren(...res.warnings.map((w) => cell('li', w)));

  const a = $('download');
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  downloadUrl = null;
  if (data) {
    downloadUrl = URL.createObjectURL(new Blob([data], { type: 'application/octet-stream' }));
    a.href = downloadUrl;
    a.download = res.filename;
    a.textContent = `Download ${res.filename}`;
    a.hidden = false;
  } else {
    a.hidden = true;
    $('warnings').prepend(cell('li', 'Verification failed, so no file was produced. Please report this as a bug.'));
  }
  $('result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('files').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });
$('stitch').addEventListener('click', stitch);
$('clear').addEventListener('click', () => { files = []; $('result').hidden = true; update(); });
const drop = $('drop');
for (const ev of ['dragenter', 'dragover']) {
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); });
}
for (const ev of ['dragleave', 'drop']) {
  drop.addEventListener(ev, () => drop.classList.remove('over'));
}
drop.addEventListener('drop', (e) => { e.preventDefault(); addFiles(e.dataTransfer.files); });

init();
