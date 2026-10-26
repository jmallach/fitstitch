#!/usr/bin/env python3
"""Build the static web frontend into a directory (default: _site/).

The page runs fitstitch in the browser with Pyodide. fitstitch and
garmin-fit-sdk are bundled as wheels next to the page, so the only thing
loaded from elsewhere is the Pyodide runtime itself.

Serve the result with e.g. `python -m http.server -d _site`.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ['index.html', 'app.js', 'app.py', 'style.css']
ASSETS = ['icon.svg', 'apple-touch-icon.png', 'social.png']
SITE = 'https://jmallach.github.io/fitstitch/'
# hreflang codes; Aranese is the site's only Occitan
HREFLANG = {'oc-aranes': 'oc', 'zh': 'zh-Hans', 'zh-hant': 'zh-Hant'}


def _alternates(langs):
    return [('x-default', SITE)] + [(HREFLANG.get(l, l), SITE if l == 'en' else f'{SITE}?lang={l}')
                                    for l in langs]


def _sitemap(langs):
    alts = '\n'.join(f'    <xhtml:link rel="alternate" hreflang="{h}" href="{u.replace("&", "&amp;")}"/>'
                     for h, u in _alternates(langs))
    urls = '\n'.join(f'  <url>\n    <loc>{u}</loc>\n{alts}\n  </url>'
                     for h, u in _alternates(langs) if h != 'x-default')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n' + urls + '\n</urlset>\n')


def build(out):
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    wheels = out / 'wheels'
    wheels.mkdir(parents=True)
    for name in PAGE:
        shutil.copy(ROOT / 'web' / name, out / name)
    shutil.copytree(ROOT / 'web' / 'locales', out / 'locales')
    for name in ASSETS:
        shutil.copy(ROOT / 'docs' / name, out / name)
    langs = ['en'] + sorted(p.stem for p in (out / 'locales').glob('*.json') if p.stem != 'en')
    page = out / 'index.html'
    html = page.read_text().replace('<!-- alternates -->', '\n'.join(
        f'<link rel="alternate" hreflang="{h}" href="{u}">' for h, u in _alternates(langs)))
    page.write_text(html)
    (out / 'sitemap.xml').write_text(_sitemap(langs))
    pip = [sys.executable, '-m', 'pip', '-q', '--disable-pip-version-check']
    subprocess.run(pip + ['wheel', '--no-deps', '-w', str(wheels), str(ROOT)], check=True)
    subprocess.run(pip + ['download', '--no-deps', '--only-binary', ':all:', '-d', str(wheels),
                          'garmin-fit-sdk'], check=True)
    names = sorted(p.name for p in wheels.glob('*.whl'))
    (out / 'wheels.json').write_text(json.dumps(names) + '\n')
    # GitHub Pages lets browsers cache files for 10 minutes. Version
    # everything the page loads with a hash of the build, so a deploy never
    # mixes a new page with an old stylesheet, script or translation.
    digest = hashlib.sha256()
    for f in sorted(p for p in out.rglob('*') if p.is_file() and p.name != 'index.html'):
        digest.update(f.relative_to(out).as_posix().encode() + b'\0' + f.read_bytes())
    version = digest.hexdigest()[:10]
    html = page.read_text()
    for name in ('style.css', 'app.js'):
        html = html.replace(f'"{name}"', f'"{name}?v={version}"')
    page.write_text(html)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('out', nargs='?', default=ROOT / '_site')
    out = build(p.parse_args().out)
    print(f'built {out}')


if __name__ == '__main__':
    main()
