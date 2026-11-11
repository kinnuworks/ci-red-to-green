// ci-red-to-green: Desktop half. A pane with the live proof, a full page with the watch
// form and incident history, palette commands, a status-bar chip and a native notification
// when CI goes red or a fix is proven. Same engine as the chat flow
// (via /api/plugins/ci-red-to-green), so the pane, the chat and GitHub always agree.
import {
  Button,
  Input,
  PALETTE_AREA,
  PANES_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  host,
  queryClient,
  haptic,
  relativeTime,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ci-red-to-green'
const PAGE = '/ci-red-to-green'
const RED = 'var(--ui-danger, #e5484d)'
const GREEN = 'var(--ui-success, #2da44e)'
const AMBER = 'var(--ui-warning, #d29922)'
const MUTED = 'var(--ui-text-tertiary)'
const LINE = 'var(--ui-stroke-secondary)'

const mono = { fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)', fontSize: '0.75rem' }
const KEY = [ID, 'state']

function chip(text, tone) {
  const color = tone === 'fail' ? RED : tone === 'pass' ? GREEN : tone === 'warn' ? AMBER : MUTED
  return jsx('span', {
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      height: 20,
      padding: '0 8px',
      borderRadius: 999,
      fontSize: '0.6875rem',
      fontWeight: 600,
      color,
      border: `1px solid ${color}`,
      whiteSpace: 'nowrap'
    },
    children: text
  })
}

function dots(n, needed, color) {
  return jsx('span', {
    style: { display: 'inline-flex', gap: 3 },
    children: Array.from({ length: needed }, (_, i) =>
      jsx('i', { key: i, style: { width: 8, height: 8, borderRadius: 4, display: 'inline-block', background: i < n ? color : LINE } })
    )
  })
}

function row(label, right, key) {
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, padding: '7px 0', borderTop: `1px solid ${LINE}` },
    children: [jsx('div', { style: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: label }), right]
  }, key)
}

function useCiState(ctx) {
  return useQuery({
    queryKey: KEY,
    queryFn: () => ctx.rest('/state'),
    // Live updates come from the plugin's state.changed event; this is only a fallback.
    refetchInterval: 10000,
    retry: false
  })
}

function useActions(ctx) {
  const qc = useQueryClient()
  const refresh = () => qc.invalidateQueries({ queryKey: KEY })
  const onError = (msg) => (e) => host.notifyError(e, msg)
  const openPr = useMutation({
    mutationFn: () => ctx.rest('/open-pr', { method: 'POST' }),
    onSuccess: (r) => {
      haptic('success')
      host.notify({ kind: 'success', message: (r.message || 'PR opened').split('\n')[0] })
      refresh()
    },
    onError: onError('Could not open the PR')
  })
  const discard = useMutation({ mutationFn: () => ctx.rest('/discard', { method: 'POST' }), onSuccess: refresh })
  const demo = useMutation({
    mutationFn: () => ctx.rest('/demo', { method: 'POST' }),
    onSuccess: () => {
      host.notify({ kind: 'info', message: 'Demo ready. Reply fix in any chat to start.' })
      refresh()
    },
    onError: onError('Could not start the demo')
  })
  const watch = useMutation({
    mutationFn: (body) => ctx.rest('/watch', { method: 'POST', body }),
    onSuccess: (r) => {
      host.notify({ kind: 'success', message: r.message || 'Watching' })
      refresh()
    },
    onError: onError('Could not start watching')
  })
  return { openPr, discard, demo, watch }
}

function headline(inc, demo) {
  if (!inc) return null
  switch (inc.status) {
    case 'alerted':
      return 'CI is red. Reply fix in any chat to start.'
    case 'fixing':
      return 'Hermes is fixing it. The proof fills in as the tests run.'
    case 'ready':
      return demo ? 'The fix is proven. Open PR writes the commit as a patch.' : 'The fix is proven. Open the PR when you are ready.'
    case 'pr_open':
      return "PR opened. Waiting for GitHub's checks."
    case 'done':
      return demo ? 'Demo done. With a real repo, GitHub has the last word.' : "Done. GitHub's CI is green on the fix."
    case 'ci_red':
      return "GitHub's CI is still red on the fix PR. Reply fix to try again."
    default:
      return inc.status
  }
}

function WatchForm({ ctx, actions }) {
  const [repo, setRepo] = useState('')
  const [branch, setBranch] = useState('main')
  const ok = /^[\w.-]+\/[\w.-]+$/.test(repo.trim())
  return jsxs('form', {
    style: { display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' },
    onSubmit: (e) => {
      e.preventDefault()
      if (ok) actions.watch.mutate({ repo: repo.trim(), branch: branch.trim() || 'main' })
    },
    children: [
      jsx(Input, { value: repo, placeholder: 'owner/repo', onChange: (e) => setRepo(e.target.value), style: { flex: '1 1 140px', minWidth: 0 } }),
      jsx(Input, { value: branch, placeholder: 'main', onChange: (e) => setBranch(e.target.value), style: { width: 90 } }),
      jsx(Button, { size: 'sm', type: 'submit', disabled: !ok || actions.watch.isPending, children: actions.watch.isPending ? 'Starting…' : 'Watch' })
    ]
  })
}

function Proof({ inc }) {
  const p = inc.proof
  const rows = []
  if (p) {
    for (const [t, st] of Object.entries(p.tests)) {
      const before = st.flaky
        ? chip('flaky', 'warn')
        : !st.fails_before
        ? chip('not run yet', 'dim')
        : jsxs('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 4 }, children: [chip('FAILED before', 'fail'), dots(Math.min(st.fails_before, p.needed_before), p.needed_before, RED)] })
      rows.push(
        row(
          jsx('span', { style: mono, title: t, children: t.split('::').pop() }),
          jsxs('span', {
            style: { display: 'inline-flex', alignItems: 'center', gap: 6 },
            children: [
              st.at_green ? jsx('span', { title: `${st.at_green} with the source as it was at the last green commit`, children: chip(st.at_green === 'PASSED' ? 'was green' : 'red at green', st.at_green === 'PASSED' ? 'pass' : 'warn') }) : null,
              before,
              dots(Math.min(st.passes_after, p.needed), p.needed, GREEN)
            ]
          }),
          t
        )
      )
    }
    rows.push(row('Full suite', chip(p.suite, p.suite === 'no new failures' ? 'pass' : p.suite === 'not run' ? 'dim' : 'fail'), 'suite'))
    rows.push(row('Test files', Object.keys(p.protected).length ? chip('changed: refused', 'fail') : chip('untouched', 'pass'), 'tests'))
    rows.push(row('Changed', jsx('span', { style: { ...mono, color: MUTED }, children: Object.keys(p.source).join(', ') || 'nothing yet' }), 'changed'))
  } else {
    for (const t of inc.tests) {
      rows.push(row(jsx('span', { style: mono, title: t, children: t.split('::').pop() }), chip(inc.status === 'done' ? 'fixed' : 'failing', inc.status === 'done' ? 'pass' : 'fail'), t))
    }
  }
  return jsx('div', { children: rows })
}

function blocking(reasons) {
  return reasons.filter((r) => !/failing runs before|passing runs after|no source change/.test(r))
}

function Suspects({ inc }) {
  const s = inc.suspects || []
  if (!s.length) return null
  const c = s[s.length - 1]
  const text = s.length === 1 ? `Came in with ${c.sha.slice(0, 7)} "${c.title}"${c.author ? ' by ' + c.author : ''}` : `${s.length} commits since the last green run (${inc.last_green}), newest ${c.sha.slice(0, 7)} "${c.title}"`
  return jsx('div', { style: { fontSize: '0.75rem', color: MUTED }, children: text })
}

function Diff({ text, max }) {
  if (!text) return null
  const lines = text.split('\n').slice(0, max || 400)
  return jsx('pre', {
    style: { ...mono, margin: 0, padding: 8, borderRadius: 6, border: `1px solid ${LINE}`, overflow: 'auto', maxHeight: max ? 220 : 480, lineHeight: 1.45 },
    children: lines.map((l, i) =>
      jsx('div', {
        style: {
          color: l.startsWith('+') && !l.startsWith('+++') ? GREEN : l.startsWith('-') && !l.startsWith('---') ? RED : l.startsWith('@@') ? MUTED : 'inherit',
          whiteSpace: 'pre'
        },
        children: l || ' '
      }, i)
    )
  })
}

function Incident({ s, actions, wide }) {
  const inc = s.incident
  const p = inc.proof
  const canOpen = p && p.accepted && (inc.status === 'ready' || inc.status === 'fixing')
  const links = []
  if (inc.pr_url && !s.demo) links.push(jsx('a', { href: inc.pr_url, target: '_blank', rel: 'noreferrer', style: { color: 'var(--ui-accent)' }, children: 'Pull request' }, 'pr'))
  if (inc.pr_run_url) links.push(jsx('a', { href: inc.pr_run_url, target: '_blank', rel: 'noreferrer', style: { color: 'var(--ui-accent)' }, children: "GitHub's check" }, 'run'))
  if (inc.run_url) links.push(jsx('a', { href: inc.run_url, target: '_blank', rel: 'noreferrer', style: { color: MUTED }, children: `Red run #${inc.run_number}` }, 'red'))
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 8 },
    children: [
      jsx('div', { style: { fontWeight: 600, color: inc.status === 'done' ? GREEN : inc.status === 'alerted' || inc.status === 'ci_red' ? RED : 'inherit' }, children: headline(inc, s.demo) }),
      jsx('div', { style: { color: MUTED, ...mono }, children: `run #${inc.run_number} · ${inc.branch} · ${inc.sha}` }),
      jsx(Suspects, { inc }),
      jsx(Proof, { inc }),
      // Progress shows in the dots; only list reasons that block the PR for another cause.
      p && !p.accepted && blocking(p.reasons).length
        ? jsx('div', { style: { color: RED, fontSize: '0.75rem' }, children: blocking(p.reasons).join('; ') })
        : null,
      inc.diff ? jsx(Diff, { text: inc.diff, max: wide ? 0 : 40 }) : null,
      inc.status === 'ready' || inc.status === 'fixing'
        ? jsxs('div', {
            style: { display: 'flex', gap: 8, marginTop: 4 },
            children: [
              jsx(Button, { size: 'sm', disabled: !canOpen || actions.openPr.isPending, onClick: () => actions.openPr.mutate(), children: actions.openPr.isPending ? 'Opening…' : s.demo ? 'Open PR (patch)' : 'Open PR' }),
              jsx(Button, { size: 'sm', variant: 'ghost', disabled: actions.discard.isPending, onClick: () => actions.discard.mutate(), children: 'Discard' })
            ]
          })
        : null,
      links.length ? jsx('div', { style: { display: 'flex', gap: 12, fontSize: '0.75rem' }, children: links }) : null
    ]
  })
}

function Header({ s }) {
  const inc = s && s.incident
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 8 },
    children: [
      jsx(StatusDot, { status: !inc ? (s && s.watching ? 'ok' : 'idle') : inc.status === 'done' ? 'ok' : inc.status === 'pr_open' || inc.status === 'fixing' ? 'busy' : 'error' }),
      jsx('div', { style: { fontWeight: 600, whiteSpace: 'nowrap' }, children: 'CI red to green' }),
      s && s.demo ? chip('demo', 'warn') : null,
      jsx('div', {
        style: { marginLeft: 'auto', color: MUTED, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', ...mono },
        children: s && s.watch ? `${s.watch.repo} @ ${s.watch.branch}` : 'not watching'
      })
    ]
  })
}

function Body({ ctx, wide }) {
  const q = useCiState(ctx)
  const actions = useActions(ctx)
  const pad = { padding: wide ? 24 : 12, fontSize: '0.8125rem', display: 'flex', flexDirection: 'column', gap: 10, maxWidth: wide ? 820 : undefined }
  if (q.isError) {
    return jsxs('div', {
      style: pad,
      children: [
        jsx('div', { style: { fontWeight: 600 }, children: 'CI red to green' }),
        jsx('div', { style: { color: MUTED }, children: 'The plugin is not loaded in this gateway. Enable ci-red-to-green and restart the gateway.' })
      ]
    })
  }
  const s = q.data
  if (!s) return jsx('div', { style: { ...pad, color: MUTED }, children: 'Loading…' })
  const inc = s.incident
  const demoBtn = jsx(Button, { size: 'sm', variant: 'ghost', disabled: actions.demo.isPending, onClick: () => actions.demo.mutate(), children: 'Try the demo' })
  const parts = [jsx(Header, { s }, 'h')]
  if (inc) {
    parts.push(jsx(Incident, { s, actions, wide }, 'i'))
  } else {
    parts.push(
      jsx('div', {
        style: { color: MUTED },
        children: s.watch
          ? s.watching
            ? 'Watching. The next red run shows up here, in your chat and as a notification.'
            : 'Send any /ci command in chat to resume watching after a restart.'
          : 'Watch a GitHub repo whose Actions run pytest or go test. When a run goes red, Hermes reproduces it, fixes the source and offers a PR only after pytest proves the fix.'
      }, 'e')
    )
  }
  if (s.queue && s.queue.length) {
    parts.push(
      jsx('div', {
        style: { fontSize: '0.75rem', color: MUTED },
        children: 'Queued: ' + s.queue.map((q) => `${q.repo} run #${q.run_number}`).join(', ')
      }, 'q')
    )
  }
  if (wide && s.watches && s.watches.length > 1) {
    parts.push(jsx('div', { style: { fontSize: '0.75rem', color: MUTED }, children: 'Watching ' + s.watches.map((w) => `${w.repo} @ ${w.branch}`).join(' · ') }, 'ws'))
  }
  if (wide || !s.watch || (inc && inc.status === 'done')) {
    parts.push(jsx(WatchForm, { ctx, actions }, 'w'))
    parts.push(jsxs('div', { style: { display: 'flex', alignItems: 'center', gap: 8, color: MUTED, fontSize: '0.75rem' }, children: [demoBtn, 'A real humanize bug on a bundled copy. No token or GitHub needed.'] }, 'd'))
  }
  if (wide && s.history && s.history.length) {
    parts.push(jsx('div', { style: { fontWeight: 600, marginTop: 12 }, children: 'Earlier incidents' }, 'hh'))
    parts.push(
      jsx('div', {
        children: s.history.map((h, i) =>
          row(
            jsx('span', { style: mono, children: `${h.repo} #${h.run_number} · ${h.first_test.split('::').pop()}${h.tests > 1 ? ` +${h.tests - 1}` : ''}` }),
            jsxs('span', {
              style: { display: 'inline-flex', gap: 8, alignItems: 'center' },
              children: [
                h.opened_at ? jsx('span', { style: { color: MUTED, fontSize: '0.75rem' }, children: relativeTime(h.opened_at * 1000) }) : null,
                h.pr_url && h.pr_url.startsWith('http') ? jsx('a', { href: h.pr_url, target: '_blank', rel: 'noreferrer', style: { color: 'var(--ui-accent)', fontSize: '0.75rem' }, children: 'PR' }) : null,
                chip(h.status === 'done' ? 'green' : h.status, h.status === 'done' ? 'pass' : h.status === 'ci_red' ? 'fail' : 'dim')
              ]
            }),
            i
          )
        )
      }, 'hl')
    )
  }
  parts.push(jsx('div', { style: { color: MUTED, fontSize: '0.6875rem', marginTop: 4 }, children: 'Results come from test output recorded by the plugin. The PR holds source files only.' }, 'f'))
  return jsx('div', { style: pad, children: parts })
}

const NOTES = {
  alerted: (inc) => ({ title: `CI red · run #${inc.run_number}`, body: `${inc.tests[0].split('::').pop()}${inc.tests.length > 1 ? ` +${inc.tests.length - 1} more` : ''}. Reply fix to start.` }),
  ready: (inc) => ({ title: 'Fix proven', body: `Run #${inc.run_number}: failed before, passed 3 times after. Open the PR from the CI pane.` }),
  done: (inc) => ({ title: 'CI green', body: `GitHub's CI is green on the fix for run #${inc.run_number}.` }),
  ci_red: (inc) => ({ title: 'Fix PR still red', body: `GitHub's CI is red on the fix for run #${inc.run_number}.` })
}

function Chip({ ctx }) {
  const q = useCiState(ctx)
  const inc = q.data && q.data.incident
  const last = useRef(null)
  useEffect(() => {
    const key = inc ? `${inc.run_number}:${inc.status}` : null
    if (last.current !== null && key && key !== last.current && NOTES[inc.status]) {
      ctx.os.notify({ ...NOTES[inc.status](inc), activate: PAGE })
    }
    last.current = key || ''
  }, [inc && inc.run_number, inc && inc.status])
  if (!q.data || !q.data.watch) return null
  const label = !inc
    ? 'CI ok'
    : { alerted: `CI red · #${inc.run_number}`, fixing: 'CI fixing', ready: 'Fix ready', pr_open: 'PR checks…', done: 'CI green', ci_red: 'PR red' }[inc.status] || inc.status
  const color = !inc || inc.status === 'done' ? GREEN : inc.status === 'alerted' || inc.status === 'ci_red' ? RED : 'var(--ui-accent)'
  return jsxs('button', {
    type: 'button',
    onClick: () => host.navigate(PAGE),
    style: { display: 'inline-flex', alignItems: 'center', gap: 5, padding: '0 6px', fontSize: '0.6875rem', color: 'var(--ui-text-tertiary)', background: 'none', border: 0, cursor: 'pointer' },
    title: q.data.watch.repo,
    children: [jsx('i', { style: { width: 7, height: 7, borderRadius: 4, display: 'inline-block', background: color } }), label]
  })
}

export default {
  id: ID,
  name: 'CI red to green',
  register(ctx) {
    ctx.onEvent(`plugin.${ID}.state.changed`, () => queryClient.invalidateQueries({ queryKey: KEY }))
    ctx.registerMany([
      { id: 'pane', area: PANES_AREA, title: 'CI red to green', data: { placement: 'right', width: '360px' }, render: () => jsx(Body, { ctx }) },
      { id: 'page', area: ROUTES_AREA, data: { path: PAGE }, render: () => jsx(Body, { ctx, wide: true }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PAGE, label: 'CI red to green', codicon: 'pass' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(Chip, { ctx }) },
      { id: 'open', area: PALETTE_AREA, data: { id: `${ID}.open`, label: 'CI red to green: Open', keywords: ['ci', 'pytest', 'github', 'red', 'fix'], run: () => host.navigate(PAGE) } },
      {
        id: 'demo',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.demo`,
          label: 'CI red to green: Try the demo',
          keywords: ['ci', 'demo', 'humanize'],
          run: async () => {
            try {
              await ctx.rest('/demo', { method: 'POST' })
              queryClient.invalidateQueries({ queryKey: KEY })
              host.navigate(PAGE)
              host.notify({ kind: 'info', message: 'Demo ready. Reply fix in any chat to start.' })
            } catch (e) {
              host.notifyError(e, 'Could not start the demo')
            }
          }
        }
      },
      {
        id: 'open-pr',
        area: PALETTE_AREA,
        data: {
          id: `${ID}.open-pr`,
          label: 'CI red to green: Open the proven PR',
          keywords: ['ci', 'pr', 'pull request'],
          run: async () => {
            try {
              const r = await ctx.rest('/open-pr', { method: 'POST' })
              host.notify({ kind: 'success', message: (r.message || '').split('\n')[0] })
              queryClient.invalidateQueries({ queryKey: KEY })
            } catch (e) {
              host.notifyError(e, 'Could not open the PR')
            }
          }
        }
      }
    ])
  }
}
