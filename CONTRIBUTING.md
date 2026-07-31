# Contributing to Freedom

Thanks for considering contributing! This is a fun side project, not a product — contributions are welcome, but expectations are set accordingly.

## Ground rules

- **Be patient.** This is a side project maintained in spare time. Responses may be slow.
- **The built-in providers are brittle.** They scrape undocumented websites that change without warning and **will** break. If a provider breaks, the best contribution is often a **plugin** — not a patch to a scraper.
- **The plugin system is the stable part.** Prefer writing a new provider plugin over modifying the app's core scrapers.
- **License.** This project is GPL-3.0. By contributing, you agree your contributions are licensed under GPL-3.0.

## Getting started

1. **Fork** the repo and clone it.
2. **Install dependencies** — see the [README](README.md) → Getting Started.
3. **Create a branch** for your change:

   ```bash
   git checkout -b fix/my-change
   ```

4. **Make your changes** and test them:

   ```bash
   python3 -m anime_watch plugin validate user_providers/myprovider.py
   ./anime-watch
   ```

5. **Commit** with a clear message:

   ```bash
   git commit -m "describe the change"
   ```

6. **Push** and open a pull request.

## Pull request checklist

- [ ] Tested the change (search / stream / download / torrent).
- [ ] No secrets, cookies, or personal data added.
- [ ] New provider plugins live in `user_providers/`, not in `anime_watch/providers/`.
- [ ] No whitespace-only or unrelated changes.

## Reporting bugs

Open an issue with:

- What you searched and which provider/category was active.
- The error output (if any).
- Whether it's reproducible on a fresh request.

> Search can occasionally return no results — try searching again before filing a bug.

## Feature requests

Open an issue describing the problem you're solving, not just a solution. Ideas are welcome, but the plugin system is the preferred extension point for new providers.
