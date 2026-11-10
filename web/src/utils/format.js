/**
 * 展示格式化。
 */

/** 列表 → 顿号分隔文本；空列表给一个明确的占位，不显示空串。 */
export function joinList(list, placeholder = '—') {
  if (!Array.isArray(list) || list.length === 0) return placeholder
  return list.map((item) => String(item ?? '').trim()).filter(Boolean).join('、') || placeholder
}

/**
 * 满足率。
 *
 * ⚠️ 后端在"没有可判定要求"时返回 `match_rate = null`（不是 0），
 * 这时候必须显示"无可判定要求"，**绝不能显示成 0%** —— 那会把
 * "判不了"说成"一个都不满足"。
 */
export function formatMatchRate(rate) {
  if (rate === null || rate === undefined || rate === '') return '无可判定要求'
  const num = Number(rate)
  if (Number.isNaN(num)) return '无可判定要求'
  return `${Math.round(num * 100)}%`
}

export function hasMatchRate(rate) {
  return rate !== null && rate !== undefined && rate !== '' && !Number.isNaN(Number(rate))
}

/**
 * 清单里的证据有时存成一段结构（kind + summary），不能直接 {{ }}，
 * 否则界面会原样打印 JSON；这里只取给人看的那一句。
 */
export function formatEvidence(value) {
  if (value == null) return ''
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'object') {
    return String(value.summary || value.text || value.note || '').trim()
  }
  return String(value).trim()
}

/** 秒 → "3 分 12 秒" / "45 秒" */
export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  if (total < 60) return `${total} 秒`
  const m = Math.floor(total / 60)
  const s = total % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分`
}

/** 后端时间戳是 ISO（秒级精度），失败就原样显示，不假装解析成功。 */
export function formatDateTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/** 重要程度 1~5 → "4/5"；为 null 时明确写"未知"，不默认成 0。 */
export function formatImportance(value) {
  if (value === null || value === undefined || value === '') return '未知'
  return `${value}/5`
}

/** 文件字节数（系统状态页展示索引大小用）。 */
export function formatBytes(bytes) {
  const num = Number(bytes)
  if (!Number.isFinite(num) || num < 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = num
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`
}

// 后端把失败原因存成 "ValueError: 具体原因"（backend/jobs.py 的 TaskRecord.error）
const ERROR_CLASS_PREFIX = /^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Timeout):\s*/

/**
 * 后台任务的失败原因 → 给人看的一句话。
 *
 * 异常类名（`ValueError`、`RuntimeError`）对使用者毫无意义，只会让人以为看到了
 * 别人的调试输出，所以削掉前缀只留后面那句中文原因；整条为空才用兜底文案。
 */
export function formatRunError(raw, fallback = '任务没有完成，这次没拿到具体原因。') {
  const text = String(raw ?? '').trim().replace(ERROR_CLASS_PREFIX, '')
  return text || fallback
}
