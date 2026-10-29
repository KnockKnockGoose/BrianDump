# Contributing

Thanks for looking. This is a one-person alpha, so small and clear beats big and clever.

- **Issues first.** For anything bigger than a typo, open an issue before a PR. Say what you tried to do, what you said to it, and what happened.
- **Set up:** `py -m pip install -r requirements-dev.txt -r requirements.txt`. The page test also needs a browser: `py -m playwright install chromium`.
- **Test:** `py -m pytest tests -q`. **Lint:** `py -m ruff check .` must pass. The code is formatted by hand in a compact 140-column style, so please don't run a formatter over whole files in a PR.
- **Tests make no model calls, press no keys and open no microphone.** Use the fakes and the fixtures in `tests/` and `briandump/fixtures/`. Mark tests that need Windows `@pytest.mark.win32` (CI runs them on Windows only) and slow ones `@pytest.mark.slow`.
- **Keep it Windows-safe:** `pathlib` paths, explicit `encoding="utf-8"`, subprocess argument lists (never a shell string), parsers that accept CRLF. The server and the page should keep running on other systems, even though the hotkey, window and console code is Win32.
- **Keep the keystroke rule.** Nothing new may turn text into keystrokes except the owner's own inbox files (see `SECURITY.md`).
- **Small PRs**, one change each, with a line under `Unreleased` in `CHANGELOG.md`.
- **Nothing personal in the repo:** no real transcripts, recordings or names in fixtures, screenshots or logs. Media is real screen captures only.
- By contributing you agree that your work is released under the MIT license, like the rest.
