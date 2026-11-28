// @ts-check
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { beforeEach, test } from 'node:test'

register(new URL('./_vite_loader.mjs', import.meta.url))

/** @type {Map<string, string>} */
const storage = new Map()
/** @type {Array<{ url: string, init: any }>} */
const fetchCalls = []
/** @type {(url: string, init?: any) => Promise<Response>} */
let fetchImpl = async () => new Response('{}', { status: 200 })

Object.defineProperty(globalThis, 'document', {
  configurable: true,
  value: {
    documentElement: { dataset: {} },
    body: { appendChild: () => {} },
    createElement: () => ({
      addEventListener: () => {},
      removeEventListener: () => {},
      pause: () => {},
      load: () => {},
      play: async () => {},
      src: '',
      currentTime: 0,
      duration: 0,
      paused: true,
    }),
  },
})

Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: {
    localStorage: {
      /** @param {string} k */
      getItem: (k) => (storage.has(k) ? storage.get(k) ?? null : null),
      /** @param {string} k @param {string} v */
      setItem: (k, v) => {
        storage.set(k, String(v))
      },
      /** @param {string} k */
      removeItem: (k) => {
        storage.delete(k)
      },
    },
    location: { pathname: '/', search: '', assign: () => {} },
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
  },
})

Object.defineProperty(globalThis, 'fetch', {
  configurable: true,
  /** @param {string} url @param {any} init */
  value: (url, init) => {
    fetchCalls.push({ url, init })
    return fetchImpl(url, init)
  },
})

const dubbingApi = await import('../../web/src/producers/dubbing/api.js')
const dubbingStore = await import('../../web/src/producers/dubbing/store.js')

/** @param {unknown} data @param {number} [status] */
function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

beforeEach(() => {
  storage.clear()
  fetchCalls.length = 0
  dubbingStore.state.currentId = 'proj-1'
  dubbingStore.state.detail = {
    project: { id: 'proj-1', name: '演示' },
    roles: [{ id: 'r1', name: '旁白', mode: 'tts' }],
    cues: [
      { id: 'c1', role_id: 'r1', text: '第一句', start_ms: 0, end_ms: 1000 },
      { id: 'c2', role_id: '', text: '第二句', start_ms: 1000, end_ms: 2000 },
    ],
    steps: [],
    job: null,
  }
  dubbingStore.state.pending = {}
  dubbingStore.state.error = ''
  dubbingStore.state.editing = ''
})

test('mediaUrl encodes filename and version query parameters cleanly', () => {
  assert.equal(
    dubbingApi.mediaUrl('p1', 'cue', '001 句.wav', 42),
    '/api/v1/producers/dubbing/projects/p1/media/cue?name=001%20%E5%8F%A5.wav&v=42',
  )
  assert.equal(
    dubbingApi.mediaUrl('p1', 'mix', '', 'v9'),
    '/api/v1/producers/dubbing/projects/p1/media/mix?v=v9',
  )
})

test('patchCue rolls back optimistic edit on 4xx response', async () => {
  fetchImpl = async (url) => {
    if (url.includes('/api/v1/logs/client')) return jsonResponse({ ok: true })
    return jsonResponse({ error: { message: '段落不存在' } }, 404)
  }

  const cue = dubbingStore.state.detail.cues[0]
  const res = await dubbingStore.patchCue(cue, { text: '改坏了' })
  assert.equal(res, null)
  assert.equal(dubbingStore.state.detail.cues[0].text, '第一句')
  assert.equal(Boolean(dubbingStore.state.pending.c1), false)
})

test('patchCue queues and merges pending edits on 5xx, surviving refresh', async () => {
  fetchImpl = async (url) => {
    if (url.includes('/api/v1/logs/client')) return jsonResponse({ ok: true })
    if (url.endsWith('/cues/c1')) {
      return jsonResponse({ error: { message: '暂时繁忙' } }, 503)
    }
    return jsonResponse({
      project: { id: 'proj-1', name: '演示' },
      roles: [{ id: 'r1', name: '旁白', mode: 'tts' }],
      cues: [
        { id: 'c1', role_id: 'r1', text: '旧服务端', start_ms: 0, end_ms: 1000 },
        { id: 'c2', role_id: '', text: '第二句', start_ms: 1000, end_ms: 2000 },
      ],
      steps: [],
      job: null,
    })
  }

  const cue = dubbingStore.state.detail.cues[0]
  await dubbingStore.patchCue(cue, { text: '本地新字' })
  await dubbingStore.patchCue(dubbingStore.state.detail.cues[0], { role_id: 'r2' })
  assert.equal(dubbingStore.state.pending.c1, true)

  // 轮询拉回旧服务端文本时，挂起的合并补丁仍要覆盖在界面上。
  await dubbingStore.refresh({ silent: true })
  assert.equal(dubbingStore.state.detail.cues[0].text, '本地新字')
  assert.equal(dubbingStore.state.detail.cues[0].role_id, 'r2')
})

test('refresh discards stale snapshot when a mutation runs while detail is in flight', async () => {
  /** @type {(res: Response) => void} */
  let resolveDetail = () => {}
  const pendingDetail = new Promise((resolve) => {
    resolveDetail = resolve
  })

  fetchImpl = async (url) => {
    if (url.endsWith('/projects/proj-1')) return pendingDetail
    if (url.endsWith('/cues/c1')) {
      return jsonResponse({ id: 'c1', role_id: 'r1', text: '刚保存的新字', start_ms: 0, end_ms: 1000 })
    }
    return jsonResponse({ ok: true })
  }

  const refreshing = dubbingStore.refresh({ silent: true })
  await dubbingStore.patchCue(dubbingStore.state.detail.cues[0], { text: '刚保存的新字' })

  resolveDetail(
    jsonResponse({
      project: { id: 'proj-1', name: '演示' },
      roles: [{ id: 'r1', name: '旁白', mode: 'tts' }],
      cues: [
        { id: 'c1', role_id: 'r1', text: '过期快照', start_ms: 0, end_ms: 1000 },
        { id: 'c2', role_id: '', text: '第二句', start_ms: 1000, end_ms: 2000 },
      ],
      steps: [],
      job: null,
    }),
  )
  const snapshot = await refreshing
  assert.equal(snapshot, null)
  assert.equal(dubbingStore.state.detail.cues[0].text, '刚保存的新字')
})
