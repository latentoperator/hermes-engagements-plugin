// Engagements for Hermes Desktop: a read-only Clients page, a sidebar entry, a status-bar
// chip that shows which client the focused chat is bound to, and an "Open Clients" command
// (SPEC 6.7). Nothing here writes to the library or to a binding: every request is a GET to
// this plugin's own backend (dashboard/plugin_api.py), and "Start a chat" and the starter
// prompts on an empty page only place text in a new chat's composer for the user to send.
//
// Loaded uncompiled by Desktop, so there is no JSX: jsx()/jsxs() from react/jsx-runtime.
import {
  Badge,
  Button,
  Codicon,
  CopyButton,
  EmptyState,
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  SearchField,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Switch,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PATH = '/engagements'
const CHANGED = 'plugin.engagements.changed'
const PRIVACY_KEY = 'privacy'
const HIDDEN_MESSAGE = 'Details are hidden while privacy mode is on.'

let CTX = null
let revision = 0
const listeners = new Set()

// Every view re-reads when the agent changes a record or a binding, or when the user asks.
function bump() {
  revision += 1
  listeners.forEach(fn => fn(revision))
}

function useRevision() {
  const [value, setValue] = useState(revision)
  useEffect(() => {
    listeners.add(setValue)
    return () => listeners.delete(setValue)
  }, [])
  return value
}

function useRest(path, deps) {
  const [state, setState] = useState({ key: null, loading: true, data: null, error: null })
  useEffect(() => {
    let live = true
    if (!path) {
      setState({ key: null, loading: false, data: null, error: null })
      return () => {}
    }
    // A response belongs to the request that produced it. A new resource starts with
    // nothing rendered, so the previous record cannot be shown (or a chat started) under a
    // new id; a refresh of the same resource may keep showing what it already had.
    setState(prev =>
      prev.key === path ? { ...prev, loading: true } : { key: path, loading: true, data: null, error: null }
    )
    CTX.rest(path)
      .then(data => live && setState({ key: path, loading: false, data, error: null }))
      .catch(err => live && setState({ key: path, loading: false, data: null, error: errorText(err) }))
    return () => {
      live = false
    }
  }, deps)
  // The effect above runs after the render, so the guard must also hold during it: on the
  // first render for a new path, state still belongs to the previous one.
  if (!path) return IDLE
  return state.key === path ? state : { key: path, loading: true, data: null, error: null }
}

const IDLE = { key: null, loading: false, data: null, error: null }

function errorText(err) {
  const raw = (err && (err.detail || err.message)) || String(err || '')
  if (/404|not found/i.test(raw) && /plugins\/engagements/.test(raw)) {
    return 'The Engagements backend is not running for this profile. Enable the engagements plugin, then restart Hermes.'
  }
  return raw.replace(/^Error:\s*/, '') || 'Engagements could not read the library.'
}

// Privacy mode masks names and paths so the page can be shown on a shared screen or recorded.
let privacy = false
const privacyListeners = new Set()

function setPrivacy(on) {
  privacy = Boolean(on)
  if (CTX) CTX.storage.set(PRIVACY_KEY, privacy)
  privacyListeners.forEach(fn => fn(privacy))
}

function usePrivacy() {
  const [value, setValue] = useState(privacy)
  useEffect(() => {
    privacyListeners.add(setValue)
    return () => privacyListeners.delete(setValue)
  }, [])
  return value
}

function mask(text, hidden) {
  if (!hidden || !text) return text
  return String(text)
    .split(/\s+/)
    .filter(Boolean)
    .map(word => word[0].toUpperCase() + '.')
    .join(' ')
}

// A sentence from the backend can carry an absolute path (the missing-library message
// does) or an id. With privacy on the page shows a fixed sentence instead of trying to
// redact parts of it, so nothing identifying reaches the screen.
function shown(text, hidden) {
  return hidden ? HIDDEN_MESSAGE : text
}

const muted = 'text-(--ui-text-tertiary)'

function statusBadge(status) {
  if (status === 'active') return jsx(Badge, { variant: 'success', children: 'active' })
  if (!status) return null
  return jsx(Badge, { variant: 'muted', children: status })
}

// Text placed in a new chat's composer, never sent: the user presses Enter. Shared by "Start a
// chat" and both starter prompts, so the two cannot drift. If no composer answers, the text is
// copied instead, so the user can still paste it.
async function startChatWith(text, messages) {
  host.newChat()
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 250))
    if (await host.composer.setDraft('new', text)) {
      host.notify({ kind: 'info', message: messages.ready })
      return
    }
  }
  const copied = await CTX.os.writeClipboard(text)
  host.notify({ kind: 'info', message: copied ? messages.copied : messages.manual })
}

function startChat(id) {
  const command = `/client ${id}`
  return startChatWith(command, {
    ready: `New chat ready. Press Enter to send ${command}.`,
    copied: `Copied ${command}. Paste it into the new chat to bind it.`,
    manual: `Type ${command} in the new chat to bind it.`
  })
}

// Starter prompts for an empty Clients page (spec 6.7, decision D12). Each names the bundled skill so the
// agent picks it up, and still reads as a plain request if the skill is not loaded. Neither prompt
// carries any user data, and a button only drafts: nothing is sent and nothing is written.
const SUGGEST_PROMPT =
  'Look through my past Hermes chats for clients or recurring projects I have worked on. ' +
  'Use the engagements:suggest-clients skill. Suggest up to five worth adding, each with one ' +
  'line on why and which chats it came from. Do not save anything yet; let me pick. If you find ' +
  'none, help me add one client instead.'

const ADD_PROMPT = 'Help me add a client to Engagements. Use the engagements:onboard-client skill. Ask me only what you need.'

const STORY = 'Find clients from chats you have already had, or add one now.'

function startStarter(which) {
  return startChatWith(which === 'add' ? ADD_PROMPT : SUGGEST_PROMPT, {
    ready: 'New chat ready. Press Enter to send the prompt.',
    copied: 'Copied the prompt. Paste it into the new chat and press Enter.',
    manual: 'Type the prompt in the new chat to get started.'
  })
}

function Starter({ title, description }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 p-3',
    'data-engagements-starter': 'true',
    children: [
      jsx('div', { className: 'text-sm font-medium', children: title }),
      jsx('div', { className: `text-xs ${muted}`, children: description }),
      jsxs('div', {
        className: 'flex flex-wrap gap-2',
        children: [
          jsx(Button, {
            variant: 'default',
            size: 'sm',
            onClick: () => startStarter('find'),
            title: 'Ask the agent to suggest clients from your past chats',
            children: 'Find clients in my past chats'
          }),
          jsx(Button, {
            variant: 'outline',
            size: 'sm',
            onClick: () => startStarter('add'),
            title: 'Start a new chat with the agent to add one client',
            children: 'Add a client'
          })
        ]
      })
    ]
  })
}

// Records the page cannot list: two files under one id, or a file whose id or kind cannot be
// read. Every one of them is named exactly once, with its path and a plain reason, so the page
// never leaves a record out silently (SPEC 6.7).
const NOT_LISTED = 'It is not in the latest list of clients and projects.'

function NotListed({ records, hidden }) {
  if (!records || !records.length) return null
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded px-2 py-1.5 text-xs',
    style: { border: '1px solid var(--ui-stroke-secondary)' },
    'data-engagements-not-listed': 'unlisted',
    children: [
      jsx('div', {
        className: 'font-medium',
        children: records.length === 1 ? '1 record is not listed' : `${records.length} records are not listed`
      }),
      ...records.map(record =>
        jsxs(
          'div',
          {
            className: 'break-all',
            children: [
              jsx('span', { className: 'font-mono', children: hidden ? '(hidden)' : record.path }),
              ' ',
              jsx('span', { className: muted, children: record.reason || NOT_LISTED })
            ]
          },
          record.path
        )
      ),
      jsx('div', { className: muted, children: 'Run hermes engagements doctor to see what needs fixing.' })
    ]
  })
}

async function reveal(path) {
  const shown = await CTX.os.revealPath(path)
  if (!shown) {
    const copied = await CTX.os.writeClipboard(path)
    host.notify({
      kind: 'info',
      message: copied
        ? 'This folder is not on this computer, so its path was copied instead.'
        : 'This folder is not on this computer.'
    })
  }
}

function FolderRow({ path, found, hidden, label }) {
  return jsxs('div', {
    className: 'flex min-w-0 items-center gap-2 py-0.5',
    children: [
      jsx(StatusDot, { tone: found ? 'good' : 'warn' }),
      jsx('span', {
        className: 'min-w-0 flex-1 truncate font-mono text-xs',
        title: hidden ? undefined : path,
        children: hidden ? '(hidden)' : path
      }),
      found ? null : jsx(Badge, { variant: 'warn', size: 'xs', children: 'not found here' }),
      jsx(Button, {
        variant: 'ghost',
        size: 'sm',
        disabled: hidden,
        onClick: () => reveal(path),
        title: label || 'Show in file manager',
        children: jsx(Codicon, { name: 'folder-opened' })
      }),
      hidden ? null : jsx(CopyButton, { text: path, label: 'Copy path', appearance: 'icon', buttonVariant: 'ghost' })
    ]
  })
}

// A link or other non-local location (SPEC 4): shown as text with a copy button, never as a
// missing folder and never offered to the file manager.
function ReferenceRow({ path, hidden }) {
  return jsxs('div', {
    className: 'flex min-w-0 items-center gap-2 py-0.5',
    children: [
      jsx(Codicon, { name: 'link', size: 12 }),
      jsx('span', {
        className: 'min-w-0 flex-1 truncate font-mono text-xs',
        title: hidden ? undefined : path,
        children: hidden ? '(hidden)' : path
      }),
      hidden ? null : jsx(CopyButton, { text: path, label: 'Copy link', appearance: 'icon', buttonVariant: 'ghost' })
    ]
  })
}

function Overview({ data, hidden, query, selected, onSelect }) {
  const needle = query.trim().toLowerCase()
  const matches = item =>
    !needle || [item.name, item.id].some(v => v && v.toLowerCase().includes(needle))
  const rows = []
  const projectRow = project => {
    // Only local paths are folders (SPEC 4); URLs are references and never "missing".
    const folders = project.locations.filter(l => l.local)
    const missing = folders.filter(l => !l.found).length
    return jsxs(
      'button',
      {
        type: 'button',
        'data-engagements-id': project.id,
        onClick: () => onSelect(project.id),
        className:
          'flex w-full items-center gap-2 rounded py-1 pr-2 pl-7 text-left text-sm hover:bg-(--chrome-action-hover) ' +
          (selected === project.id ? 'bg-(--ui-bg-quaternary)' : ''),
        children: [
          jsx(Codicon, { name: 'project' }),
          jsx('span', { className: 'min-w-0 flex-1 truncate', children: mask(project.name, hidden) }),
          folders.length
            ? jsx(Badge, {
                variant: missing ? 'warn' : 'outline',
                size: 'xs',
                children: `${folders.length} folder${folders.length === 1 ? '' : 's'}`
              })
            : null,
          statusBadge(project.status)
        ]
      },
      project.id
    )
  }
  const clients = data.clients
    .map(client => ({ client, projects: client.projects.filter(p => matches(p) || matches(client)) }))
    .filter(({ client, projects }) => matches(client) || projects.length)
  clients.forEach(({ client, projects }) => {
    rows.push(
      jsxs(
        'button',
        {
          type: 'button',
          'data-engagements-id': client.id,
          onClick: () => onSelect(client.id),
          className:
            'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-(--chrome-action-hover) ' +
            (selected === client.id ? 'bg-(--ui-bg-quaternary)' : ''),
          children: [
            jsx(Codicon, { name: 'organization' }),
            jsx('span', { className: 'min-w-0 flex-1 truncate font-medium', children: mask(client.name, hidden) }),
            statusBadge(client.status),
            client.valid ? null : jsx(Badge, { variant: 'warn', size: 'xs', children: 'needs repair' })
          ]
        },
        client.id
      )
    )
    projects.forEach(project => rows.push(projectRow(project)))
  })
  // Projects whose client is missing or unknown are still in the library and still counted in
  // the header, so they are listed, selectable and masked like any other row
  // There is no client to start a chat with.
  const unassigned = (data.unassigned || []).filter(matches)
  if (unassigned.length) {
    rows.push(
      jsx(
        'div',
        {
          className: `px-2 pt-3 pb-1 text-xs font-medium uppercase tracking-wide ${muted}`,
          children: 'Needs a client'
        },
        '__unassigned'
      )
    )
    unassigned.forEach(project => rows.push(projectRow(project)))
  }
  if (!rows.length) {
    return jsx('div', { className: `p-3 text-sm ${muted}`, children: needle ? 'Nothing matches.' : 'No clients yet.' })
  }
  return jsx('div', { className: 'flex flex-col gap-0.5', children: rows })
}

function Detail({ id, hidden, blocked }) {
  const tick = useRevision()
  // A selection the latest overview does not list is not read at all: neither the detail already
  // on screen nor a late response may appear beside a notice that says the record is not listed.
  const { loading, data, error } = useRest(id && !blocked ? `/record/${encodeURIComponent(id)}` : null, [id, blocked, tick])
  if (!id) {
    return jsx(EmptyState, { title: 'Pick a client or project', description: 'Its record opens here, read-only.' })
  }
  if (blocked) return jsx(EmptyState, { title: 'This record is not listed', description: blocked })
  if (error) return jsx('div', { className: 'p-4 text-sm text-destructive', children: shown(error, hidden) })
  if (!data) return jsx('div', { className: `p-4 text-sm ${muted}`, children: loading ? 'Loading…' : '' })
  // The library can disappear between the list and this read: the envelope then carries a
  // status and a message and no record at all, so nothing below may touch record fields.
  if (data.status !== 'ok') {
    return jsx(EmptyState, { title: 'No client library yet', description: shown(data.message, hidden) })
  }
  const props = data.properties || {}
  const locations = props.locations || []
  const folders = locations.filter(l => l.local)
  const references = locations.filter(l => !l.local)
  // A project with no client record cannot start a chat: there is no client to bind to.
  const ownerId = data.kind === 'client' ? data.id : data.client && data.client.known ? data.client.id : null
  return jsxs('div', {
    className: 'flex flex-col gap-4 p-4',
    'data-engagements-detail': data.id,
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(Codicon, { name: data.kind === 'client' ? 'organization' : 'project', size: 18 }),
          jsx('h2', { className: 'text-base font-semibold', children: mask(data.name, hidden) }),
          statusBadge(data.record_status),
          jsx(Badge, { variant: 'outline', children: data.kind }),
          data.valid ? null : jsx(Badge, { variant: 'warn', children: 'needs repair' }),
          jsx('div', { className: 'flex-1' }),
          ownerId
            ? jsxs(Button, {
                variant: 'default',
                size: 'sm',
                onClick: () => startChat(ownerId),
                children: [jsx(Codicon, { name: 'comment-discussion' }), 'Start a chat']
              })
            : null
        ]
      }),
      jsxs('div', {
        // Arbitrary-value layout is inline: Desktop's compiled CSS only carries classes it uses itself.
        className: `grid gap-x-3 gap-y-1 text-xs ${muted}`,
        style: { gridTemplateColumns: '8rem minmax(0, 1fr)' },
        children: [
          jsx('span', { children: 'Id' }),
          jsx('span', { className: 'font-mono', children: hidden ? '(hidden)' : data.id }),
          data.kind === 'project' ? jsx('span', { children: 'Client' }) : null,
          data.kind === 'project'
            ? jsx('span', { children: ownerId ? mask(data.client.name || ownerId, hidden) : '(unknown client)' })
            : null,
          data.kind === 'client' ? jsx('span', { children: 'Projects' }) : null,
          data.kind === 'client'
            ? jsx('span', {
                children: data.projects.length ? data.projects.map(p => mask(p.name, hidden)).join(', ') : 'none yet'
              })
            : null,
          jsx('span', { children: 'Record file' }),
          jsx(FolderRow, { path: data.abs_path, found: true, hidden, label: 'Show the record in the file manager' })
        ]
      }),
      folders.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'text-xs font-medium uppercase tracking-wide', children: 'Project folders' }),
              ...folders.map(loc => jsx(FolderRow, { path: loc.expanded, found: loc.found, hidden }, loc.path))
            ]
          })
        : null,
      references.length
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('div', { className: 'text-xs font-medium uppercase tracking-wide', children: 'Links and references' }),
              ...references.map(loc => jsx(ReferenceRow, { path: loc.path, hidden }, loc.path))
            ]
          })
        : null,
      hidden
        ? jsx('div', { className: `text-sm ${muted}`, children: 'Record text is hidden while privacy mode is on.' })
        : jsx('div', {
            className: 'flex flex-col gap-3',
            children: data.sections.map((section, index) =>
              jsxs(
                'section',
                {
                  className: 'flex flex-col gap-1',
                  children: [
                    section.title
                      ? jsx('h3', { className: 'text-sm font-semibold', children: section.title })
                      : null,
                    jsx('pre', {
                      className: 'font-sans text-sm whitespace-pre-wrap break-words',
                      children: section.text.trim() || '(empty)'
                    })
                  ]
                },
                `${index}:${section.title}`
              )
            )
          }),
      data.truncated
        ? jsx('div', { className: `text-xs ${muted}`, children: 'This record is long; open the file to read the rest.' })
        : null
    ]
  })
}

function Header({ data, hidden }) {
  const doctor = data && data.doctor
  const counts = data && data.counts
  return jsxs('div', {
    className: 'flex flex-wrap items-center gap-3 border-b border-(--ui-stroke-secondary) px-4 py-3',
    children: [
      jsx('h1', { className: 'text-base font-semibold', children: 'Clients' }),
      counts
        ? jsx('span', {
            className: `text-xs ${muted}`,
            children: `${counts.clients} clients, ${counts.projects} projects, ${counts.active_projects} active`
          })
        : null,
      doctor
        ? jsx(Badge, {
            variant: doctor.clean ? 'success' : 'warn',
            title: 'Run `hermes engagements doctor` for details',
            children: doctor.clean ? 'library healthy' : `${doctor.counts.error || 0} errors, ${doctor.counts.warning || 0} warnings`
          })
        : null,
      jsx('div', { className: 'flex-1' }),
      jsxs('label', {
        className: `flex items-center gap-2 text-xs ${muted}`,
        children: [jsx(Switch, { checked: hidden, onCheckedChange: setPrivacy, size: 'xs' }), 'Privacy']
      }),
      jsx(Button, {
        variant: 'ghost',
        size: 'sm',
        onClick: bump,
        title: 'Refresh',
        children: jsx(Codicon, { name: 'refresh' })
      })
    ]
  })
}

// The ids the latest overview offers as rows: the only ones the detail pane may open. Everything
// else is named in the notice, so a selection reported there shows its reason and no record.
function listedIds(data) {
  const ids = new Set()
  ;(data.clients || []).forEach(client => {
    ids.add(client.id)
    ;(client.projects || []).forEach(project => ids.add(project.id))
  })
  ;(data.unassigned || []).forEach(project => ids.add(project.id))
  return ids
}

function notListedReason(data, id) {
  if (!id || listedIds(data).has(id)) return null
  const record = (data.unlisted || []).find(item => item.id === id)
  return (record && record.reason) || NOT_LISTED
}

function ClientsPage() {
  const tick = useRevision()
  const hidden = usePrivacy()
  // A search belongs to the privacy setting it was typed under. Turning privacy on blanks it in
  // that same render, not one effect later, so a name typed before never reaches a private screen.
  const [search, setSearch] = useState({ text: '', hidden })
  const query = search.hidden === hidden ? search.text : ''
  const setQuery = text => setSearch({ text, hidden })
  const [selected, setSelected] = useState(null)
  const { loading, data, error } = useRest('/overview', [tick])
  // Then drop it for good, so turning privacy off again does not bring the old search back.
  useEffect(() => {
    if (hidden) setSearch(prev => (prev.text || !prev.hidden ? { text: '', hidden: true } : prev))
  }, [hidden])
  const body = useMemo(() => {
    if (error) return jsx('div', { className: 'p-4 text-sm text-destructive', children: shown(error, hidden) })
    if (!data) return jsx('div', { className: `p-4 text-sm ${muted}`, children: loading ? 'Loading…' : '' })
    // A missing library is the one "nothing yet" state that quietly invited the user to run a CLI
    // command. The starter prompts sit beside that hint, which stays as the secondary sentence.
    if (data.status === 'missing') {
      return jsxs('div', {
        className: 'flex flex-col gap-3 p-4',
        children: [
          jsx(EmptyState, { title: 'No client library yet', description: shown(data.message, hidden) }),
          jsx(Starter, { title: 'Start with your first client', description: STORY })
        ]
      })
    }
    // Every other non-ok state (an unusable root, a broken library.yaml) keeps its own message:
    // the starter would promise work that cannot be saved.
    if (data.status !== 'ok') {
      return jsx(EmptyState, { title: 'No client library yet', description: shown(data.message, hidden) })
    }
    // The starter is for a library with nothing in it, not for one that is merely empty of
    // clients while holding projects that need a client.
    const starter = !(data.counts && (data.counts.clients || data.counts.projects))
    // A selection the latest overview no longer lists is shown as such, whatever the record
    // request in flight or already answered says.
    const blocked = notListedReason(data, selected)
    return jsxs('div', {
      className: 'grid min-h-0 flex-1',
      // The list takes at most 35%: a fixed 22rem left the record ~325px in a default window.
      style: { gridTemplateColumns: 'minmax(12rem, 35%) minmax(0, 1fr)' },
      children: [
        jsxs('div', {
          className: 'flex min-h-0 flex-col gap-2 border-r border-(--ui-stroke-secondary) p-2',
          children: [
            // Every record the page cannot list is named here, above the rows, so it is not
            // simply absent.
            jsx(NotListed, { records: data.unlisted, hidden }),
            // Nothing to search on an empty page: the starter block takes the column instead.
            starter ? null : jsx(SearchField, { placeholder: 'Find a client or project', value: query, onChange: setQuery }),
            jsx(Scroller, {
              className: 'min-h-0 flex-1',
              children: starter
                ? jsx(Starter, { title: 'No clients yet', description: STORY })
                : jsx(Overview, { data, hidden, query, selected, onSelect: setSelected })
            })
          ]
        }),
        jsx(Scroller, { className: 'min-h-0', children: jsx(Detail, { id: selected, hidden, blocked }) })
      ]
    })
  }, [data, error, loading, hidden, query, selected])
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    'data-engagements-page': 'clients',
    children: [jsx(Header, { data, hidden }), body]
  })
}

// A plain vertical scroller. Not the SDK's ScrollArea: its Radix viewport wraps content in a
// display:table box that grows to the content's natural width, so a long path pushed the record
// pane past the window and no truncate could engage (P3V on Desktop).
function Scroller({ className, children }) {
  return jsx('div', {
    className: `min-w-0 ${className || ''}`,
    style: { overflowY: 'auto', overflowX: 'hidden' },
    children
  })
}

// The status-bar chip: which client the focused chat is bound to, as the agent last stored it.
function BindingChip() {
  const tick = useRevision()
  const hidden = usePrivacy()
  const session = useValue(host.state.focusedStoredSessionId)
  const busy = useValue(host.state.busy)
  const { data } = useRest(session ? `/session?id=${encodeURIComponent(session)}` : null, [session, tick, busy])
  if (!session || !data || data.status !== 'ok') return null
  const bound = data.client
  const text = bound
    ? mask(bound.name || bound.id, hidden) + (data.project ? ` / ${mask(data.project.name || data.project.id, hidden)}` : '')
    : 'No client'
  return jsxs('button', {
    type: 'button',
    'data-engagements-chip': bound ? bound.id : '',
    className: `flex items-center gap-1 px-1.5 ${muted} hover:text-(--ui-text-primary)`,
    style: { fontSize: '0.6875rem' },
    title: bound
      ? `This chat works on ${hidden ? 'a client' : bound.name}${data.automatic ? ' (matched from the project folder)' : ''}. Click to open Clients.`
      : 'This chat is not bound to a client. Use /client to choose one.',
    onClick: () => host.navigate(PATH),
    children: [jsx(Codicon, { name: 'organization', size: 12 }), text]
  })
}

export default {
  id: 'engagements',
  name: 'Engagements',
  register(ctx) {
    CTX = ctx
    privacy = Boolean(ctx.storage.get(PRIVACY_KEY, false))
    ctx.onEvent(CHANGED, () => bump())
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(ClientsPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Clients', codicon: 'organization' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(BindingChip, {}) },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'engagements.open',
          label: 'Open Clients',
          keywords: ['engagements', 'clients', 'projects'],
          run: () => host.navigate(PATH)
        }
      }
    ])
  }
}
