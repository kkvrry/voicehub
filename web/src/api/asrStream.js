// @ts-check
// 流式识别客户端：麦克风 → WebSocket → 服务端 VAD 分段 → 文字事件。
//
// 与一键式 MicButton 的分工：那边是"点一下、录一句、整段上传"；这边是
// 麦克风常开、服务端判断说完没有（Silero VAD，神经模型），说完一段就推
// 一段文字——连续对话用它，识别在哪台引擎上跑仍然由服务端决定。
//
// 协议见 app/api/sections/studio.py 的 /asr/stream 文档：第一条文本是
// {"type":"start","sample_rate":…}（AudioContext 的实际采样率，服务端统一
// 重采样），之后二进制 int16 单声道 PCM，收尾 {"type":"stop"}；服务端推
// ready / speech / partial / segment / error / done。partial 是说话中的
// 部分识别（"边说边出字"，文字随说话推进变化），segment 才是一段说完的定稿。

/** 100ms 一帧（@16k = 1600 样本）：延迟与开销的平衡点。 */
const FRAME_SAMPLES = 1600

/** AudioWorklet 源码：把 float32 采样聚成 100ms 的 int16 帧交给主线程。 */
const WORKLET_CODE = `
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super()
    this._buf = new Int16Array(${FRAME_SAMPLES})
    this._n = 0
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch) {
      for (let i = 0; i < ch.length; i += 1) {
        const s = Math.max(-1, Math.min(1, ch[i]))
        this._buf[this._n++] = s < 0 ? s * 0x8000 : s * 0x7fff
        if (this._n === this._buf.length) {
          this.port.postMessage(this._buf.slice(0))
          this._n = 0
        }
      }
    }
    return true
  }
}
registerProcessor('pcm-tap', PcmTap)
`

/**
 * @typedef {object} StreamEvent
 * @property {'ready'|'speech'|'partial'|'segment'|'error'|'done'} type
 * @property {string} [text] segment：这段话的定稿；partial：说话中的部分识别
 * @property {string} [message] error 事件：中文说明
 * @property {string} [error] segment 事件：识别失败的原因（文字为空时）
 */

/**
 * @typedef {object} AsrStreamHandlers
 * @property {(event: StreamEvent) => void} onEvent 服务端推来的每一件事
 * @property {() => void} [onClosed] 连接关闭（无论主动还是异常）
 */

export class AsrStream {
  /** @param {AsrStreamHandlers} handlers */
  constructor(handlers) {
    this._handlers = handlers
    /** @type {WebSocket|null} */
    this._ws = null
    /** @type {AudioContext|null} */
    this._ctx = null
    /** @type {MediaStream|null} */
    this._mic = null
    /** @type {AudioWorkletNode|null} */
    this._tap = null
    /** @type {null | ((value?: unknown) => void)} */
    this._onDone = null
    this._closed = false
  }

  /** 打开麦克风、连上服务、开始收音。失败抛 Error（message 可直接给人看）。 */
  async start() {
    this._closed = false
    // 平板用局域网地址（http://192.168.x.x）打开时，浏览器不把它算作安全上下文，
    // `navigator.mediaDevices` 直接是 undefined——照原样调就是一句英文的
    // "Cannot read properties of undefined (reading 'getUserMedia')"，看不出该改什么。
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(
        window.isSecureContext
          ? '这个浏览器没有给麦克风接口，换 Chrome / Edge 再试。'
          : '这个地址不能开麦克风：浏览器只在 https 或 localhost 下给麦克风。把地址换成 http://localhost:端口，或给这台机器配 https。'
      )
    }
    try {
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      })
      this._mic = mic
      // 请求 16k 是为了让服务端少干活；浏览器不理这个请求也没关系——
      // 把实际采样率报给服务端，它统一重采样。
      const ctx = new AudioContext({ sampleRate: 16000 })
      this._ctx = ctx
      const url = URL.createObjectURL(new Blob([WORKLET_CODE], { type: 'text/javascript' }))
      try {
        await ctx.audioWorklet.addModule(url)
      } finally {
        URL.revokeObjectURL(url)
      }
      await ctx.resume()
      const source = ctx.createMediaStreamSource(mic)
      const tap = new AudioWorkletNode(ctx, 'pcm-tap')
      this._tap = tap
      // 不连 destination 麦克风会漏进扬声器；经一个静音增益落到 graph 上
      // 是为了让处理链真的被拉动。
      const mute = ctx.createGain()
      mute.gain.value = 0
      source.connect(tap)
      tap.connect(mute)
      mute.connect(ctx.destination)

      await this._connect()
      tap.port.onmessage = (event) => {
        if (this._ws?.readyState === WebSocket.OPEN) {
          this._ws.send(/** @type {Int16Array} */ (event.data).buffer)
        }
      }
    } catch (error) {
      // 麦克风已拿到、音频图已建好时连接失败：不收拾就会留下一路开着的
      // 麦克风与一个没人关的 AudioContext。
      this._finish()
      throw error
    }
  }

  /** 收尾：把没说完的话冲出来，等 done 再关。返回前一切已落定。 */
  async stop() {
    if (this._ws?.readyState === WebSocket.OPEN) {
      const finished = new Promise((resolve) => {
        this._onDone = resolve
      })
      this._ws.send(JSON.stringify({ type: 'stop' }))
      // 收尾段要等识别跑完，几秒是正常的；超时只是兜底，不能让界面一直挂着。
      await Promise.race([finished, new Promise((r) => setTimeout(r, 10000))])
    }
    this._finish()
  }

  /** 直接断开（不等冲帧）——组件卸载时用。 */
  abort() {
    this._finish()
  }

  // ---- 内部 -------------------------------------------------------------- //

  async _connect() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/api/v1/asr/stream`)
    this._ws = ws
    ws.binaryType = 'arraybuffer'
    await new Promise((resolve, reject) => {
      const fail = () => reject(new Error('流式识别服务连不上。'))
      ws.onopen = () => {
        ws.onerror = null
        ws.onclose = null
        resolve(undefined)
      }
      ws.onerror = fail
      ws.onclose = fail
    })
    ws.onmessage = (event) => {
      if (typeof event.data !== 'string') return
      let payload
      try {
        payload = JSON.parse(event.data)
      } catch {
        return
      }
      if (payload?.type === 'done' && this._onDone) {
        const done = this._onDone
        this._onDone = null
        done()
      }
      this._handlers.onEvent(payload)
    }
    ws.onclose = () => {
      this._ws = null
      this._finish()
    }
    ws.send(
      JSON.stringify({ type: 'start', sample_rate: this._ctx?.sampleRate || 16000 }),
    )
  }

  /** 幂等收摊：先到的 onclose / stop / abort 负责清理，后来者不再重复回调。 */
  _finish() {
    if (this._closed) return
    this._closed = true
    try {
      if (this._ws?.readyState === WebSocket.OPEN) this._ws.close()
    } catch {
      /* 已经断就算了 */
    }
    this._ws = null
    this._tap?.port.close()
    this._tap?.disconnect()
    this._tap = null
    this._mic?.getTracks().forEach((track) => track.stop())
    this._mic = null
    void this._ctx?.close()
    this._ctx = null
    this._onDone?.()
    this._onDone = null
    this._handlers.onClosed?.()
  }
}
