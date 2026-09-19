/* The live packet feed's client (design D3).
 *
 * About a hundred lines by hand, and it is the only genuinely streaming element
 * on the panel — which is exactly why HTMX alone was not enough and why there
 * is no bundler here to have produced this.
 *
 * Three things it has to get right:
 *
 *  1. The **boundary**. Recorded history and live records are different claims,
 *     and a feed that blurred them would be lying about the present. The server
 *     sends a `boundary` message; this draws a rule for it.
 *  2. The **incomplete state**. A connection that fell behind lost its oldest
 *     records, and the server says how many. That is shown, not swallowed: a
 *     feed missing records must never look like a quiet mesh.
 *  3. **Bounded memory.** The browser keeps a fixed number of rows. A tab left
 *     open overnight is not a memory leak.
 */
(function () {
  "use strict";

  var MAX_ROWS = 500;

  function ready(fn) {
    if (document.readyState !== "loading") { fn(); } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  ready(function () {
    var table = document.getElementById("feed-rows");
    if (!table) { return; }

    var status = document.getElementById("feed-status");
    var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/feed";
    var socket = new WebSocket(url);

    function setStatus(text, level) {
      if (!status) { return; }
      status.textContent = text;
      status.className = "feed-status feed-" + (level || "ok");
    }

    function trim() {
      while (table.rows.length > MAX_ROWS) {
        table.deleteRow(table.rows.length - 1);
      }
    }

    function cell(row, text, className) {
      var td = row.insertCell();
      td.textContent = text === null || text === undefined ? "—" : String(text);
      if (className) { td.className = className; }
      if (text === null || text === undefined) { td.classList.add("absent"); }
      return td;
    }

    /* Newest at the top: what a watcher wants is the present. */
    function draw(record) {
      var row = table.insertRow(0);
      row.className = "feed-row feed-" + record.source + " dir-" + record.direction;
      if (record.duplicate) { row.classList.add("feed-duplicate"); }

      cell(row, (record.at || "").slice(11, 23), "mono");
      cell(row, record.direction.toUpperCase(), "dir-" + record.direction);
      cell(row, record.route_type, "mono");
      cell(row, record.payload_type, "mono");
      cell(row, record.size_bytes, "num");
      cell(row, record.path || "", "mono");
      cell(row, record.path_len, "num");
      cell(row, record.snr_db, "num");
      cell(row, record.rssi_dbm, "num");
      cell(row, record.duplicate ? "dup" : "", "mono");
      cell(row, record.outcome, "mono");
      /* An undecodable frame keeps its bytes and its reason: a frame we could
         not read must not become invisible (§4.1). A decoded frame shows its
         summary where it has one (node discovery), live rows only. */
      cell(row, record.reason || record.summary || (record.raw ? "raw " + record.raw : ""), "mono");
      cell(row, record.packet_id, "mono");
      trim();
    }

    function boundary(message) {
      var row = table.insertRow(0);
      row.className = "feed-boundary";
      var td = row.insertCell();
      td.colSpan = 13;
      td.textContent =
        "— live from here (" + message.painted + " recorded record(s) below) —";
    }

    socket.addEventListener("open", function () {
      setStatus("connected", "ok");
    });

    socket.addEventListener("message", function (event) {
      var message = JSON.parse(event.data);
      if (message.kind === "history") {
        /* Oldest last: the server sends newest first, and drawing in order
           puts them below the boundary in the same order. */
        for (var i = message.records.length - 1; i >= 0; i--) { draw(message.records[i]); }
        if (message.note) { setStatus(message.note, "warn"); }
      } else if (message.kind === "boundary") {
        boundary(message);
      } else if (message.kind === "records") {
        for (var j = 0; j < message.records.length; j++) { draw(message.records[j]); }
      } else if (message.kind === "status") {
        if (message.incomplete) {
          setStatus(
            "feed incomplete — " + message.dropped +
              " record(s) lost because this browser could not keep up",
            "alarm"
          );
        } else {
          setStatus("live — " + message.delivered + " record(s) shown", "ok");
        }
      }
    });

    socket.addEventListener("close", function () {
      setStatus("feed disconnected — reload to reconnect", "warn");
    });

    socket.addEventListener("error", function () {
      setStatus("feed connection failed", "alarm");
    });
  });
})();
