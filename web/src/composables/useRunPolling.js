/**
 * 后台运行轮询：`POST` 拿到 run_id 后，反复查 `GET /api/runs/{run_id}` 直到终态。
 *
 * 后端的运行记录只有四态（queued / running / succeeded / failed），
 * **没有进度百分比**，所以这里只负责"查到状态就回调"，不编造任何进度数字。
 *
 * 复用于「岗位分析」（分析链路）与「下一轮成长计划」（成长写入链路）——
 * 两者都是 202 + run_id 的同一套协议。
 */

import { getRun } from '../api/endpoints'
import { ApiError } from '../api/client'

export const RUN_STATUS = {
  QUEUED: 'queued',
  RUNNING: 'running',
  SUCCEEDED: 'succeeded',
  FAILED: 'failed',
}

export const TERMINAL_STATUSES = [RUN_STATUS.SUCCEEDED, RUN_STATUS.FAILED]

export function isTerminal(status) {
  return TERMINAL_STATUSES.includes(status)
}

const DEFAULT_INTERVAL_MS = 2000
// 一次分析要跑七个节点的本地小模型推理，分钟级很正常；给到 20 分钟再判超时。
const DEFAULT_TIMEOUT_MS = 20 * 60 * 1000

function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      cleanup()
      resolve()
    }, ms)
    const onAbort = () => {
      cleanup()
      reject(new DOMException('已取消', 'AbortError'))
    }
    function cleanup() {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * 轮询到终态并返回最后一次的运行记录。
 *
 * @param {string} runId
 * @param {object} [options]
 * @param {number} [options.intervalMs]
 * @param {number} [options.timeoutMs]
 * @param {(record: object) => void} [options.onTick] 每次拿到状态时回调（UI 用来刷新状态）
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<object>} 运行记录（status 为 succeeded 或 failed）
 * @throws {ApiError} 404（运行记录已失效）、网络错误、超时
 */
export async function pollRun(runId, options = {}) {
  const {
    intervalMs = DEFAULT_INTERVAL_MS,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    onTick,
    signal,
    planId = null,
  } = options

  const deadline = Date.now() + timeoutMs

  for (;;) {
    const record = await getRun(runId, { signal }, planId)
    onTick?.(record)

    if (isTerminal(record.status)) return record

    if (Date.now() >= deadline) {
      throw new ApiError(
        `等待超过 ${Math.round(timeoutMs / 60000)} 分钟仍未结束。` +
          '任务可能仍在后台执行，稍后可再次查询。',
        { status: 408 },
      )
    }

    await delay(intervalMs, signal)
  }
}
