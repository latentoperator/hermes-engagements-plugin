// Runs the real Clients dashboard front ends under Node with a small hooks runtime, mocked
// SDK/components and deferred requests, so a test can drive the components the way a person
// would (select a row, type a search, turn on privacy) and read back what would be on screen.
//
// Usage: node component_probe.mjs <scenario.json>
//
// The scenario names the front end, its file and a list of steps; one snapshot is printed per
// step. Nothing here ships: it is a test double for React, not a renderer for the product.

import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const scenario = JSON.parse(readFileSync(process.argv[2], 'utf8'))

// --- the hooks runtime ----------------------------------------------------------------

let current = null // { fn, index } while a component function is being called
const slotsByFn = new WeakMap()
let pendingEffects = []
let dirty = false

function slots(fn) {
  let found = slotsByFn.get(fn)
  if (!found) {
    found = []
    slotsByFn.set(fn, found)
  }
  return found
}

function depsChanged(previous, next) {
  if (!previous || !next) return true
  if (previous.length !== next.length) return true
  return next.some((value, index) => !Object.is(value, previous[index]))
}

function useState(init) {
  const slotIndex = current.index++
  const found = slots(current.fn)
  let slot = found[slotIndex]
  if (!slot || slot.kind !== 'state') {
    slot = found[slotIndex] = { kind: 'state', value: typeof init === 'function' ? init() : init }
  }
  const set = value => {
    const next = typeof value === 'function' ? value(slot.value) : value
    if (!Object.is(next, slot.value)) {
      slot.value = next
      dirty = true
    }
  }
  return [slot.value, set]
}

function useEffect(fn, deps) {
  const slotIndex = current.index++
  const found = slots(current.fn)
  const slot = found[slotIndex] || (found[slotIndex] = { kind: 'effect', ran: false, deps: undefined, cleanup: null })
  if (!slot.ran || depsChanged(slot.deps, deps)) pendingEffects.push({ slot, fn, deps })
}

function useMemo(fn, deps) {
  const slotIndex = current.index++
  const found = slots(current.fn)
  const slot = found[slotIndex]
  if (!slot || slot.kind !== 'memo' || depsChanged(slot.deps, deps)) {
    const next = { kind: 'memo', deps, value: fn() }
    found[slotIndex] = next
    return next.value
  }
  return slot.value
}

// --- elements -------------------------------------------------------------------------

function element(type, props, key) {
  return { type, props: props || {}, key, children: undefined }
}

const React = {
  createElement(type, props, ...children) {
    return { type, props: props || {}, key: undefined, children: children.length ? children : undefined }
  }
}

function jsx(type, props, key) {
  return element(type, props, key)
}

function jsxs(type, props, key) {
  return element(type, props, key)
}

function childNodes(node) {
  const out = []
  if (node.props && node.props.children !== undefined) out.push(node.props.children)
  if (node.children !== undefined) out.push(node.children)
  return out.flat(Infinity).filter(child => child !== null && child !== undefined && child !== false && child !== true)
}

// Expand every component function into host elements, so the tree can be read and clicked.
function renderNode(node) {
  if (node === null || node === undefined || node === false || node === true) return null
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(renderNode)
  if (typeof node.type === 'function') {
    // React.createElement(type, props, ...children) keeps its children in the third argument,
    // while jsx(type, {children}) keeps them in props; a component must see both as children.
    const props = Object.assign({}, node.props || {})
    if (node.children !== undefined) props.children = node.children
    const previous = current
    current = { fn: node.type, index: 0 }
    let out
    try {
      out = node.type(props)
    } finally {
      current = previous
    }
    return renderNode(out)
  }
  return { type: node.type, props: node.props || {}, key: node.key, children: childNodes(node).map(renderNode).filter(Boolean) }
}

function textOf(node) {
  if (node === null || node === undefined) return ''
  if (typeof node === 'string') return node
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (node.children === undefined) return ''
  return textOf(node.children)
}

function walk(node, visit) {
  if (node === null || node === undefined || typeof node === 'string') return
  if (Array.isArray(node)) {
    node.forEach(child => walk(child, visit))
    return
  }
  visit(node)
  if (node.children) node.children.forEach(child => walk(child, visit))
}

// --- the deferred backend ---------------------------------------------------------------

const requests = []

function defer(path) {
  let settle
  const promise = new Promise((resolve, reject) => {
    settle = { resolve, reject }
  })
  const entry = { path, done: false, resolve: settle.resolve, reject: settle.reject }
  requests.push(entry)
  return promise
}

function takeRequest(path) {
  const found = requests.find(request => !request.done && request.path === path)
  if (!found) throw new Error(`no pending request for ${path}; pending: ${requests.filter(r => !r.done).map(r => r.path).join(', ')}`)
  found.done = true
  return found
}

// --- mocks -------------------------------------------------------------------------------

function component(displayName, render) {
  const C = props => render(props || {})
  C.displayName = displayName
  return C
}

const passthrough = name => component(name, props => jsx('div', { className: props.className, children: props.children }))

const Badge = component('Badge', props =>
  jsx('span', { className: 'badge', children: props.children === undefined ? '' : props.children })
)
const Button = component('Button', props =>
  // The host Button forwards ``title`` as the accessible label; an icon-only button needs it.
  jsx('button', { type: 'button', disabled: props.disabled, onClick: props.onClick, title: props.title, style: props.style, children: props.children })
)
const Codicon = component('Codicon', () => jsx('span', { className: 'codicon', children: '' }))
const CopyButton = component('CopyButton', props => jsx('button', { type: 'button', children: 'copy', 'data-copy': props.text }))
const EmptyState = component('EmptyState', props =>
  jsxs('div', { className: 'empty', children: [jsx('div', { children: props.title }), props.description ? jsx('div', { children: props.description }) : null] })
)
const StatusDot = component('StatusDot', () => jsx('span', { className: 'dot', children: '' }))
const Switch = component('Switch', props => jsx('button', { type: 'button', 'data-switch': String(Boolean(props.checked)), onClick: () => props.onCheckedChange(!props.checked) }))
const Input = component('Input', props => jsx('input', { placeholder: props.placeholder, value: props.value, onChange: props.onChange }))
// Desktop's SearchField takes the new string, not the event (apps/desktop/src/components/ui/search-field.tsx).
const SearchField = component('SearchField', props =>
  jsx('input', { placeholder: props.placeholder, value: props.value, onChange: event => props.onChange(event.target.value) })
)
const Card = passthrough('Card')
const CardContent = passthrough('CardContent')
const CardHeader = passthrough('CardHeader')
const CardTitle = component('CardTitle', props => jsx('h2', { children: props.children }))

function atom(initial) {
  const listeners = new Set()
  const store = {
    value: initial,
    get: () => store.value,
    set: next => {
      store.value = next
      listeners.forEach(fn => fn(next))
    },
    subscribe: fn => {
      listeners.add(fn)
      return () => listeners.delete(fn)
    }
  }
  return store
}

function useValue(store) {
  const [value, setValue] = useState(store.get())
  useEffect(() => store.subscribe(setValue), [store])
  return value
}

function memoryStorage() {
  const values = new Map()
  return {
    values,
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key)
  }
}

// --- build the sandbox --------------------------------------------------------------------

const storage = { local: memoryStorage(), session: memoryStorage() }
const notifications = []
const calls = { rest: [], reveal: [], clipboard: [], navigate: [], newChat: [], setDraft: [], composerSubmit: 0 }

const host = {
  state: { focusedStoredSessionId: atom(scenario.session || null), busy: atom(false) },
  navigate: path => calls.navigate.push(path),
  notify: note => notifications.push(note),
  newChat: () => calls.newChat.push(true),
  composer: {
    setDraft: async (target, draft) => {
      calls.setDraft.push({ target, draft })
      // A mounted composer claims the draft; the default (false) is the "no surface answered"
      // case, which is what makes the clipboard fallback reachable.
      return scenario.draftWorks === true
    },
    submit: async () => {
      calls.composerSubmit += 1
    }
  }
}

const ctx = {
  storage: {
    get: (key, fallback) => (storage.local.values.has(key) ? JSON.parse(storage.local.values.get(key)) : fallback),
    set: (key, value) => storage.local.setItem(key, JSON.stringify(value))
  },
  rest: path => {
    calls.rest.push(path)
    return defer(path)
  },
  os: {
    revealPath: async path => {
      calls.reveal.push(path)
      return false
    },
    writeClipboard: async text => {
      calls.clipboard.push(text)
      return true
    }
  },
  onEvent: () => () => {},
  registerMany: list => registrations.push(...list)
}

const registrations = []
let webRoot = null

const sandbox = {
  console,
  setTimeout,
  Promise,
  React,
  useState,
  useEffect,
  useMemo,
  useValue,
  jsx,
  jsxs,
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Codicon,
  CopyButton,
  EmptyState,
  Input,
  SearchField,
  StatusDot,
  Switch,
  host,
  PALETTE_AREA: 'palette',
  ROUTES_AREA: 'routes',
  SIDEBAR_NAV_AREA: 'sidebar',
  STATUSBAR_AREAS: { right: 'statusbar.right' },
  __ctx: ctx
}

sandbox.window = sandbox
sandbox.globalThis = sandbox
// The web tab has no composer and no plugin OS bag: its clipboard is the browser's.
sandbox.navigator = {
  clipboard: {
    writeText: async text => {
      if (scenario.clipboardFail === true) throw new Error('clipboard unavailable')
      calls.clipboard.push(text)
    }
  }
}
sandbox.__HERMES_PLUGINS__ = {
  register: (name, C) => {
    if (name !== 'engagements') throw new Error(`unexpected web registration ${name}`)
    webRoot = jsx(C, {})
  }
}
sandbox.__HERMES_PLUGIN_SDK__ = {
  React,
  hooks: { useState, useEffect },
  components: { Badge, Button, Card, CardContent, CardHeader, CardTitle, Input },
  fetchJSON: path => {
    calls.rest.push(path)
    return defer(path)
  }
}
sandbox.localStorage = storage.local
sandbox.sessionStorage = storage.session

if (scenario.privacy) {
  if (scenario.frontend === 'web') storage.session.setItem('hermes.plugin.engagements.privacy', 'true')
  storage.local.setItem('privacy', 'true')
}

// --- load the front end ---------------------------------------------------------------------

let source = readFileSync(scenario.file, 'utf8')
let roots = []

if (scenario.frontend === 'desktop') {
  // The file is an ES module for Desktop's loader; strip the three import statements and the
  // default export so the same code can run as a script against the mocks above.
  source = source.replace(/^import\s+[\s\S]*?from\s+'[^']+'\s*$/gm, '')
  source = source.replace('export default {', 'const __pluginDefault = {')
  source += '\n;__pluginDefault\n'
  const plugin = vm.runInNewContext(source, sandbox, { filename: scenario.file })
  plugin.register(ctx)
  roots = registrations.filter(entry => typeof entry.render === 'function').map(entry => entry.render())
} else {
  vm.runInNewContext(source, sandbox, { filename: scenario.file })
  if (!webRoot) throw new Error('the web tab did not register')
  roots = [webRoot]
}

// --- drive it ------------------------------------------------------------------------------

function runEffects() {
  for (const effect of pendingEffects) {
    if (effect.slot.cleanup) {
      const cleanup = effect.slot.cleanup
      effect.slot.cleanup = null
      try {
        cleanup()
      } catch (error) {
        // a thrown cleanup is a test failure, not a product path
        throw error
      }
    }
    const cleanup = effect.fn()
    effect.slot.cleanup = typeof cleanup === 'function' ? cleanup : null
    effect.slot.ran = true
    effect.slot.deps = effect.deps
  }
}

let tree = []

// Every render of a step, captured before its effects run. A guard that only holds once
// effects have settled shows up here.
let passes = []

async function settle() {
  passes = []
  for (let pass = 0; pass < 40; pass += 1) {
    pendingEffects = []
    dirty = false
    tree = roots.map(renderNode)
    const view = snapshot()
    passes.push({ text: view.text, inputs: view.inputs, chips: view.chips })
    runEffects()
    await new Promise(resolve => setImmediate(resolve))
    if (!dirty && pendingEffects.length === 0) break
  }
  return { ...snapshot(), passes }
}

function findClickable(predicate) {
  let found = null
  tree.forEach(node =>
    walk(node, element => {
      if (!found && predicate(element)) found = element
    })
  )
  return found
}

function text(element) {
  return textOf(element).replace(/\s+/g, ' ').trim()
}

function snapshot() {
  const inputs = []
  const ids = []
  const chips = []
  tree.forEach(node =>
    walk(node, element => {
      if (element.type === 'input') inputs.push({ placeholder: element.props.placeholder, value: element.props.value })
      if (element.props && element.props['data-engagements-id']) ids.push(element.props['data-engagements-id'])
      if (element.props && element.props['data-engagements-chip'] !== undefined) {
        chips.push({ id: element.props['data-engagements-chip'], text: text(element) })
      }
    })
  )
  // Layout the text cannot show: whether a button keeps its label on one line, and whether the
  // element that holds it also holds a text field, which is what "beside the search" means.
  const buttons = []
  const holdsInput = node => {
    let found = false
    walk(node, element => {
      if (element.type === 'input') found = true
    })
    return found
  }
  const visit = (node, parent) => {
    if (node === null || node === undefined || typeof node === 'string') return
    if (Array.isArray(node)) {
      node.forEach(child => visit(child, parent))
      return
    }
    if (node.type === 'button') {
      const style = (node.props && node.props.style) || {}
      buttons.push({
        text: text(node),
        nowrap: style.whiteSpace === 'nowrap' && style.flexShrink === 0,
        besideInput: parent ? holdsInput(parent) : false,
        // What shares the button's own container: a pair of actions wraps as one unit only
        // when nothing else is in there with them.
        group: parent ? (parent.children || []).filter(child => child && typeof child !== 'string').map(child => text(child)) : []
      })
    }
    if (node.children) node.children.forEach(child => visit(child, node))
  }
  tree.forEach(node => visit(node, null))
  return {
    text: tree.map(textOf).join('\n').replace(/[ \t]+/g, ' ').trim(),
    inputs,
    buttons,
    ids,
    chips,
    pending: requests.filter(request => !request.done).map(request => request.path),
    local: Object.fromEntries(storage.local.values),
    session: Object.fromEntries(storage.session.values),
    notifications,
    calls
  }
}

const snapshots = []

for (const step of scenario.steps || []) {
  if (step.op === 'render') {
    snapshots.push(await settle())
  } else if (step.op === 'click') {
    // A host button may carry only an icon, with its label in ``title`` (Desktop's Refresh).
    const target = findClickable(element => element.type === 'button' &&
      (text(element).includes(step.text) || element.props.title === step.text || element.props['aria-label'] === step.text))
    if (!target) throw new Error(`no button containing ${JSON.stringify(step.text)}; screen: ${snapshot().text}`)
    // A handler may be async (a starter prompt waits for the composer, then the clipboard): await
    // it, so a snapshot after the click sees what the click actually did.
    await target.props.onClick()
    snapshots.push(await settle())
  } else if (step.op === 'select') {
    const target = findClickable(element => element.props && element.props['data-engagements-id'] === step.id)
    if (!target) throw new Error(`no row with data-engagements-id ${JSON.stringify(step.id)}; ids: ${snapshot().ids.join(', ')}`)
    target.props.onClick()
    snapshots.push(await settle())
  } else if (step.op === 'type') {
    const target = findClickable(element => element.type === 'input')
    if (!target) throw new Error('no search field on the page')
    target.props.onChange({ target: { value: step.value } })
    snapshots.push(await settle())
  } else if (step.op === 'togglePrivacy') {
    if (scenario.frontend === 'desktop') {
      const target = findClickable(element => element.type === 'button' && element.props['data-switch'] !== undefined)
      if (!target) throw new Error('no privacy switch on the page')
      target.props.onClick()
    } else {
      const target = findClickable(element => element.type === 'button' && /^Privacy (on|off)$/.test(text(element)))
      if (!target) throw new Error(`no privacy button; screen: ${snapshot().text}`)
      target.props.onClick()
    }
    snapshots.push(await settle())
  } else if (step.op === 'resolve') {
    takeRequest(step.path).resolve(step.body)
    snapshots.push(await settle())
  } else if (step.op === 'reject') {
    takeRequest(step.path).reject(new Error(step.message || 'request failed'))
    snapshots.push(await settle())
  } else if (step.op === 'atom') {
    if (!host.state[step.name]) throw new Error(`no atom ${step.name}`)
    host.state[step.name].set(step.value)
    snapshots.push(await settle())
  } else if (step.op === 'snapshot') {
    snapshots.push(await settle())
  } else {
    throw new Error(`unknown step ${step.op}`)
  }
}

process.stdout.write(JSON.stringify({ ok: true, snapshots }))
