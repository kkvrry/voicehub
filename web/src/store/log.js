// @ts-check
// 日志这一域：快照（首屏与重连补帧）与 SSE 两条路合用，给的行是同一个形状（带 seq）。
// 两条都要：只用流则服务重启那几十秒的事见不到，只用快照则不实时。两条必有重叠，
// `claim` 按 `seq` 去重——那是它存在的全部理由。
//
// @ts-check —— 这一域只有 SSE 状态机与环形缓冲，标注成本低、出错代价高（重复行、
// 统计只涨不落），所以先拿它当 checkJs 的起点。
import { api } from '../api'
import { store } from './state'

/** 环形深度，与后端那个环同深（浅了补帧会只剩半段）。 */
const KEEP = 1000

/** 断了之后隔多久自己再连。不用浏览器原生重连：它只会照旧地址再要一遍，
 *  而我们要把 `since` 换成刚读到的游标。 */
const RETRY_MS = 1500

/** 退避上限：服务没起时别每 1.5 秒撞一次，日志里全是连接失败。 */
const RETRY_MAX_MS = 30_000

/** @type {EventSource | null} */
let stream = null
/** @type {ReturnType<typeof setTimeout> | undefined} */
let retryTimer
let retryMs = RETRY_MS
/** 读到的最大 `seq`：连接自己的游标，store 里那份 `cursor` 只是给界面看的同一个值。 */
let lastSeq = 0

/** 增量统计：级别计数与来源表跟着 `lines` 同进同出。
 *
 *  角标（导航、左栏）与日志页的来源下拉都读 `store.logStats` / `store.logSources`。
 *  不这么干的话，每条日志到达都要有几个消费方各把整份数组（≤1000 行）滤一遍，
 *  生成音频时后端每秒几十条——几次全量遍历叠起来，界面"什么都没干"也在掉帧。
 *  `step` 为 1 收下、-1 环形丢弃。
 */
// state.js 还没开 checkJs，`[]` 会被推成 never[]；这里按真实形状收窄一次。
const stats = /** @type {{ total: number, warning: number, error: number }} */ (
  store.logStats
)
/** @type {string[]} */
const logSources = store.logSources
/** @type {Map<string, number>} */
const sourceCounts = new Map()

/**
 * @param {{ level?: string, source?: string }} line
 * @param {number} step
 */
function tally(line, step) {
  stats.total += step
  if (line.level === 'warning') stats.warning += step
  else if (line.level === 'error') stats.error += step
  const name = line.source || 'unknown'
  const count = (sourceCounts.get(name) || 0) + step
  if (count > 0) {
    sourceCounts.set(name, count)
    // 头一次出现的来源才进列表：顺序即首次出现序，界面那边自己排序。
    if (step > 0 && count === 1) logSources.push(name)
  } else {
    sourceCounts.delete(name)
    const at = logSources.indexOf(name)
    if (at >= 0) logSources.splice(at, 1)
  }
}

/**
 * 收下一条，按 `seq` 去重——快照与流的重叠段、重连后重发的那一段都在这里被吃掉。
 * @param {any} line
 */
function claim(line) {
  const seq = Number(line?.seq) || 0
  if (seq && seq <= lastSeq) return
  if (seq) lastSeq = seq
  /** @type {any[]} */
  const lines = store.logs.lines
  lines.push(line)
  tally(line, 1)
  if (lines.length > KEEP) {
    // 环转过一圈：被挤掉的是最旧的那些，统计跟着减，角标才不会只涨不落。
    for (const gone of lines.splice(0, lines.length - KEEP)) tally(gone, -1)
  }
  store.logs.cursor = lastSeq
}

/** 把 `since` 之后缺的那一段取回来。**首屏与每次重连之前都要走一遍。** */
async function backfill() {
  try {
    const data = await api.logs(lastSeq, KEEP)
    for (const line of data.lines || []) claim(line)
    if (typeof data.cursor === 'number') lastSeq = Math.max(lastSeq, data.cursor)
    store.logs.cursor = lastSeq
    // 缺了就如实说，别假装连续。
    store.logs.dropped = Boolean(data.dropped)
  } catch {
    // 服务还没起来 / 正在重启：不报错，重连那一次会把这一段补回来。
    // 在这里弹"拉日志失败"只会让启动那一瞬间看起来像出了故障。
    store.logs.dropped = false
  }
}

function close() {
  if (!stream) return
  stream.close()
  stream = null
}

function connect() {
  close()
  stream = new EventSource(api.logStreamUrl(lastSeq))
  stream.onopen = () => {
    store.logs.connected = true
    retryMs = RETRY_MS
    // 每次连上都补一次帧：断线期间的行只有快照能带回来，重叠由 `claim` 去掉。
    backfill()
  }
  stream.addEventListener('log', (event) => {
    try {
      claim(JSON.parse(event.data))
    } catch {
      // 坏帧丢掉，不该让整条流停住。
    }
  })
  stream.onerror = () => {
    store.logs.connected = false
    close()
    clearTimeout(retryTimer)
    retryTimer = setTimeout(connect, retryMs)
    retryMs = Math.min(retryMs * 2, RETRY_MAX_MS)
  }
}

/** 开始接收，幂等（重复开会在界面上把每条日志显示两遍）。 */
export async function openLogStream() {
  if (stream) return
  await backfill()
  connect()
}

/** 打开日志页（右侧主区那一页）。数据在 App 挂载时就接上了，这里只负责换页。 */
export function openLogs() {
  store.view = 'logs'
}

/** 清屏只清视图：库里与文件里仍是全量。把"我暂时不想看"做成"删掉历史"
 *  是这一块最容易犯、也最没法挽回的误删。游标不动，新行照旧接得上。 */
export function clearLogs() {
  store.logs.lines.splice(0)
  stats.total = 0
  stats.warning = 0
  stats.error = 0
  sourceCounts.clear()
  logSources.splice(0)
}
