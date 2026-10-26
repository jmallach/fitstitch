"""Consistency of the web frontend's translations (web/locales/*.json).

Messages produced by fitstitch (table labels, checks, errors) are translated
in the browser by matching them against English templates. These tests make
sure the templates match what fitstitch actually says, so a reworded message
is noticed, and that every locale translates every string with the same
placeholders and markup.
"""
import json
import re
from pathlib import Path

import pytest

from fitstitch.cli import ROWS
from fitstitch.merge import load, stitch, StitchError
from fitstitch.verify import verify
from fitstitch import fitraw

from test_stitch import HEADER, T0, make_activity

WEB = Path(__file__).resolve().parent.parent / 'web'
LANGS = re.findall(r"^\s+'?([a-z0-9-]+)'?: '", (WEB / 'app.js').read_text().split('const LANGUAGES')[1]
                   .split('};')[0], re.M)
EN = json.loads((WEB / 'locales' / 'en.json').read_text())


def locale(lang):
    return json.loads((WEB / 'locales' / f'{lang}.json').read_text())


def template_regex(template):
    parts = re.split(r'\{(\d+)\}', template)
    return re.compile('^' + ''.join('(.+?)' if i % 2 else re.escape(p)
                                    for i, p in enumerate(parts)) + '$', re.S)


def translatable(message):
    return any(t == message or ('{' in t and template_regex(t).match(message))
               for t in EN['messages'])


def write(tmp_path, name, msgs):
    p = tmp_path / name
    fitraw.write(p, HEADER, msgs)
    return p


def test_menu_matches_files():
    files = sorted(p.stem for p in (WEB / 'locales').glob('*.json'))
    assert sorted(LANGS) == files
    assert LANGS[0] == 'en'


def test_table_labels_translatable():
    for label, _ in ROWS:
        assert label in EN['messages']


def test_checks_translatable(tmp_path):
    paths = [write(tmp_path, 'a.fit', make_activity(T0)),
             write(tmp_path, 'b.fit', make_activity(T0 + 100))]
    acts = [load(p) for p in paths]
    merged, ordered, _ = stitch(acts)
    checks = verify(fitraw.encode(merged.header, merged.msgs), ordered)
    assert len(checks) > 15
    for _, desc in checks:
        assert translatable(desc), desc


@pytest.mark.parametrize('case', ['one', 'sport', 'device', 'overlap', 'not_activity'])
def test_errors_translatable(tmp_path, case):
    a = make_activity(T0)
    b = {'sport': make_activity(T0 + 100, sport=2),
         'device': make_activity(T0 + 100, serial=99),
         'overlap': make_activity(T0 + 30)}.get(case, make_activity(T0 + 100))
    if case == 'not_activity':
        a[0].set_raw('type', 6)
    with pytest.raises(StitchError) as e:
        paths = [write(tmp_path, 'a.fit', a)] + ([] if case == 'one' else [write(tmp_path, 'b.fit', b)])
        stitch([load(p) for p in paths])
    assert translatable(str(e.value)), str(e.value)


def _markup(text):
    return sorted(re.findall(r'<[^>]+>', text))


def _placeholders(text):
    return sorted(re.findall(r'\{\d+\}', text))


@pytest.mark.parametrize('lang', LANGS)
def test_locale_complete(lang):
    loc = locale(lang)
    assert set(loc) == {'ui', 'messages'}
    for section in ('ui', 'messages'):
        assert set(loc[section]) == set(EN[section]), f'{section}: keys differ from en.json'
        for key, text in loc[section].items():
            assert text.strip(), f'{section}.{key} is empty'
            assert _placeholders(text) == _placeholders(EN[section][key]), f'{section}.{key}'
            assert _markup(text) == _markup(EN[section][key]), f'{section}.{key}'
    # translations of templates must keep every placeholder of the template itself
    for template, text in loc['messages'].items():
        assert _placeholders(text) == _placeholders(template), template
