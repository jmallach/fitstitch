# Suunto Ambit3 Sport, run split in two

A real run accidentally saved halfway, as two activities, exported from
Suunto as FIT. Anonymized with `tools/anonymize_fit.py`: positions within
500 m of the start and end are cleared, the rest of the track is rotated
and moved to a random place, and serial numbers are removed. Everything else is as the
watch wrote it, quirks included:

- no laps, and no serial number in `file_id`;
- `local_timestamp` equals the UTC timestamp, so the time zone is unknown;
- part 1 has Suunto developer fields (recovery time, time in zones…),
  part 2 has none;
- the watch lost the heart-rate strap in part 2: every record repeats
  123 bpm, and its session has no calories or training stress score.

Shared by the project author for testing.
