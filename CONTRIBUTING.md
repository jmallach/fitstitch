# Contributing

Bug reports and pull requests are welcome.

## Running the tests

```
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python tools/fetch_corpus.py   # public FIT files used by tests/test_corpus.py
.venv/bin/pytest
```

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
