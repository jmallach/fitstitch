# Contributing

Bug reports and pull requests are welcome.

## Running the tests

```
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python tools/fetch_corpus.py      # public FIT files used by tests/test_corpus.py
.venv/bin/python tools/fetch_fitcsvtool.py  # optional: Garmin's validator, needs Java
.venv/bin/pytest
```

`pytest` prints whether FitCSVTool validation is enabled in its header (run
it without `-q` to see the header).

## Sample files

The most useful bug reports include the FIT files that triggered the problem.
FIT files from devices or apps not yet covered by the corpus are especially
welcome, above all real activities that were accidentally split in two.

**Activity files are personal data.** Besides your GPS track, they usually
contain your name, weight, birth year, heart-rate zones and device serial
numbers. Only share files you are comfortable making public, or ask in the
issue for a private way to send them.

To test with your own files without committing them, put each case in its
own directory under `samples/` (git-ignored), e.g. `samples/my-split-run/`
holding the two parts. `tests/test_stitch.py` stitches and verifies every
such directory.

## Translations

The web page's strings live in `web/locales/<lang>.json`, with
`web/locales/en.json` as the source. Corrections from native speakers are
very welcome: the initial translations were machine-made.

- `ui` holds the page's strings. Keep the `{0}` placeholders and the HTML
  markup (`<em>`, `<a href=…>`…) as they are.
- `messages` translates the English messages fitstitch itself produces
  (table labels, checks, refusals). The keys are matching templates: copy
  them unchanged and translate only the values.
- To add a language, copy `en.json`, translate it and add the language's
  native name to `LANGUAGES` in `web/app.js`. For a right-to-left language,
  also add it to `RTL` there, and turn the `→` in the export instructions
  into `←`.

`tests/test_locales.py` checks that every locale has every string with the
same placeholders and markup, and that the templates still match what
fitstitch says.
