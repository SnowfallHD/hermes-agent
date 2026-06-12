(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardContent, Button, Badge } = SDK.components;
  const { useCallback, useEffect, useMemo, useRef, useState } = SDK.hooks;
  const { timeAgo, cn } = SDK.utils;
  const API = "/api/plugins/thoughts";
  const LS_BOARD_KEY = "hermes.kanban.selectedBoard";

  function selectedBoard() {
    try {
      const value = window.localStorage.getItem(LS_BOARD_KEY);
      return (value || "").trim();
    } catch (_e) {
      return "";
    }
  }

  function withBoard(url) {
    const board = selectedBoard();
    if (!board) return url;
    const sep = url.indexOf("?") >= 0 ? "&" : "?";
    return `${url}${sep}board=${encodeURIComponent(board)}`;
  }


  function entryTime(entry) {
    const value = entry && entry.created_at;
    if (typeof value === "number") return value * 1000;
    const parsed = Date.parse(value || "");
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function sortEntries(entries) {
    return (entries || []).slice().sort(function (a, b) {
      const byTime = entryTime(b) - entryTime(a);
      if (byTime !== 0) return byTime;
      return String(b.id || "").localeCompare(String(a.id || ""));
    });
  }

  function dedupeAppend(prev, incoming) {
    const byId = new Map();
    for (const entry of prev || []) byId.set(String(entry.id), entry);
    for (const entry of incoming || []) byId.set(String(entry.id), entry);
    return sortEntries(Array.from(byId.values())).slice(0, 300);
  }

  function ThoughtsPage() {
    const [entries, setEntries] = useState([]);
    const [latestEventId, setLatestEventId] = useState(0);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);
    const [live, setLive] = useState(true);
    const [connected, setConnected] = useState(false);
    const [filter, setFilter] = useState("all");
    const [board, setBoard] = useState(selectedBoard());
    const wsRef = useRef(null);

    const load = useCallback(function () {
      setLoading(true);
      return SDK.fetchJSON(withBoard(`${API}/thoughts?limit=160`))
        .then(function (data) {
          setEntries(sortEntries(data.entries || []));
          setLatestEventId(data.latest_event_id || 0);
          setErr(null);
        })
        .catch(function (e) { setErr(String(e.message || e)); })
        .finally(function () { setLoading(false); });
    }, [board]);

    useEffect(function () { load(); }, [load]);

    useEffect(function () {
      function syncBoard() { setBoard(selectedBoard()); }
      window.addEventListener("storage", syncBoard);
      window.addEventListener("focus", syncBoard);
      return function () {
        window.removeEventListener("storage", syncBoard);
        window.removeEventListener("focus", syncBoard);
      };
    }, []);

    useEffect(function () {
      if (!live) {
        setConnected(false);
        if (wsRef.current) {
          wsRef.current.close();
          wsRef.current = null;
        }
        return;
      }

      let stopped = false;
      let retryTimer = null;

      function connect() {
        if (stopped) return;
        const wsParams = { cursor: String(latestEventId || 0), limit: "100" };
        const activeBoard = selectedBoard();
        if (activeBoard) wsParams.board = activeBoard;
        SDK.buildWsUrl(`${API}/events`, wsParams).then(function (url) {
          if (stopped) return;
          let ws;
          try { ws = new WebSocket(url); } catch (_e) { return; }
          wsRef.current = ws;
          ws.onopen = function () { if (!stopped) setConnected(true); };
          ws.onmessage = function (ev) {
            try {
              const data = JSON.parse(ev.data);
              const incoming = data.entries || [];
              if (incoming.length) {
                setEntries(function (prev) { return dedupeAppend(prev, incoming); });
                setLatestEventId(data.latest_event_id || 0);
              }
            } catch (_e) { /* ignore malformed frames */ }
          };
          ws.onerror = function () { setConnected(false); };
          ws.onclose = function () {
            setConnected(false);
            if (!stopped) retryTimer = setTimeout(connect, 1500);
          };
        }).catch(function () {
          setConnected(false);
          if (!stopped) retryTimer = setTimeout(connect, 1500);
        });
      }

      connect();
      return function () {
        stopped = true;
        if (retryTimer) clearTimeout(retryTimer);
        if (wsRef.current) wsRef.current.close();
        wsRef.current = null;
      };
    }, [live, board]);

    const newest = entries[0];
    const filters = ["all", "mind", "kanban", "cron", "revenue", "self_improvement", "uncertainty", "decision"];
    const grouped = useMemo(function () {
      if (filter === "all") return entries;
      return entries.filter(function (entry) {
        return entry.category === filter || entry.source === filter || entry.kind === filter || entry.event_type === filter || entry.priority === filter;
      });
    }, [entries, filter]);

    return h("div", { className: "hermes-thoughts-page" },
      h("div", { className: "hermes-thoughts-header" },
        h("div", null,
          h("div", { className: "hermes-thoughts-title" }, "Thoughts"),
          h("div", { className: "hermes-thoughts-subtitle" },
            "Unified Mind/Event feed: explicit observations, uncertainties, decisions, routes, crons, and Kanban activity — not raw chain-of-thought."),
          board ? h("div", { className: "hermes-thoughts-board" }, "Board: ", board) : null,
        ),
        h("div", { className: "hermes-thoughts-actions" },
          h(Badge, { variant: connected ? "default" : "secondary" }, connected ? "live" : (live ? "reconnecting" : "paused")),
          h(Button, { size: "sm", variant: "outline", onClick: function () { setLive(function (v) { return !v; }); } }, live ? "Pause" : "Resume"),
          h(Button, { size: "sm", onClick: load }, "Refresh"),
        ),
      ),
      h("div", { className: "hermes-thoughts-filters", role: "radiogroup", "aria-label": "Thought filters" },
        filters.map(function (item) {
          const active = filter === item;
          return h("button", {
            key: item,
            type: "button",
            role: "radio",
            "aria-checked": active,
            className: cn("hermes-thought-filter", active && "hermes-thought-filter--active"),
            onClick: function () { setFilter(item); },
          }, item.replace(/_/g, " ").toUpperCase());
        })
      ),
      newest ? h(Card, { className: "hermes-thoughts-now" },
        h(CardContent, { className: "hermes-thoughts-now-content" },
          h("div", { className: "hermes-thoughts-now-label" }, "Current signal"),
          h("div", { className: "hermes-thoughts-now-text" }, newest.thought),
          h("div", { className: "hermes-thoughts-now-meta" },
            newest.task_id ? `${newest.source || "mind"} · ${newest.task_id} · ${newest.kind}` : `${newest.source || "mind"} · ${newest.kind}`,
            " · ",
            timeAgo ? timeAgo(newest.created_at) : "",
          ),
        ),
      ) : null,
      err ? h("div", { className: "hermes-thoughts-error" }, err) : null,
      loading ? h("div", { className: "hermes-thoughts-muted" }, "Loading thoughts…") : null,
      !loading && grouped.length === 0 ? h("div", { className: "hermes-thoughts-muted" },
        entries.length === 0
          ? "No thoughts yet. Mind events, cron observations, metacognitive decisions, or Kanban updates will appear here."
          : "No thoughts match this filter yet.") : null,
      h("div", { className: "hermes-thoughts-list" },
        grouped.map(function (entry) {
          return h("div", { key: entry.id, className: cn("hermes-thought-row", `hermes-thought-row--${entry.kind}`) },
            h("div", { className: "hermes-thought-dot" }),
            h("div", { className: "hermes-thought-body" },
              h("div", { className: "hermes-thought-line" }, entry.thought),
              h("div", { className: "hermes-thought-meta" },
                h("span", null, entry.source || "mind"),
                entry.task_id ? h("span", null, " · ", entry.task_id) : null,
                entry.task_title ? h("span", null, " · ", entry.task_title) : null,
                h("span", null, " · ", entry.event_type || entry.kind),
                entry.confidence_label ? h("span", null, " · ", entry.confidence_label) : null,
                entry.urgency ? h("span", null, " · ", entry.urgency) : null,
                h("span", null, " · ", timeAgo ? timeAgo(entry.created_at) : ""),
              ),
              entry.why_it_matters ? h("div", { className: "hermes-thought-why" }, entry.why_it_matters) : null,
              entry.next_best_action ? h("div", { className: "hermes-thought-next" }, "Next: ", entry.next_best_action.replace(/_/g, " ")) : null,
            ),
          );
        })
      ),
    );
  }

  if (window.__HERMES_PLUGINS__ && window.__HERMES_PLUGINS__.register) {
    window.__HERMES_PLUGINS__.register("thoughts", ThoughtsPage);
  }
})();
