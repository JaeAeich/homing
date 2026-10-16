# Samples from the live GitHub target

Real runs against a private scratch repo (`make bootstrap`), recorded with
`make verify-live`. Account identifiers are redacted by homing itself; this
folder was leak-scanned against every value in `.env`.

## `success/`: a fresh run (no saved state)

- `output.txt`: the CLI summary (`homing runs show RUN_ID`). Full login through
  the TOTP provider, then all seven stages.
- `manifest.json`: inputs, pages visited, stage timings, resolver wins, files
  with sha256, session mode, provenance.
- `files/v0.1.0-statement.pdf`: the release asset, saved through a native
  download. The second document, the draft printed to PDF, is listed in the
  manifest but not included: it is a print of the account's own page and shows
  the username.

## `failure/`: a broken step (`make break`)

The release-title locator was changed to something that does not exist. The run
stops at `fill_form` with `LOCATOR_NOT_FOUND` and writes the evidence bundle:

- `evidence/screenshot.png` (header and avatar masked), `page.html`, `url.txt`,
  `console.jsonl`, `stage.txt`, `resolver_attempts.json`, `error.json`.
- `output.txt` and `manifest.json`, with the exact `homing resume` command.

The screenshot also shows the dependent field: the new tag is set, so the
"Previous tag" picker has appeared.
