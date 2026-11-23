// @ts-check
// 连接途中停止 / 卸载：AsrStream 不能在 getUserMedia 落定后继续建音频图、留麦克风。
// 不碰真实麦克风：navigator.mediaDevices.getUserMedia 由这里手动兑现。
import assert from 'node:assert/strict'

const { AsrStream } = await import('../../web/src/api/asrStream.js')

/** @param {(stream: import('../../web/src/api/asrStream.js').AsrStream) => Promise<void>|null} action */
async function scenario(action) {
  /** @type {(value: any) => void} */
  let resolveMic = () => {}
  const pending = new Promise((resolve) => {
    resolveMic = resolve
  })
  const track = {
    stopped: false,
    stop() {
      this.stopped = true
    },
  }
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { mediaDevices: { getUserMedia: () => pending } },
  })

  let closed = 0
  const stream = new AsrStream({
    onEvent: () => {},
    onClosed: () => {
      closed += 1
    },
  })
  const starting = stream.start()
  const stopping = action(stream)
  resolveMic({ getTracks: () => [track] })
  await starting
  if (stopping) await stopping

  // start 已经收摊，且 onClosed 只报一次。
  assert.equal(stream._closed, true)
  assert.equal(closed, 1)
  assert.equal(track.stopped, true)
  assert.equal(stream._mic, null)
  assert.equal(stream._ctx, null)
}

await scenario((stream) => {
  stream.abort()
  return null
})
await scenario((stream) => stream.stop())

console.log('asr stream closed while connecting: ok')
