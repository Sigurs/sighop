# Vendored assets

Everything a page of this interface loads is served from this directory. There
is no CDN reference anywhere in `templates/` and no build step that would fetch
one: DESIGN.md §10's image is read-only with no assumed network egress, so an
asset that has to be fetched at page load is an interface that breaks in the
deployment it was built for (design D3).

`tests/test_web_assets.py` asserts both halves of that — no external origin in
any template, and every `src`/`href` a template names resolving to a file here.

## `htmx.min.js`

- **Version**: 2.0.4
- **Source**: <https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js>
- **Upstream**: <https://github.com/bigskysoftware/htmx> (tag `v2.0.4`)
- **SHA-256**: `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`
- **License**: Zero-Clause BSD (0BSD), reproduced below from the tag's `LICENSE`.

```
Zero-Clause BSD
===============

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

To update: replace the file, update the version, URL and hash above, and run the
asset tests. Nothing else in the project depends on the version.

## Everything else here

`panel.css` and `feed.js` are this project's own, written by hand. `feed.js` is
the WebSocket client for the live packet feed — the one genuinely streaming
element on the panel, and the reason HTMX alone was not enough (design D3).
