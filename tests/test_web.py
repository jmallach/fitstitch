"""Drive the web frontend in a headless browser.

Needs playwright and a Chromium (`playwright install chromium`, or a system
one in $CHROMIUM), plus network access for the Pyodide runtime. Skipped
otherwise.
"""
import functools
import http.server
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from fitstitch import fitraw
from fitstitch.merge import load, stitch

from test_stitch import HEADER, T0, make_activity

pw = pytest.importorskip('playwright.sync_api')

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import build_web  # noqa: E402


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    out = build_web.build(tmp_path_factory.mktemp('site') / '_site')
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(out))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}/'
    server.shutdown()


@pytest.fixture(scope='module')
def page(site):
    exe = os.environ.get('CHROMIUM') or None
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=exe)
        except pw.Error:
            system = shutil.which('chromium')
            if exe or not system:
                pytest.skip('no Chromium for playwright')
            browser = p.chromium.launch(executable_path=system)
        page = browser.new_page(locale='en-US')
        errors = []
        page.on('pageerror', lambda e: errors.append(e))
        # anything the Content-Security-Policy blocks is a bug (or a leak)
        page.on('console', lambda m: errors.append(m.text) if 'Content Security Policy' in m.text else None)
        page.csp_errors = errors
        page.goto(site)
        page.wait_for_selector('html[data-ready]', state='attached', timeout=120_000)
        yield page
        assert not errors
        browser.close()


def write(tmp_path, name, msgs):
    p = tmp_path / name
    fitraw.write(p, HEADER, msgs)
    return p


def add(page, paths):
    page.click('#clear') if page.is_visible('#clear') else None
    page.set_input_files('#files', [str(p) for p in paths])


def test_stitch_and_download(page, tmp_path):
    # given out of order, as a user might
    paths = [write(tmp_path, 'second.fit', make_activity(T0 + 100, summary_first=True)),
             write(tmp_path, 'first.fit', make_activity(T0, summary_first=True))]
    add(page, paths)
    with page.expect_download() as dl:
        page.click('#stitch')
        page.wait_for_selector('#download:not([hidden])')
        page.click('#download')
    got = Path(dl.value.path()).read_bytes()

    merged, _, _ = stitch([load(p) for p in paths])
    assert got == fitraw.encode(merged.header, merged.msgs)
    assert dl.value.suggested_filename.startswith('stitched-')
    assert 'sorted by start time' in page.text_content('#notes')
    assert 'Pause between part 1 and 2: 0:00:41' in page.text_content('#notes')
    assert page.locator('#checks li.fail').count() == 0
    assert page.locator('#checks li.ok').count() > 5
    assert page.locator('#table tr').count() == 11


def test_refusal_is_shown(page, tmp_path):
    paths = [write(tmp_path, 'run.fit', make_activity(T0)),
             write(tmp_path, 'ride.fit', make_activity(T0 + 100, sport=2))]
    add(page, paths)
    page.click('#stitch')
    page.wait_for_selector('#error:not([hidden])')
    msg = page.text_content('#error')
    assert 'sport' in msg and '/tmp/in' not in msg
    assert page.is_hidden('#download')

    page.check('#force')
    page.click('#stitch')
    page.wait_for_selector('#download:not([hidden])')
    page.uncheck('#force')


def test_language_switch(page, tmp_path):
    import json
    ca = json.loads((ROOT / 'web' / 'locales' / 'ca.json').read_text())
    paths = [write(tmp_path, 'a.fit', make_activity(T0)),
             write(tmp_path, 'b.fit', make_activity(T0 + 100))]
    add(page, paths)
    page.click('#stitch')
    page.wait_for_selector('#download:not([hidden])')

    page.select_option('#lang', 'ca')
    page.wait_for_selector('html[lang="ca"]', state='attached')
    assert page.inner_text('[data-i18n=result_h]') == ca['ui']['result_h']
    assert page.text_content('#stitch') == ca['ui']['stitch']
    # the existing result is re-rendered: labels, checks and pauses
    assert page.text_content('#table tr:nth-child(2) th') == ca['messages']['Start']
    assert page.text_content('#checks li') == ca['messages']['file structure and CRC are valid']
    assert ca['ui']['pause'].split('{')[0] in page.text_content('#notes')

    # remembered across reloads
    page.reload()
    page.wait_for_selector('html[data-ready]', state='attached', timeout=120_000)
    assert page.eval_on_selector('#lang', 'e => e.value') == 'ca'
    page.select_option('#lang', 'en')
    page.wait_for_selector('html[lang="en"]', state='attached')


def test_right_to_left(page):
    page.select_option('#lang', 'ar')
    page.wait_for_selector('html[lang="ar"]', state='attached')
    assert page.evaluate('document.documentElement.dir') == 'rtl'
    # the language menu sits on the left in RTL, at the reading end of the row
    title = page.locator('h1').bounding_box()
    menu = page.locator('#lang').bounding_box()
    assert menu['x'] < title['x']
    page.select_option('#lang', 'en')
    page.wait_for_selector('html[lang="en"]', state='attached')
    assert page.evaluate('document.documentElement.dir') == 'ltr'


def test_csp_blocks_other_hosts(page):
    # the privacy promise is enforced by the browser: no request can leave
    # for anywhere but the site itself and the Pyodide CDN
    result = page.evaluate("""() => fetch('https://example.com/', {method: 'POST', body: 'data'})
                                   .then(() => 'sent', () => 'blocked')""")
    assert result == 'blocked'
    blocked = [e for e in page.csp_errors if 'example.com' in e]
    assert blocked
    for e in blocked:
        page.csp_errors.remove(e)


def test_trim_one_file(page, tmp_path):
    from fitstitch.trim import trim, suggest
    from test_trim import with_tail
    act = with_tail(n=900, tail=300)  # stood still for the last 5 minutes
    path = write(tmp_path, 'forgot.fit', act.msgs)
    add(page, [path])
    assert page.text_content('#stitch') == 'Trim'
    assert not page.is_visible('#force')  # nothing to force when trimming
    page.click('#stitch')
    page.wait_for_selector('#trim:not([hidden])')
    assert 'remove the last 0:05:00' in page.text_content('#trim-hint')
    assert page.input_value('#t-end') == '0:09:59'
    with page.expect_download() as dl:
        page.click('#do-trim')
        page.wait_for_selector('#download:not([hidden])')
        page.click('#download')
    got = Path(dl.value.path()).read_bytes()
    _, end = suggest(act)
    expected, _ = trim(load(path), None, end)
    assert got == fitraw.encode(expected.header, expected.msgs)
    assert dl.value.suggested_filename.startswith('trimmed-')
    assert page.locator('#checks li.fail').count() == 0
    assert page.text_content('#notes') == 'Removed 0:05:00 from the end.'
    assert page.is_hidden('#trim-result')


def test_trim_drag_and_type(page, tmp_path):
    from test_trim import with_tail
    add(page, [write(tmp_path, 'run.fit', with_tail(n=600, tail=0).msgs)])
    page.click('#stitch')
    page.wait_for_selector('#trim:not([hidden])')
    assert page.input_value('#t-end') == '0:09:59'
    assert page.is_disabled('#do-trim')  # no suggestion: nothing to remove yet
    box = page.locator('#chart').bounding_box()
    # drag the end handle to the middle of the chart
    page.mouse.move(box['x'] + box['width'] - 7, box['y'] + box['height'] / 2)
    page.mouse.down()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2, steps=5)
    page.mouse.up()
    end = page.input_value('#t-end')
    assert '0:04:4' in end or '0:05:0' in end, end
    assert page.is_enabled('#do-trim')
    page.fill('#t-start', '1:00')
    page.press('#t-start', 'Enter')
    assert page.input_value('#t-start') == '0:01:00'
    assert page.text_content('#keep').startswith('Keeping 0:0')


def test_trim_stitched_result(page, tmp_path):
    paths = [write(tmp_path, 'a.fit', make_activity(T0)), write(tmp_path, 'b.fit', make_activity(T0 + 100))]
    add(page, paths)
    page.click('#stitch')
    page.wait_for_selector('#trim-result:not([hidden])')
    page.click('#trim-result')
    page.wait_for_selector('#trim:not([hidden])')
    page.fill('#t-end', '0:02:00')
    page.press('#t-end', 'Enter')
    page.click('#do-trim')
    page.wait_for_selector('#download:not([hidden])')
    assert page.text_content('#download').startswith('Download trimmed-')
    assert page.locator('#checks li.fail').count() == 0
