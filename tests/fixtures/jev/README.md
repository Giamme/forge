# Jev test fixtures

Filenames are `sha256(json.dumps({'state':..,'model':..,'questions':..}, sort_keys=True,
separators=(',',':'))).hexdigest() + '.json'` — the documented fixture-key formula from
`forge_jev.client.ask`. `tests/test_jev.py` recomputes each hash independently (see
`fixture_key()` in that file) rather than importing a private helper, and the derivation
for every committed file here is spelled out next to the test that uses it.

- `5999b389….json` — a full response (noul + choice + score answers) for the state/questions
  pair documented in `ClientFixtureHitTests`.
- `81ab2812….json` — a syntactically valid API response that is missing the required
  `answers` dict, used by the malformed-fixture test.

Most other fixtures (fixture misses, invalid JSON, per-test malformed payloads) are written
into temporary directories inline in the test that needs them, per the task's guidance to
prefer generated fixtures where an on-disk repo fixture isn't genuinely needed.
