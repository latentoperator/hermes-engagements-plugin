// Engagements tab for the Hermes web dashboard: the same read-only Clients view as the
// Desktop page, served by this plugin's dashboard/plugin_api.py (SPEC 6.7). Nothing here
// writes, and it has no composer: the starter prompts on an empty page are copied for the
// user to paste into a chat. React and the UI kit come from window.__HERMES_PLUGIN_SDK__; no build step.
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useEffect, useState } = SDK.hooks;
  const { Badge, Button, Card, CardContent, CardHeader, CardTitle, Input } = SDK.components;
  const h = React.createElement;
  const API = "/api/plugins/engagements";
  const PRIVACY_KEY = "hermes.plugin.engagements.privacy";
  const HIDDEN_MESSAGE = "Details are hidden while privacy mode is on.";

  // Starter prompts for an empty Clients page (spec 6.7, decision D12). Each names the bundled skill so the
  // agent picks it up, and still reads as a plain request if the skill is not loaded. Neither
  // prompt carries user data, and the web dashboard has no composer: it copies the text instead.
  const SUGGEST_PROMPT =
    "Look through my past Hermes chats for clients or recurring projects I have worked on. " +
    "Use the engagements:suggest-clients skill. Suggest up to five worth adding, each with one " +
    "line on why and which chats it came from. Do not save anything yet; let me pick. If you find " +
    "none, help me add one client instead.";
  const ADD_PROMPT =
    "Help me add a client to Engagements. Use the engagements:onboard-client skill. Ask me only what you need.";
  const STORY = "Find clients from chats you have already had, or add one now.";

  function mask(text, hidden) {
    if (!hidden || !text) return text;
    return String(text).split(/\s+/).filter(Boolean).map(function (w) { return w[0].toUpperCase() + "."; }).join(" ");
  }

  // A sentence from the backend can carry an absolute path (the missing-library message
  // does) or an id. With privacy on the page shows a fixed sentence instead of trying to
  // redact parts of it, so nothing identifying reaches the screen.
  function shown(text, hidden) {
    if (hidden) return HIDDEN_MESSAGE;
    return text;
  }

  function useJSON(path, tick) {
    const [state, setState] = useState({ key: null, loading: true, data: null, error: null });
    useEffect(function () {
      let live = true;
      if (!path) { setState({ key: null, loading: false, data: null, error: null }); return function () {}; }
      // A response belongs to the request that produced it. A new resource starts with
      // nothing rendered, so the previous record cannot be shown (or selected) under a new
      // id; a refresh of the same resource may keep showing what it already had.
      setState(function (prev) {
        return prev.key === path
          ? Object.assign({}, prev, { loading: true })
          : { key: path, loading: true, data: null, error: null };
      });
      SDK.fetchJSON(path)
        .then(function (data) { if (live) setState({ key: path, loading: false, data: data, error: null }); })
        .catch(function (err) { if (live) setState({ key: path, loading: false, data: null, error: String((err && err.message) || err) }); });
      return function () { live = false; };
    }, [path, tick]);
    // The effect above runs after the render, so the guard must also hold during it: on the
    // first render for a new path, state still belongs to the previous one.
    if (!path) return { key: null, loading: false, data: null, error: null };
    return state.key === path ? state : { key: path, loading: true, data: null, error: null };
  }

  function status(s) {
    if (!s) return null;
    return h(Badge, { variant: s === "active" ? "default" : "outline" }, s);
  }

  // The two starter actions on an empty page. The web dashboard has no composer, so a button
  // copies its prompt, shows the exact text, and says to paste it into a new chat. Nothing is
  // sent, and the page still issues no request beyond its GETs.
  function Starter(props) {
    const copied = props.copied;
    return h("div", { className: "flex flex-col gap-2" },
      h("p", { className: "text-sm font-medium" }, props.title),
      h("p", { className: "text-xs text-muted-foreground" }, props.description),
      h("div", { className: "flex flex-wrap gap-2" },
        h(Button, { variant: "default", size: "sm", onClick: function () { props.onCopy(SUGGEST_PROMPT); } },
          "Find clients in my past chats"),
        h(Button, { variant: "outline", size: "sm", onClick: function () { props.onCopy(ADD_PROMPT); } },
          "Add a client")),
      copied ? h("div", { className: "flex flex-col gap-1" },
        h("p", { className: "text-xs text-muted-foreground" },
          copied.ok ? "Copied. Paste it into a new chat." : "Copy this prompt into a new chat."),
        // The prompt holds no client data, so it is shown even with privacy on.
        h("pre", { className: "whitespace-pre-wrap break-words font-sans text-xs" }, copied.text)) : null);
  }

  function List(props) {
    const needle = props.query.trim().toLowerCase();
    const match = function (item) {
      return !needle || [item.name, item.id].some(function (v) { return v && v.toLowerCase().indexOf(needle) >= 0; });
    };
    const rows = [];
    const projectRow = function (project) {
      // Only local paths are folders (SPEC 4); URLs are references and never "missing".
      const folders = project.locations.filter(function (l) { return l.local; });
      const missing = folders.filter(function (l) { return !l.found; }).length;
      return h("button", {
        key: project.id, type: "button", "data-engagements-id": project.id,
        onClick: function () { props.onSelect(project.id); },
        className: "flex w-full items-center gap-2 rounded py-1 pr-2 text-left text-sm hover:bg-muted " +
          (props.selected === project.id ? "bg-muted" : ""),
        style: { paddingLeft: "1.5rem" }
      },
      h("span", { className: "flex-1 truncate" }, mask(project.name, props.hidden)),
      folders.length ? h(Badge, { variant: missing ? "destructive" : "outline" },
        folders.length + (folders.length === 1 ? " folder" : " folders")) : null,
      status(project.status));
    };
    props.data.clients.forEach(function (client) {
      const projects = client.projects.filter(function (p) { return match(p) || match(client); });
      if (!match(client) && !projects.length) return;
      rows.push(h("button", {
        key: client.id, type: "button", "data-engagements-id": client.id,
        onClick: function () { props.onSelect(client.id); },
        className: "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-muted " +
          (props.selected === client.id ? "bg-muted" : "")
      }, h("span", { className: "flex-1 truncate font-medium" }, mask(client.name, props.hidden)), status(client.status)));
      projects.forEach(function (project) { rows.push(projectRow(project)); });
    });
    // Projects whose client is missing or unknown are still in the library and still counted
    // in the header, so they are listed, selectable and masked like any other row
    // There is no client to start a chat with.
    const unassigned = (props.data.unassigned || []).filter(match);
    if (unassigned.length) {
      rows.push(h("p", { key: "__unassigned", className: "px-2 pt-3 pb-1 text-xs font-medium uppercase text-muted-foreground" },
        "Needs a client"));
      unassigned.forEach(function (project) { rows.push(projectRow(project)); });
    }
    if (!rows.length) return h("p", { className: "p-2 text-sm text-muted-foreground" }, needle ? "Nothing matches." : "No clients yet.");
    return h("div", { className: "flex flex-col gap-0.5" }, rows);
  }

  // Records the page cannot list: two files under one id, or a file whose id or kind cannot be
  // read. Every one of them is named exactly once, with its path and a plain reason, so the page
  // never leaves a record out silently (SPEC 6.7).
  const NOT_LISTED = "It is not in the latest list of clients and projects.";

  function NotListed(props) {
    const records = props.records || [];
    if (!records.length) return null;
    return h("div", {
      className: "flex flex-col gap-1 rounded text-xs",
      style: { border: "1px solid currentColor", padding: "0.375rem 0.5rem" },
      "data-engagements-not-listed": "unlisted"
    },
      h("p", { className: "font-medium" },
        records.length === 1 ? "1 record is not listed" : records.length + " records are not listed"),
      records.map(function (record) {
        return h("p", { key: record.path, className: "break-all" },
          h("span", { className: "font-mono" }, props.hidden ? "(hidden)" : record.path), " ",
          h("span", { className: "text-muted-foreground" }, record.reason || NOT_LISTED));
      }),
      h("p", { className: "text-muted-foreground" },
        "Run hermes engagements doctor to see what needs fixing."));
  }

  // The ids the latest overview offers as rows: the only ones the detail pane may open. Everything
  // else is named in the notice, so a selection reported there shows its reason and no record.
  function listedIds(data) {
    const ids = new Set();
    (data.clients || []).forEach(function (client) {
      ids.add(client.id);
      (client.projects || []).forEach(function (project) { ids.add(project.id); });
    });
    (data.unassigned || []).forEach(function (project) { ids.add(project.id); });
    return ids;
  }

  function notListedReason(data, id) {
    if (!id || listedIds(data).has(id)) return null;
    const record = (data.unlisted || []).find(function (item) { return item.id === id; });
    return (record && record.reason) || NOT_LISTED;
  }

  function Detail(props) {
    // A selection the latest overview does not list is not read at all: neither the detail already
    // on screen nor a late response may appear beside a notice that says the record is not listed.
    const res = useJSON(props.id && !props.blocked ? API + "/record/" + encodeURIComponent(props.id) : null, props.tick);
    const hidden = props.hidden;
    if (!props.id) return h("p", { className: "text-sm text-muted-foreground" }, "Pick a client or project to read its record.");
    if (props.blocked) {
      return h("div", { className: "flex flex-col gap-1" },
        h("p", { className: "text-sm font-medium" }, "This record is not listed"),
        h("p", { className: "text-sm text-muted-foreground" }, props.blocked));
    }
    if (res.error) return h("p", { className: "text-sm text-destructive" }, shown(res.error, hidden));
    if (!res.data) return h("p", { className: "text-sm text-muted-foreground" }, "Loading…");
    const d = res.data;
    // The library can disappear between the list and this read: the envelope then carries a
    // status and a message and no record at all, so nothing below may touch record fields.
    if (d.status !== "ok") {
      return h("div", { className: "flex flex-col gap-1" },
        h("p", { className: "text-sm font-medium" }, "No client library yet"),
        h("p", { className: "text-sm text-muted-foreground" }, shown(d.message, hidden)));
    }
    const locations = (d.properties && d.properties.locations) || [];
    const folders = locations.filter(function (l) { return l.local; });
    const references = locations.filter(function (l) { return !l.local; });
    // A project with no client record cannot start a chat: there is no client to bind to.
    const ownerId = d.kind === "client" ? d.id : (d.client && d.client.known ? d.client.id : null);
    return h("div", { className: "flex flex-col gap-4" },
      h("div", { className: "flex flex-wrap items-center gap-2" },
        h("h2", { className: "text-lg font-semibold" }, mask(d.name, hidden)), status(d.record_status),
        h(Badge, { variant: "outline" }, d.kind), d.valid ? null : h(Badge, { variant: "destructive" }, "needs repair")),
      // Layout that the host's compiled CSS may not carry (arbitrary values) is inline, not a class.
      h("dl", { className: "grid gap-x-3 gap-y-1 text-xs text-muted-foreground", style: { gridTemplateColumns: "8rem minmax(0, 1fr)" } },
        h("dt", null, "Id"), h("dd", { className: "font-mono" }, hidden ? "(hidden)" : d.id),
        d.kind === "project" ? h("dt", null, "Client") : null,
        d.kind === "project" ? h("dd", null, ownerId ? mask(d.client.name || ownerId, hidden) : "(unknown client)") : null,
        h("dt", null, "Record file"), h("dd", { className: "break-all font-mono" }, hidden ? "(hidden)" : d.abs_path),
        h("dt", null, "Start a chat"),
        h("dd", { className: "font-mono" },
          ownerId ? "/client " + (hidden ? "…" : ownerId) : "(no client on this record)")),
      folders.length ? h("div", { className: "flex flex-col gap-1" },
        h("div", { className: "text-xs font-medium uppercase" }, "Project folders"),
        folders.map(function (loc) {
          return h("div", { key: loc.path, className: "flex items-center gap-2 text-xs" },
            h("span", { className: "flex-1 break-all font-mono" }, hidden ? "(hidden)" : loc.expanded),
            loc.found ? h(Badge, { variant: "outline" }, "found") : h(Badge, { variant: "destructive" }, "not found here"));
        })) : null,
      references.length ? h("div", { className: "flex flex-col gap-1" },
        h("div", { className: "text-xs font-medium uppercase" }, "Links and references"),
        references.map(function (loc) {
          return h("div", { key: loc.path, className: "flex items-center gap-2 text-xs" },
            h("span", { className: "flex-1 break-all font-mono" }, hidden ? "(hidden)" : loc.path));
        })) : null,
      hidden ? h("p", { className: "text-sm text-muted-foreground" }, "Record text is hidden while privacy mode is on.") :
        d.sections.map(function (s, i) {
          return h("section", { key: i + ":" + s.title, className: "flex flex-col gap-1" },
            s.title ? h("h3", { className: "text-sm font-semibold" }, s.title) : null,
            h("pre", { className: "whitespace-pre-wrap break-words font-sans text-sm" }, s.text.trim() || "(empty)"));
        }),
      d.truncated ? h("p", { className: "text-xs text-muted-foreground" }, "This record is long; open the file to read the rest.") : null);
  }

  function ClientsTab() {
    const [tick, setTick] = useState(0);
    const [query, setQuery] = useState("");
    const [selected, setSelected] = useState(null);
    const [copied, setCopied] = useState(null);
    // sessionStorage, not localStorage: SPEC 6.7 says privacy is remembered per window, so a
    // new window or tab starts from the safe default instead of another window's last toggle.
    const [hidden, setHidden] = useState(function () {
      try { return window.sessionStorage.getItem(PRIVACY_KEY) === "true"; } catch (e) { return false; }
    });
    const res = useJSON(API + "/overview", tick);
    // A copy is only a copy: nothing is sent, and no request replaces the GETs above. When the
    // browser has no clipboard (an insecure origin), the prompt is still shown to copy by hand.
    const copyPrompt = function (text) {
      const clipboard = window.navigator && window.navigator.clipboard;
      const written = clipboard && clipboard.writeText
        ? clipboard.writeText(text)
        : Promise.reject(new Error("clipboard unavailable"));
      return written.then(
        function () { setCopied({ text: text, ok: true }); },
        function () { setCopied({ text: text, ok: false }); });
    };
    const togglePrivacy = function () {
      const next = !hidden;
      // A search typed before privacy was turned on names a client. It is cleared in the same
      // click, so the first private render already has an empty field.
      if (next) setQuery("");
      setHidden(next);
      try { window.sessionStorage.setItem(PRIVACY_KEY, String(next)); } catch (e) { /* storage unavailable */ }
    };
    const d = res.data;
    // Only a library with nothing in it gets the starter: not one that is merely empty of
    // clients while holding projects, and not one that cannot be written to at all.
    const starter = d && d.status === "ok" && d.counts && !d.counts.clients && !d.counts.projects;
    // A selection the latest overview no longer lists is shown as such, whatever the record
    // request in flight or already answered says.
    const blocked = d ? notListedReason(d, selected) : null;
    let body;
    if (res.error) body = h("p", { className: "text-sm text-destructive" }, shown(res.error, hidden));
    else if (!d) body = h("p", { className: "text-sm text-muted-foreground" }, "Loading…");
    // The missing-library sentence carries the `init --root` hint; it stays as secondary text.
    else if (d.status === "missing") body = h("div", { className: "flex flex-col gap-3" },
      h("p", { className: "text-sm text-muted-foreground" }, shown(d.message, hidden)),
      h(Starter, { title: "Start with your first client", description: STORY, copied: copied, onCopy: copyPrompt }));
    // Every other non-ok state keeps its own message: the starter would promise work that cannot be saved.
    else if (d.status !== "ok") body = h("p", { className: "text-sm text-muted-foreground" }, shown(d.message, hidden));
    // Two panes that wrap to one column on a narrow window, without relying on a breakpoint class.
    else body = h("div", { className: "flex flex-wrap gap-4" },
      h("div", { className: "flex flex-col gap-2", style: { flex: "1 1 16rem", maxWidth: "22rem" } },
        // Every record the page cannot list is named here, above the rows, so it is not simply absent.
        h(NotListed, { records: d.unlisted, hidden: hidden }),
        // Nothing to search on an empty page: the starter block takes the column instead.
        starter ? null : h(Input, { placeholder: "Find a client or project", value: query, onChange: function (e) { setQuery(e.target.value); } }),
        starter
          ? h(Starter, { title: "No clients yet", description: STORY, copied: copied, onCopy: copyPrompt })
          : h(List, { data: d, hidden: hidden, query: query, selected: selected, onSelect: setSelected })),
      h("div", { style: { flex: "999 1 24rem", minWidth: 0 } },
        h(Detail, { id: selected, hidden: hidden, tick: tick, blocked: blocked })));
    return h(Card, { "data-engagements-page": "clients" },
      h(CardHeader, null,
        h("div", { className: "flex flex-wrap items-center gap-3" },
          h(CardTitle, null, "Clients"),
          d && d.counts ? h("span", { className: "text-xs text-muted-foreground" },
            d.counts.clients + " clients, " + d.counts.projects + " projects, " + d.counts.active_projects + " active") : null,
          d && d.doctor ? h(Badge, { variant: d.doctor.clean ? "default" : "destructive" },
            d.doctor.clean ? "library healthy" : (d.doctor.counts.error || 0) + " errors, " + (d.doctor.counts.warning || 0) + " warnings") : null,
          h("div", { className: "flex-1" }),
          h(Button, { variant: hidden ? "default" : "outline", size: "sm", onClick: togglePrivacy }, hidden ? "Privacy on" : "Privacy off"),
          h(Button, { variant: "outline", size: "sm", onClick: function () { setTick(tick + 1); } }, "Refresh"))),
      h(CardContent, null, body,
        h("p", { className: "mt-4 text-xs text-muted-foreground" },
          "Read-only. Ask the agent to change a record, or edit the file directly.")));
  }

  window.__HERMES_PLUGINS__.register("engagements", ClientsTab);
})();
