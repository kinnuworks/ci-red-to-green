// ci-red-to-green: Desktop half. A pane with the live proof for the current red run, and a
// status-bar chip. Same engine as the chat flow (via /api/plugins/ci-red-to-green), so the
// proof, the Open PR button and GitHub's result match what Telegram shows.
import {
  Button,
  PANES_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  host,
  haptic,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const RED = 'var(--ui-danger, #e5484d)'
const GREEN = 'var(--ui-success, #2da44e)'
const MUTED = 'var(--ui-text-tertiary)'
const LINE = 'var(--ui-stroke-secondary)'

const mono = { fontFamily: 'var(--ui-font-mono, ui-monospace, monospace)', fontSize: '0.75rem' }

function chip(text, tone) {
  const color = tone === 'fail' ? RED : tone === 'pass' ? GREEN : MUTED
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

function dots(n, needed) {
  return jsx('span', {
    style: { display: 'inline-flex', gap: 3 },
    children: Array.from({ length: needed }, (_, i) =>
      jsx('i', { key: i, style: { width: 8, height: 8, borderRadius: 4, display: 'inline-block', background: i < n ? GREEN : LINE } })
    )
  })
}

function row(label, right, key) {
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, padding: '7px 0', borderTop: `1px solid ${LINE}` },
    children: [jsx('div', { style: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }, children: label }), right]
  }, key)
}

function useCiState(ctx) {
  return useQuery({
    queryKey: [ctx.source, 'state'],
    queryFn: () => ctx.rest('/state'),
    refetchInterval: 2000,
    retry: false
  })
}

function headline(inc) {
  if (!inc) return null
  switch (inc.status) {
    case 'alerted':
      return 'CI is red. Reply fix in chat to start.'
    case 'fixing':
      return 'Hermes is fixing it. The proof fills in as pytest runs.'
    case 'ready':
      return 'The fix is proven. Open the PR when you are ready.'
    case 'pr_open':
      return "PR opened. Waiting for GitHub's checks."
    case 'done':
      return "Done. GitHub's CI is green on the fix."
    case 'ci_red':
      return "GitHub's CI is still red on the fix PR. Reply fix to try again."
    default:
      return inc.status
  }
}

function Pane({ ctx }) {
  const qc = useQueryClient()
  const q = useCiState(ctx)
  const refresh = () => qc.invalidateQueries({ queryKey: [ctx.source, 'state'] })
  const openPr = useMutation({
    mutationFn: () => ctx.rest('/open-pr', { method: 'POST' }),
    onSuccess: (r) => {
      haptic('success')
      host.notify({ kind: 'success', message: r.message || 'PR opened' })
      refresh()
    },
    onError: (e) => host.notifyError(e, 'Could not open the PR')
  })
  const discard = useMutation({
    mutationFn: () => ctx.rest('/discard', { method: 'POST' }),
    onSuccess: () => refresh()
  })

  const pad = { padding: 12, fontSize: '0.8125rem', display: 'flex', flexDirection: 'column', gap: 8 }
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
  const head = jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 8 },
    children: [
      jsx(StatusDot, { status: !inc ? (s.watching ? 'ok' : 'idle') : inc.status === 'done' ? 'ok' : inc.status === 'pr_open' || inc.status === 'fixing' ? 'busy' : 'error' }),
      jsx('div', { style: { fontWeight: 600 }, children: 'CI red to green' }),
      jsx('div', { style: { marginLeft: 'auto', color: MUTED, ...mono }, children: s.watch ? `${s.watch.repo} @ ${s.watch.branch}` : 'not watching' })
    ]
  })
  if (!s.watch) {
    return jsxs('div', { style: pad, children: [head, jsx('div', { style: { color: MUTED }, children: 'In your chat with Hermes, send /ci watch owner/repo main to start.' })] })
  }
  if (!inc) {
    return jsxs('div', {
      style: pad,
      children: [
        head,
        jsx('div', { style: { color: MUTED }, children: s.watching ? 'Watching. The next red run shows up here and in your chat.' : 'Send any /ci command in chat to resume watching after a restart.' })
      ]
    })
  }

  const p = inc.proof
  const rows = []
  if (p) {
    for (const [t, st] of Object.entries(p.tests)) {
      rows.push(
        row(
          jsx('span', { style: mono, title: t, children: t.split('::').pop() }),
          jsxs('span', {
            style: { display: 'inline-flex', alignItems: 'center', gap: 6 },
            children: [st.failed_before ? chip('FAILED before', 'fail') : chip('not seen failing', 'dim'), dots(Math.min(st.passes_after, p.needed), p.needed)]
          }),
          t
        )
      )
    }
    rows.push(row('Full suite', chip(p.suite, p.suite === 'no new failures' ? 'pass' : p.suite === 'not run' ? 'dim' : 'fail'), 'suite'))
    rows.push(row('Test files', Object.keys(p.protected).length ? chip('changed: refused', 'fail') : chip('untouched', 'pass'), 'tests'))
    rows.push(
      row(
        'Changed',
        jsx('span', { style: { ...mono, color: MUTED }, children: Object.keys(p.source).join(', ') || 'nothing yet' }),
        'changed'
      )
    )
  } else {
    for (const t of inc.tests) {
      rows.push(row(jsx('span', { style: mono, title: t, children: t.split('::').pop() }), chip(inc.status === 'done' ? 'fixed' : 'failing', inc.status === 'done' ? 'pass' : 'fail'), t))
    }
  }

  const canOpen = p && p.accepted && (inc.status === 'ready' || inc.status === 'fixing')
  const links = []
  if (inc.pr_url) links.push(jsx('a', { href: inc.pr_url, target: '_blank', rel: 'noreferrer', style: { color: 'var(--ui-accent)' }, children: 'Pull request' }, 'pr'))
  if (inc.pr_run_url) links.push(jsx('a', { href: inc.pr_run_url, target: '_blank', rel: 'noreferrer', style: { color: 'var(--ui-accent)' }, children: "GitHub's check" }, 'run'))
  if (inc.run_url) links.push(jsx('a', { href: inc.run_url, target: '_blank', rel: 'noreferrer', style: { color: MUTED }, children: `Red run #${inc.run_number}` }, 'red'))

  return jsxs('div', {
    style: pad,
    children: [
      head,
      jsx('div', { style: { fontWeight: 600, color: inc.status === 'done' ? GREEN : inc.status === 'alerted' || inc.status === 'ci_red' ? RED : 'inherit' }, children: headline(inc) }),
      jsx('div', { style: { color: MUTED, ...mono }, children: `run #${inc.run_number} · ${inc.branch} · ${inc.sha}` }),
      jsx('div', { children: rows }),
      p && !p.accepted && p.reasons.length
        ? jsx('div', { style: { color: MUTED, fontSize: '0.75rem' }, children: 'Not ready: ' + p.reasons.join('; ') })
        : null,
      inc.status === 'ready' || inc.status === 'fixing'
        ? jsxs('div', {
            style: { display: 'flex', gap: 8, marginTop: 4 },
            children: [
              jsx(Button, { size: 'sm', disabled: !canOpen || openPr.isPending, onClick: () => openPr.mutate(), children: openPr.isPending ? 'Opening…' : 'Open PR' }),
              jsx(Button, { size: 'sm', variant: 'ghost', disabled: discard.isPending, onClick: () => discard.mutate(), children: 'Discard' })
            ]
          })
        : null,
      links.length ? jsx('div', { style: { display: 'flex', gap: 12, fontSize: '0.75rem' }, children: links }) : null,
      jsx('div', { style: { color: MUTED, fontSize: '0.6875rem', marginTop: 4 }, children: 'Results come from pytest output recorded by the plugin. The PR holds source files only.' })
    ]
  })
}

function Chip({ ctx }) {
  const q = useCiState(ctx)
  const inc = q.data && q.data.incident
  if (!q.data || !q.data.watch) return null
  const label = !inc
    ? 'CI ok'
    : { alerted: `CI red · #${inc.run_number}`, fixing: 'CI fixing', ready: 'Fix ready', pr_open: 'PR checks…', done: 'CI green', ci_red: 'PR red' }[inc.status] || inc.status
  const color = !inc || inc.status === 'done' ? GREEN : inc.status === 'alerted' || inc.status === 'ci_red' ? RED : 'var(--ui-accent)'
  return jsxs('span', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 5, padding: '0 6px', fontSize: '0.6875rem', color: 'var(--ui-text-tertiary)' },
    title: q.data.watch.repo,
    children: [jsx('i', { style: { width: 7, height: 7, borderRadius: 4, display: 'inline-block', background: color } }), label]
  })
}

export default {
  id: 'ci-red-to-green',
  name: 'CI red to green',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'CI red to green',
      data: { placement: 'right', width: '340px' },
      render: () => jsx(Pane, { ctx })
    })
    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 140,
      render: () => jsx(Chip, { ctx })
    })
  }
}
