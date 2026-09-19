/* The panel's display conventions in the browser (web-display, design D5).
 *
 * Two jobs, both over markup the server already wrote correctly without it:
 *
 *  1. **Timestamps.** Every `<time class="when" datetime="…">` arrives as
 *     compact UTC. This rewrites it relative to now ("3 min ago", "in 5 min")
 *     in the viewer's own clock, puts the local date-time and the exact UTC
 *     value on hover, and keeps it current: every 30 s, and on content HTMX
 *     swaps in (a refreshed partial arrives with the server's text again).
 *  2. **Copying a key.** A `.copy` button carries the full key in `data-copy`.
 *     Clicks are delegated from the document, so swapped-in rows need no
 *     rebinding. Without a secure context there is no clipboard, so the full
 *     key is put in place of the abbreviation and selected for copying by hand.
 */
(function () {
  "use strict";

  var TICK_MS = 30000;

  function relative(date, now) {
    var seconds = Math.round(((now || Date.now()) - date.getTime()) / 1000);
    var future = seconds < 0;
    var abs = Math.abs(seconds);
    var text;
    if (abs < 45) { return "just now"; }
    if (abs < 3600) {
      text = Math.max(1, Math.round(abs / 60)) + " min";
    } else if (abs < 86400) {
      text = Math.round(abs / 3600) + " h";
    } else if (abs < 30 * 86400) {
      text = Math.round(abs / 86400) + " d";
    } else {
      return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    }
    return future ? "in " + text : text + " ago";
  }

  function hover(date, iso) {
    return date.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short"
    }) + "\n" + iso;
  }

  function when(element) {
    var iso = element.getAttribute("datetime");
    var date = iso ? new Date(iso) : null;
    if (!date || isNaN(date.getTime())) { return; }
    element.textContent = relative(date);
    element.title = hover(date, iso);
  }

  function render(root) {
    var scope = root && root.querySelectorAll ? root : document;
    if (scope.matches && scope.matches("time.when")) { when(scope); }
    var found = scope.querySelectorAll("time.when");
    for (var i = 0; i < found.length; i++) { when(found[i]); }
  }

  function confirm(button) {
    var original = button.textContent;
    button.textContent = "✓";
    button.classList.add("copied");
    setTimeout(function () {
      button.textContent = original;
      button.classList.remove("copied");
    }, 1500);
  }

  function selectByHand(button, full) {
    var code = button.parentNode && button.parentNode.querySelector("code");
    if (!code) { return; }
    code.textContent = full;
    var range = document.createRange();
    range.selectNodeContents(code);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function copy(button) {
    var full = button.getAttribute("data-copy") || "";
    if (window.isSecureContext && navigator.clipboard) {
      navigator.clipboard.writeText(full).then(
        function () { confirm(button); },
        function () { selectByHand(button, full); }
      );
    } else {
      selectByHand(button, full);
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("button.copy");
    if (!button) { return; }
    event.preventDefault();
    copy(button);
  });

  /* The whole document rather than the event's target: an `outerHTML` swap
     replaces the element the event was raised on, and a page holds a few
     dozen timestamps at most. */
  document.addEventListener("htmx:afterSwap", function () { render(document); });

  if (document.readyState !== "loading") { render(document); } else {
    document.addEventListener("DOMContentLoaded", function () { render(document); });
  }
  setInterval(function () { render(document); }, TICK_MS);

  window.sighopDisplay = { when: when, relative: relative, hover: hover, render: render };
})();
