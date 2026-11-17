// @ts-check
// 有声书插件自己的接口。宿主只提供通用三件（清单 / 自描述 / 配置），
// 书籍相关的端点全部在这里，宿主 api.js 不再认识它们。
import { errorMessage, isTimeout, request, toLogin } from '../../api'

/**
 * @param {string} key
 */
function base(key) {
  return `/api/v1/producers/${key}`
}

/**
 * @param {string} key
 */
export const overview = (key) => request(`${base(key)}/overview`)

/**
 * 上传书稿：multipart 走不了 request 的 JSON 通道，单独一条。
 *
 * @param {string} key
 * @param {File} file
 * @param {string} name
 */
export async function importProject(key, file, name = '') {
  const form = new FormData()
  form.append('file', file, file.name)
  if (name) form.append('name', name)
  let resp
  try {
    resp = await fetch(`${base(key)}/projects/import`, {
      method: 'POST',
      body: form,
      // 解析整本书（分章、拆句）可能要几十秒，别按普通请求的 30 秒掐断。
      signal: AbortSignal.timeout(180_000),
    })
  } catch (error) {
    if (isTimeout(error)) throw new Error('导入超时：本地服务没有响应。')
    throw error
  }
  if (resp.status === 401) throw toLogin()
  if (!resp.ok) throw new Error(await errorMessage(resp))
  return resp.json()
}

/**
 * @param {string} key
 * @param {string} id
 */
export const project = (key, id) => request(`${base(key)}/projects/${id}`)

/**
 * @param {string} key
 * @param {string} id
 */
export const marks = (key, id) => request(`${base(key)}/projects/${id}/marks`)

/**
 * 分章模式与文本处理预设：与后端同一份 patterns.json。
 *
 * @param {string} key
 */
export const patterns = (key) => request(`${base(key)}/patterns`)

/**
 * @param {string} key
 */
export const splitOptionsMeta = (key) => request(`${base(key)}/split/options`)

/**
 * @param {string} key
 * @param {string} id
 * @param {any} options
 */
export const saveSplitOptions = (key, id, options) =>
  request(`${base(key)}/projects/${id}/split-options`, { method: 'PUT', body: options })

/**
 * @param {string} key
 * @param {string} id
 */
export const source = (key, id) => request(`${base(key)}/projects/${id}/source`)

/**
 * @param {string} key
 * @param {string} id
 * @param {string} patternKey
 */
export const previewSplit = (key, id, patternKey) =>
  request(`${base(key)}/projects/${id}/split/preview`, {
    method: 'POST',
    body: { pattern_key: patternKey },
  })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} patternKey
 */
export const splitProject = (key, id, patternKey) =>
  request(`${base(key)}/projects/${id}/split`, {
    method: 'POST',
    body: { pattern_key: patternKey },
  })

/**
 * @param {string} key
 * @param {string} id
 * @param {any} rules
 */
export const previewText = (key, id, rules) =>
  request(`${base(key)}/projects/${id}/text/preview`, { method: 'POST', body: rules })

/**
 * @param {string} key
 * @param {string} id
 * @param {any} rules
 */
export const applyText = (key, id, rules) =>
  request(`${base(key)}/projects/${id}/text/apply`, { method: 'POST', body: rules })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} chapterId
 */
export const deleteChapter = (key, id, chapterId) =>
  request(`${base(key)}/projects/${id}/chapters/${chapterId}`, { method: 'DELETE' })

/**
 * 改书名与作者：只传要改的那一项。
 *
 * @param {string} key
 * @param {string} id
 * @param {any} body
 */
export const updateProject = (key, id, body) =>
  request(`${base(key)}/projects/${id}`, { method: 'PUT', body })

/**
 * @param {string} key
 * @param {any} body
 */
export const createProject = (key, body) =>
  request(`${base(key)}/projects`, { method: 'POST', body })

/**
 * @param {string} key
 * @param {string} id
 */
export const deleteProject = (key, id) =>
  request(`${base(key)}/projects/${id}`, { method: 'DELETE' })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} chapterId
 */
export const chapter = (key, id, chapterId) =>
  request(`${base(key)}/projects/${id}/chapters/${chapterId}`)

/**
 * @param {string} key
 * @param {string} id
 * @param {string} chapterId
 * @param {string} title
 */
export const renameChapter = (key, id, chapterId, title) =>
  request(`${base(key)}/projects/${id}/chapters/${chapterId}`, {
    method: 'PUT',
    body: { title },
  })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} chapterId
 */
export const resplitChapter = (key, id, chapterId) =>
  request(`${base(key)}/projects/${id}/chapters/${chapterId}/resplit`, { method: 'POST' })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} step
 * @param {any} body
 */
export const runStep = (key, id, step, body) =>
  request(`${base(key)}/projects/${id}/steps/${step}`, {
    method: 'POST',
    body,
    timeout: 120_000,
  })

/**
 * @param {string} key
 * @param {string} jobId
 */
export const job = (key, jobId) => request(`${base(key)}/jobs/${jobId}`)

/**
 * @param {string} key
 * @param {string} jobId
 */
export const cancelJob = (key, jobId) =>
  request(`${base(key)}/jobs/${jobId}/cancel`, { method: 'POST' })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} rowId
 */
export const retryRow = (key, id, rowId) =>
  request(`${base(key)}/projects/${id}/rows/${rowId}/audio`, {
    method: 'POST',
    // 单行重试是同步合成，慢的引擎要几分钟。
    timeout: 600_000,
  })

/**
 * 行级参数：这一行能调哪些（按引擎）、现在各是多少。
 *
 * @param {string} key
 * @param {string} id
 * @param {string} rowId
 */
export const rowParams = (key, id, rowId) =>
  request(`${base(key)}/projects/${id}/rows/${rowId}/params`)

/**
 * @param {string} key
 * @param {string} id
 * @param {string} rowId
 * @param {Record<string, any>} params
 */
export const saveRowParams = (key, id, rowId, params) =>
  request(`${base(key)}/projects/${id}/rows/${rowId}/params`, {
    method: 'PUT',
    body: { params },
  })

// 合并是同步请求：一章几十行要逐个编码再拼，长章节按分钟计。
// 不设上限——进度与取消走 mergeProgress / cancelMerge，不靠 HTTP 超时兜底。
/**
 * @param {string} key
 * @param {string} id
 * @param {string} chapterId
 */
export const mergeChapter = (key, id, chapterId) =>
  request(`${base(key)}/projects/${id}/chapters/${chapterId}/merge`, {
    method: 'POST',
    timeout: 0,
  })

/**
 * @param {string} key
 * @param {string} id
 * @param {AbortSignal} signal
 */
export const mergeProgress = (key, id, signal) =>
  request(`${base(key)}/projects/${id}/merge-progress`, { signal })

/**
 * @param {string} key
 * @param {string} id
 */
export const cancelMerge = (key, id) =>
  request(`${base(key)}/projects/${id}/merge/cancel`, { method: 'POST' })

/**
 * 分章 ZIP：下载交给浏览器，同样只给地址。
 *
 * @param {string} key
 * @param {string} id
 */
export const exportUrl = (key, id) => `${base(key)}/projects/${id}/export`
/**
 * @param {string} key
 * @param {string} id
 */
export const exportM4bUrl = (key, id) => `${base(key)}/projects/${id}/export/m4b`

/**
 * @param {string} key
 * @param {string} id
 * @param {any} body
 */
export const setVoice = (key, id, body) =>
  request(`${base(key)}/projects/${id}/voices`, { method: 'PUT', body })

/**
 * AI 建议落地：一次事务写选中项，只作废真正变化的角色。
 *
 * @param {string} key
 * @param {string} id
 * @param {any} body
 */
export const setVoices = (key, id, body) =>
  request(`${base(key)}/projects/${id}/voices/batch`, { method: 'PUT', body })

/**
 * @param {string} key
 */
export const assistStatus = (key) => request(`${base(key)}/assist/status`)

/**
 * 分配弹窗的引擎与方式选项；默认引擎与默认方式由后端定。
 *
 * @param {string} key
 */
export const voiceOptions = (key) => request(`${base(key)}/assist/voice-options`)

/**
 * 一次结构化提案；模型只提案，应用仍走各自的写接口。
 *
 * @param {string} key
 * @param {string} task
 * @param {any} body
 */
export const assist = (key, task, body) =>
  request(`${base(key)}/assist/${task}`, {
    method: 'POST',
    body,
    // 一次对话模型调用：慢的时候一分钟以上。
    timeout: 180_000,
  })

/**
 * 把几个说话人并成一个名字：只改行上的 speaker。
 *
 * @param {string} key
 * @param {string} id
 * @param {any} body
 */
export const mergeSpeakers = (key, id, body) =>
  request(`${base(key)}/projects/${id}/speakers/merge`, { method: 'POST', body })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} rowId
 * @param {any} body
 */
export const updateRow = (key, id, rowId, body) =>
  request(`${base(key)}/projects/${id}/rows/${rowId}`, { method: 'PUT', body })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} rowId
 */
export const deleteRow = (key, id, rowId) =>
  request(`${base(key)}/projects/${id}/rows/${rowId}`, { method: 'DELETE' })

/**
 * @param {string} key
 * @param {string} id
 * @param {string} rowId
 * @param {number} at
 */
export const splitRow = (key, id, rowId, at) =>
  request(`${base(key)}/projects/${id}/rows/${rowId}/split`, {
    method: 'POST',
    body: { at },
  })

/**
 * @param {string} key
 * @param {string} id
 * @param {string[]} ids
 */
export const mergeRows = (key, id, ids) =>
  request(`${base(key)}/projects/${id}/rows/merge`, { method: 'PUT', body: { ids } })

/**
 * 自动合并：短于上限的旁白行并入下一行，对白行不动。
 *
 * @param {string} key
 * @param {string} id
 * @param {string} chapterId
 * @param {number} maxChars
 */
export const autoMergeRows = (key, id, chapterId, maxChars) =>
  request(`${base(key)}/projects/${id}/chapters/${chapterId}/rows/auto-merge`, {
    method: 'POST',
    body: { max_chars: maxChars },
  })

/**
 * 全书自动合并：逐章走一遍，没有可合并旁白的章跳过。
 *
 * @param {string} key
 * @param {string} id
 * @param {number} maxChars
 */
export const autoMergeAllRows = (key, id, maxChars) =>
  request(`${base(key)}/projects/${id}/rows/auto-merge`, {
    method: 'POST',
    body: { max_chars: maxChars },
  })

/**
 * @param {string} key
 * @param {string} id
 * @param {any} body
 */
export const batchUpdateRows = (key, id, body) =>
  request(`${base(key)}/projects/${id}/rows/batch`, { method: 'PUT', body })
