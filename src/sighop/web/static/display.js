/* The panel's display conventions in the browser (web-display, design D5).
 *
 * Four jobs, all of them over markup the server already wrote correctly
 * without it — nothing here is required for a page to work:
 *
 *  1. **Timestamps.** Every `<time class="when" datetime="…">` arrives as
 *     compact UTC. This rewrites it relative to now ("3 min ago", "in 5 min")
 *     in the viewer's own clock, puts the local date-time and the exact UTC
 *     value on hover, and keeps it current: every 30 s, and on content HTMX
 *     swaps in (a refreshed partial arrives with the server's text again).
 *  2. **Copying a key.** A `.copy` button carries the full key in `data-copy` —
 *     or, beside a received channel message, its paths, one per line.
 *     Clicks are delegated from the document, so swapped-in rows need no
 *     rebinding. The clipboard is written where the browser allows it; where it
 *     does not — a panel served over plain HTTP is in no secure context, which
 *     is the deployment this one is actually served in — an off-screen textarea
 *     is copied from instead. Only if both fail is the key put up for copying by
 *     hand, in a holder positioned out of the flow. The abbreviation beside the
 *     control is never written over, so no column ever changes width
 *     (`web-display`).
 *  3. **Counting a composed message.** A composer's `<textarea data-limit>`
 *     carries the byte limit the server refuses against, and a channel post
 *     also carries the select whose identity name rides inside that limit. The
 *     count is in bytes, because the limit is. It never disables the send
 *     control: the refusal at submission is the authority, and a control
 *     disabled by a mis-count is a panel that cannot send.
 *  4. **Sending from the keyboard.** Ctrl+Enter or Cmd+Enter in a composer
 *     submits the form the send control submits — `requestSubmit`, so nothing
 *     the button would carry is skipped. Enter alone still inserts a newline.
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

  /* Tier two: a textarea off the left edge, copied from and removed again. It
     is what a page without a secure context has instead of a clipboard API.
     Positioned rather than `display: none`, which cannot be selected from. */
  function copyOffScreen(full) {
    var area = document.createElement("textarea");
    area.value = full;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "0";
    area.style.left = "-9999px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    var copied = false;
    try {
      area.select();
      area.setSelectionRange(0, full.length);
      copied = document.execCommand("copy");
    } catch (error) {
      copied = false;
    }
    document.body.removeChild(area);
    return copied;
  }

  var offered = null;

  /* Back to the abbreviation once the operator has moved on. */
  function withdraw() {
    if (!offered) { return; }
    offered.hidden = true;
    offered.textContent = "";
    offered = null;
  }

  /* Tier three: the key itself, selected, for copying by hand. It goes in the
     `.key-full` holder the macro leaves empty, which is positioned out of the
     flow — writing the full key over the abbreviation is what used to widen
     every column holding one. */
  function offerByHand(button, full) {
    var holder = button.parentNode && button.parentNode.querySelector(".key-full");
    if (!holder) { return; }
    withdraw();
    holder.textContent = full;
    holder.hidden = false;
    offered = holder;
    var range = document.createRange();
    range.selectNodeContents(holder);
    var selection = window.getSelection();
    if (!selection) { return; }
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function fallback(button, full) {
    if (copyOffScreen(full)) { confirm(button); return; }
    offerByHand(button, full);
  }

  function copy(button) {
    var full = button.getAttribute("data-copy") || "";
    if (window.isSecureContext && navigator.clipboard) {
      navigator.clipboard.writeText(full).then(
        function () { confirm(button); },
        function () { fallback(button, full); }
      );
      return;
    }
    fallback(button, full);
  }

  document.addEventListener("click", function (event) {
    var within = event.target.closest ? event.target : null;
    var button = within && within.closest("button.copy");
    if (!button) {
      if (offered && !(within && within.closest(".key-full"))) { withdraw(); }
      return;
    }
    event.preventDefault();
    copy(button);
  });

  /* --- Counting a composed message ---------------------------------------- */

  var encoder = window.TextEncoder ? new TextEncoder() : null;

  function bytes(text) {
    if (encoder) { return encoder.encode(text).length; }
    /* No TextEncoder is no count; the refusal at submission still applies. */
    return null;
  }

  /* What rides inside the limit besides the text. For a channel post that is
     the selected identity's name and the separator, exactly as
     `check_post_length` counts it; for a direct message it is nothing. */
  function prefix(area) {
    var from = area.getAttribute("data-prefix-from");
    if (!from) { return ""; }
    var form = area.form;
    var select = form && form.elements ? form.elements[from] : null;
    if (!select || select.selectedIndex < 0) { return ""; }
    var option = select.options[select.selectedIndex];
    if (!option || !option.value) { return ""; }
    return option.text + (area.getAttribute("data-prefix-separator") || "");
  }

  function count(area) {
    var readout = area.parentNode && area.parentNode.querySelector(".count");
    if (!readout) { return; }
    var limit = parseInt(area.getAttribute("data-limit"), 10);
    var used = bytes(prefix(area) + area.value);
    if (!limit || used === null) { readout.textContent = ""; return; }
    if (used > limit) {
      readout.textContent = used + " / " + limit + " bytes — " + (used - limit) + " over";
      readout.classList.add("over");
      return;
    }
    readout.textContent = used + " / " + limit + " bytes";
    readout.classList.remove("over");
  }

  function counters(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var areas = scope.querySelectorAll("textarea[data-limit]");
    for (var i = 0; i < areas.length; i++) { count(areas[i]); }
  }

  document.addEventListener("input", function (event) {
    var area = event.target;
    if (area && area.matches && area.matches("textarea[data-limit]")) { count(area); }
  });

  /* The identity a post is made as changes how much of the limit the text has
     left, so the count follows the select as well as the text. */
  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || !select.form || !select.form.querySelectorAll) { return; }
    counters(select.form);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { withdraw(); }
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) { return; }
    var area = event.target;
    if (!area || !area.matches || !area.matches(".composer textarea")) { return; }
    var form = area.form;
    if (!form) { return; }
    event.preventDefault();
    /* `requestSubmit`, not `submit`: the latter skips validation and the submit
       event, so a form would go out by a different route than the button's. */
    if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
  });

  /* The whole document rather than the event's target: an `outerHTML` swap
     replaces the element the event was raised on, and a page holds a few
     dozen timestamps at most. */
  document.addEventListener("htmx:afterSwap", function () {
    render(document);
    counters(document);
  });

  function start() { render(document); counters(document); }

  if (document.readyState !== "loading") { start(); } else {
    document.addEventListener("DOMContentLoaded", start);
  }
  setInterval(function () { render(document); }, TICK_MS);

  window.sighopDisplay = { when: when, relative: relative, hover: hover, render: render };
})();
