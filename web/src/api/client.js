/**
 * HTTP 客户端：统一前缀、JSON 编解码、错误归一。
 *
 * 用浏览器原生 fetch，不引入 axios —— 依赖越少，安装面越小。
 *
 * 错误归一的依据是**后端实际的错误体**（backend/app.py）：
 *   - 422 Pydantic 校验失败 → `{"detail": [{type, loc, msg, input}, ...]}`
 *   - 422 业务参数非法     → `{"detail": "<字符串>"}`（app.py 里 raise HTTPException(detail=str)）
 *   - 404 任务/运行不存在   → `{"detail": "<字符串>"}`（如 "未知的 run_id：xxx"）
 *   - 404 任务不存在        → `{"detail": {task_id, completion_status, reason}}`（dict）
 * 三种形态都要能读成一句人话，否则界面上会冒出 [object Object]。
 */

import { getAuthToken, clearAuthStorage } from '../utils/storage'

const BASE = '/api'

/** 登录态失效后跳转前，避免在登录/注册页原地打转。 */
export function redirectToLogin() {
  const { pathname, search } = window.location
  if (pathname === '/login' || pathname === '/register') return
  const redirect = encodeURIComponent(pathname + search)
  window.location.assign(`/login?redirect=${redirect}`)
}

/** 后端连不上（fetch 直接 reject）时用的状态码，区别于后端返回的 4xx/5xx。 */
export const NETWORK_ERROR_STATUS = 0

export class ApiError extends Error {
  constructor(message, { status = NETWORK_ERROR_STATUS, detail = null, cause = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.cause = cause
  }

  /** 后端进程没起来 / 代理转发失败。 */
  get isNetworkError() {
    return this.status === NETWORK_ERROR_STATUS
  }

  get isNotFound() {
    return this.status === 404
  }

  get isValidationError() {
    return this.status === 422
  }
}

/** 把后端的 detail 折成一句可读文本。 */
export function describeDetail(detail) {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail

  // 校验错误数组里是字段名与英文提示，使用者只需要知道"填的内容有问题"。
  if (Array.isArray(detail)) return '填写的内容有问题，请检查后再试。'

  // 业务 dict，例如任务完成的 404：{task_id, completion_status, reason}
  if (typeof detail === 'object') {
    if (detail.reason) return String(detail.reason)
    if (typeof detail.message === 'string') return detail.message
    return '请求没有成功，请稍后再试。'
  }

  return String(detail)
}

async function readBody(response) {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function request(path, { method = 'GET', body, signal, responseType } = {}) {
  const init = { method, headers: {}, signal }
  const wantsBlob = responseType === 'blob'

  const token = getAuthToken()
  if (token) init.headers.Authorization = `Bearer ${token}`
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  let response
  try {
    response = await fetch(`${BASE}${path}`, init)
  } catch (cause) {
    // 被主动取消的请求不要伪装成"后端挂了"
    if (cause && cause.name === 'AbortError') throw cause
    throw new ApiError(
      '连不上服务：可能是服务还没启动完，稍等一会儿再试；一直这样就去「系统状态」页看看。',
      { status: NETWORK_ERROR_STATUS, cause },
    )
  }

  // 二进制接口（Word 下载）成功时不能把正文当 JSON 读掉；失败时仍要读回
  // 后端的 detail，才能给出和普通接口同一套人话错误。
  const payload = (!response.ok || !wantsBlob) ? await readBody(response) : null

  if (!response.ok) {    // 业务接口的 401 = token 缺失/过期/无效：清掉本地登录态并回登录页。
    // /auth/ 自身的 401（比如密码错误）属于正常业务反馈，不能清登录态。
    if (response.status === 401 && !path.startsWith('/auth/')) {
      // 只让**发出这个请求时**的 token 失效登录态。切换账号后旧请求迟到返回
      // 401 时，不能把后来登录的账号一起登出。
      if (getAuthToken() === token) {
        clearAuthStorage()
        redirectToLogin()
      }
    }
    const detail = payload && typeof payload === 'object' ? payload.detail : payload
    const described = describeDetail(detail)
    const fallback = {
      404: '没有找到相关内容，可能已经被删掉了。',
      422: '填写的内容有问题，请检查后再试。',
      500: '服务出了点问题，请稍后再试。',
    }[response.status]
    throw new ApiError(
      described || fallback || '请求没有成功，请稍后再试。',
      { status: response.status, detail },
    )
  }

  return wantsBlob ? response.blob() : payload
}

export const api = {
  get: (path, options) => request(path, { ...options, method: 'GET' }),
  post: (path, body, options) => request(path, { ...options, method: 'POST', body }),
  put: (path, body, options) => request(path, { ...options, method: 'PUT', body }),
  delete: (path, options) => request(path, { ...options, method: 'DELETE' }),
  postBlob: (path, body, options) =>
    request(path, { ...options, method: 'POST', body, responseType: 'blob' }),
}
