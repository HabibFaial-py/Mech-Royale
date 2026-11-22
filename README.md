# Mech Royale — Python Starter (unchanged bot)

This folder puts the official Python starter files at the repository root, as required by the competition. No bot logic has been modified.

## Local conformance check (Docker not required)

Run from this folder:

```sh
python check/check.py -- python -u example_bot.py
```

Expected: `PASS` after 300 replayed observations. This is protocol validation, not a gameplay test.

## GitHub submission

Initialize a public GitHub repository, commit the files, push to `main`, and submit its repository URL to the Builders - Mech Royale competition. `Dockerfile` is at the repository root. Container runtime mounts the map at `/arena`; the copy under `arena/` is for local development.

Read `protocol.md` for all rules, including the 50 ms decision deadline.
