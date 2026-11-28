// @ts-check
// 让 Node 原生 ESM 测试解析 Vite 风格的无后缀路径与 `import.meta.glob(...)`。
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

/**
 * @param {string} specifier
 * @param {{ parentURL?: string }} context
 * @param {(s: string, c: any) => Promise<any>} nextResolve
 */
export async function resolve(specifier, context, nextResolve) {
  if ((specifier.startsWith('./') || specifier.startsWith('../')) && context.parentURL?.startsWith('file:')) {
    const base = path.dirname(fileURLToPath(context.parentURL))
    const raw = path.resolve(base, specifier)
    if (!path.extname(raw)) {
      if (fs.existsSync(`${raw}.js`)) {
        return nextResolve(pathToFileURL(`${raw}.js`).href, context)
      }
      if (fs.existsSync(path.join(raw, 'index.js'))) {
        return nextResolve(pathToFileURL(path.join(raw, 'index.js')).href, context)
      }
    }
  }
  return nextResolve(specifier, context)
}

/**
 * @param {string} url
 * @param {any} context
 * @param {(u: string, c: any) => Promise<any>} nextLoad
 */
export async function load(url, context, nextLoad) {
  const res = await nextLoad(url, context)
  if (res.format === 'module' && typeof res.source === 'string' && res.source.includes('import.meta.glob')) {
    return {
      ...res,
      source: res.source.replace(/import\.meta\.glob\([^)]*\)/g, '({})'),
    }
  }
  if (
    res.format === 'module' &&
    res.source instanceof Uint8Array &&
    Buffer.from(res.source).includes('import.meta.glob')
  ) {
    return {
      ...res,
      source: Buffer.from(res.source)
        .toString('utf8')
        .replace(/import\.meta\.glob\([^)]*\)/g, '({})'),
    }
  }
  return res
}
