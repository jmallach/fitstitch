# fitstitch

Stitch FIT activities that were accidentally split (stopped and saved
halfway) back into one, offline and without losing data. The result is the
activity you would have had if you had pressed pause instead of saving.

```
$ fit-stitch part1.zip part2.zip -o run.fit
                               Part 1               Part 2             Stitched
Start             2026-10-25 12:39:12  2026-10-25 13:43:54  2026-10-25 12:39:12
Distance                      7.27 km              4.19 km             11.46 km
Timer time                    0:53:51              0:26:08              1:19:59
Elapsed time                  1:04:03              0:27:12              1:31:54
...
Pause between part 1 and 2: 0:00:39

  [ok] file structure and CRC are valid
  [ok] Garmin FIT SDK decodes the file without errors
  [ok] all 16929 data messages preserved
  ...
```

## Use it in your browser

**<https://jmallach.github.io/fitstitch/>** runs the same code in your
browser, with nothing to install, on phones too. Your files are not
uploaded: the page runs fitstitch locally with
[Pyodide](https://pyodide.org) (Python compiled to WebAssembly). It is
available in 33 languages, from Arabic and Hindi to Catalan and
Japanese; corrections are welcome (see
[CONTRIBUTING.md](CONTRIBUTING.md#translations)).

## Install

```
pipx install git+https://github.com/jmallach/fitstitch
```

or, from a checkout, `pip install .`.

Requires Python 3.9+ and the official `garmin-fit-sdk` package. That package
is used for field names and units, and to check the output independently.

## Usage

```
fit-stitch PART1 PART2 [PART3...] [-o OUTPUT] [-n] [-f] [--overwrite]
```

- Inputs are `.fit` files or the `.zip` files Garmin Connect produces with
  *Export Original*. You can give them in any order; they are sorted by
  start time.
- `-n/--dry-run` shows the result and the checks without writing anything.
- `-f/--force` stitches parts even if the sport or the recording device
  differ.
- If the output fails any check, the tool writes nothing and exits with
  status 2.

### Replacing the activity on Garmin Connect and Strava

1. Export each part from the Garmin Connect website (activity → ⚙ →
   *Export Original*) or the Strava website (activity → ⋯ → *Export
   Original*). The phone apps can't export files; in a phone's browser,
   ask for the desktop site if it keeps opening the app.
2. Run `fit-stitch`, or use the web page.
3. **Delete the original parts** on Garmin Connect and on Strava. Both
   reject an upload with the same start time as an existing activity as a
   duplicate. Kudos, comments, photos, titles and gear on the parts are lost.
4. Upload the stitched file in Garmin Connect (*Import Data*). If it doesn't
   reach Strava on its own, upload it at strava.com/upload/select.

## What it does

The FIT file is read and written byte by byte. Every message and field is
copied as it was, including undocumented Garmin messages and developer
(Connect IQ) fields. The tool only changes fields whose meaning it knows.

- **Data streams**: records, GPS metadata, events, sensor and device info,
  HRV, battery and ClimbPro data are kept from all parts. Accumulating
  record fields (distance, accumulated power, cycles) continue from the end
  of the previous part instead of restarting at zero.
- **The gap** between parts becomes a pause. The timer stop/start events
  that mark it are added if the files lack them.
- **Session**: totals are added up; averages are weighted by timer time
  (speed is total distance / timer time). Maximums, minimums, the bounding
  box and normalized power (4th-power weighted mean) are combined.
  Elapsed time runs from the first start to the last end.
- **Laps** are kept as recorded and renumbered. The last lap before a gap
  absorbs the pause, as a real pause inside a lap would: its elapsed time
  grows and its timer time stays the same. This keeps laps contiguous and
  their elapsed times adding up to the session's, as Garmin's validator
  requires.
- **Splits** (run/walk/stand detection, intervals) are renumbered. The
  whole-activity interval split and the per-type split summaries are
  merged.
- **Time in zone** (HR/power/speed/cadence) is added up for the session and
  re-linked to the renumbered laps and splits.
- **Best efforts** (fastest 1 km, 1 mile, 5 km… as recorded by the watch):
  the fastest of the parts is kept.
- **Setup messages** (user profile, device settings, sport, zones, course):
  the first part's copy is kept.
- **Layout**: the first part's message order is followed. Newer Garmin
  watches write summaries before the data; older devices write them after.

## Limitations

- **Values calculated on the watch** (Training Effect, training load,
  recovery time, VO2max update) come from Garmin's proprietary algorithms and
  cannot be recalculated. The tool keeps the higher Training Effect of the
  parts and adds up training load, which leaves your weekly load unchanged.
- **Auto laps** restart at the beginning of each part, so the lap before the
  gap is a partial one, as if you had pressed the lap button.
- **Data during the gap** was never recorded and cannot be recovered.
- Multisport activities and pool swimming (length messages) are refused.
- GPX/TCX input is not supported: those formats drop most of the data.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python tools/fetch_corpus.py      # public FIT files
.venv/bin/python tools/fetch_fitcsvtool.py  # optional: Garmin's validator (needs Java)
.venv/bin/pytest
```

The tests work at three levels:

- `tests/test_stitch.py` builds synthetic FIT files. It covers both file
  layouts, three or more parts, compressed timestamps, developer fields,
  missing timer events, time in zone and the error cases.
- `tests/test_corpus.py` runs on about 45 public FIT files (from Garmin,
  Wahoo, Coros, Zwift and others, plus deliberately broken files) taken from
  the test suites of [fitdecode](https://github.com/polyvertex/fitdecode),
  [python-fitparse](https://github.com/dtcooper/python-fitparse) and the
  official Garmin SDKs. Every file must either round-trip byte for byte or
  be refused with a clear error. Every activity is cut into halves and into
  thirds with `fit-split` and stitched back, and the result must reproduce
  the original data stream exactly.
- Real activities in `samples/<case>/` (two or more parts per directory, not
  distributed) are stitched and verified too. See
  [CONTRIBUTING.md](CONTRIBUTING.md).

Every stitched file in the tests is also checked by independent decoders:

- **fitdecode** must decode every message and field value exactly as
  fitstitch's own reader does.
- **Garmin's FitCSVTool** runs its activity file validation (`-t`), the
  checks Garmin applies to activity files. Stitching must not introduce a
  failure the input files did not already have. This check runs when
  `tools/.cache/FitCSVTool.jar` (or `$FITCSVTOOL_JAR`) and Java are
  available, as they are in CI. FitCSVTool is published by Garmin under its
  [FIT Protocol License](https://github.com/garmin/fit-sdk-tools/blob/main/LICENSE.txt)
  and is downloaded, never redistributed.

The tool's own output checks include the same summary rules as Garmin's
validator, with the same tolerances: laps must be contiguous, and lap
elapsed and timer times must add up to the session's.

The web frontend lives in `web/`. `tools/build_web.py` bundles it with
wheels of fitstitch and garmin-fit-sdk into `_site/` (serve it with
`python -m http.server -d _site`), and GitHub Pages publishes it on every
push to `main`. `tests/test_web.py` drives the page in headless Chromium
and checks that it produces the same file as the Python API; it needs
`pip install playwright && playwright install chromium` and is skipped
otherwise.

`fit-split FILE 0.5` cuts an activity into parts as if it had been stopped
and restarted. It is meant for testing.

## License

MIT. See [LICENSE](LICENSE).
