import { readStreamText } from './sse'

export interface Config {
  enabled: boolean
  service_mode: 'local' | 'external'
  external_url: string
  management_token: string
  bangumi_api_url: string
  tvdb_api_url: string
  bangumi_use_proxy: boolean
  enable_correction: boolean
  assume_season_by_window: boolean
  production_cycle_gap_days: number
  grace_episodes: number
  source: string | null
  port: number
  history_max_records: number
}
export const defaults: Config = {
  enabled: false,
  service_mode: 'local',
  external_url: 'http://127.0.0.1:8632',
  management_token: '',
  bangumi_api_url: 'https://api.bgm.tv',
  tvdb_api_url: 'https://api4.thetvdb.com/v4',
  bangumi_use_proxy: true,
  enable_correction: true,
  assume_season_by_window: false,
  production_cycle_gap_days: 70,
  grace_episodes: 2,
  source: 'https://raw.githubusercontent.com/wikrin/CureTMDb/main/tv.json',
  port: 8632,
  history_max_records: 10000,
}
export type Result = '' | 'changed' | 'unchanged' | 'skipped' | 'failed'
export const results = [
  { title: '全部结果', value: '' },
  { title: '已修正', value: 'changed' },
  { title: '未改动', value: 'unchanged' },
  { title: '已跳过', value: 'skipped' },
  { title: '失败', value: 'failed' },
]
export const resultLabel = (result: string) => results.find(item => item.value === result)?.title || result
export const resultColor = (result: string) =>
  ({ changed: 'success', unchanged: 'info', skipped: 'warning', failed: 'error' })[result] || 'secondary'
export interface Client {
  defaults?: { baseURL?: string }
  get(path: string, options?: unknown): Promise<any>
  post(path: string, body?: unknown, options?: unknown): Promise<any>
}
export type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'fatal'
export interface LogEntry {
  id: number
  timestamp: string
  level: LogLevel
  message: string
}
export interface LogRetention {
  oldest_id: number
  newest_id: number
  file_count: number
  max_files: number
  max_size_bytes: number
}
export interface LogPage {
  items: LogEntry[]
  next_after: number
  has_more: boolean
  max_limit: number
  retention: LogRetention
  gap: boolean
  next_before?: number
  has_older?: boolean
  reset?: 'history_pruned' | 'cursor_ahead'
}
export interface Health {
  version: string
  status: string
  uptime_seconds?: number
  management_auth_required?: boolean
  capabilities?: { logs?: boolean; upstreams?: boolean; persistent_logs?: boolean; log_stream?: boolean }
  logging?: LogRetention & { available: boolean; stream_count?: number; max_streams?: number }
}
export const logLevels: LogLevel[] = ['debug', 'info', 'warn', 'error', 'fatal']
export const isLogId = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
export function isLogEntry(value: any): value is LogEntry {
  return (
    value &&
    isLogId(value.id) &&
    value.id > 0 &&
    typeof value.timestamp === 'string' &&
    Number.isFinite(Date.parse(value.timestamp)) &&
    logLevels.includes(value.level) &&
    typeof value.message === 'string'
  )
}
export function isLogRetention(value: any): value is LogRetention {
  return (
    value &&
    isLogId(value.oldest_id) &&
    isLogId(value.newest_id) &&
    value.oldest_id <= value.newest_id &&
    isLogId(value.file_count) &&
    isLogId(value.max_files) &&
    value.max_files > 0 &&
    value.file_count <= value.max_files &&
    isLogId(value.max_size_bytes) &&
    value.max_size_bytes > 0
  )
}
export function isLogPage(value: any): value is LogPage {
  if (
    !value ||
    !Array.isArray(value.items) ||
    !value.items.every(isLogEntry) ||
    !isLogRetention(value.retention) ||
    !isLogId(value.next_after) ||
    value.next_after > value.retention.newest_id ||
    typeof value.has_more !== 'boolean' ||
    typeof value.gap !== 'boolean' ||
    !Number.isInteger(value.max_limit) ||
    value.max_limit < 1 ||
    value.max_limit > 2000 ||
    value.items.length > value.max_limit ||
    value.gap !== Boolean(value.reset) ||
    (value.reset !== undefined && !['history_pruned', 'cursor_ahead'].includes(value.reset))
  )
    return false
  if (
    value.items.some(
      (item: LogEntry, i: number) =>
        item.id < value.retention.oldest_id ||
        item.id > value.next_after ||
        (i > 0 && item.id <= value.items[i - 1].id),
    )
  )
    return false
  if ('next_before' in value || 'has_older' in value) {
    if (!isLogId(value.next_before) || typeof value.has_older !== 'boolean' || (!value.items.length && value.has_older))
      return false
  }
  return true
}
export type UpstreamState = 'available' | 'degraded' | 'failed' | 'unknown'
export interface UpstreamResult {
  name: string
  reachable: boolean
  available: boolean
  status_code: number
  latency_ms: number
  message: string
}
export interface UpstreamProbe {
  items: UpstreamResult[]
  checked_at: string
}
export interface UpstreamSample {
  name: string
  state: UpstreamState
  slot_start: number
  slot_end: number
  checked_at: number | null
  reachable: boolean | null
  available: boolean | null
  status_code: number | null
  latency_ms: number | null
  message: string
}
export interface UpstreamStats {
  sample_count: number
  reachable_count: number
  reachability_percent: number | null
  latency_sample_count: number
  avg_latency_ms: number | null
  p90_latency_ms: number | null
}
export interface UpstreamSeries {
  name: string
  latest: UpstreamSample | null
  samples: UpstreamSample[]
  stats: UpstreamStats
}
export interface UpstreamHistory {
  interval_seconds: number
  window_hours: number
  window_start: number
  window_end: number
  generated_at: number
  checked_at: number | null
  monitoring: boolean
  message: string
  items: UpstreamSeries[]
}
export interface ProcessMetrics {
  pid: number
  memory_bytes: number
  cpu_percent: number | null
  network_rx_bytes: number | null
  network_tx_bytes: number | null
  network_rx_rate: number | null
  network_tx_rate: number | null
}
export interface Status {
  log_available?: boolean
  log_stream_available?: boolean
  state: string
  error: string
  mode: 'local' | 'external'
  address: string
  pid: number | null
  process_metrics?: ProcessMetrics | null
  installed_version: string
  required_version: string
  binary_exists: boolean
  health: Health | null
  busy: boolean
  controllable: boolean
  enabled: boolean
  patched: boolean
}
export interface ResultSummary {
  count: number
  latest_at: number
  latest_order: number
  original_range: string
  final_range: string
  reason: string
  strategy: string
  source: string
}
export interface HistoryStatus {
  pending_objects: number
  pending_bytes: number
  scheduler_available: boolean
  last_error: string
  rejected: number
}
export interface RecordItem {
  id: string
  created_at: number
  title: string
  resource_title: string
  resource_subtitle: string | null
  count: number
  selected_count: number
  counts: Record<string, number>
  results: Partial<Record<Exclude<Result, ''>, ResultSummary>>
  result: string
  original_range: string
  final_range: string
  reason: string
  strategy: string
  source: string
}
export interface Group {
  media_key: string
  title: string
  tmdb_id: string
  latest: number
  count: number
  counts: Record<string, number>
  call_count: number
  call_counts: Record<string, number>
  last: Pick<RecordItem, 'result' | 'created_at' | 'original_range' | 'final_range' | 'reason' | 'strategy' | 'source'>
}
export function createApi(client: Client, id: string) {
  const prefix = `plugin/${encodeURIComponent(id || 'CureTMDbAnime')}`
  const unwrap = (value: any) => {
    if (value && typeof value.success === 'boolean' && 'data' in value) {
      if (!value.success) throw new Error(value.message || '请求失败')
      return value.data
    }
    return value
  }
  return {
    get: async (path: string, params = {}) =>
      unwrap(await client.get(`${prefix}/${path}`, { params, feedback: 'silent' })),
    post: async (path: string, body = {}) =>
      unwrap(await client.post(`${prefix}/${path}`, body, { feedback: 'silent' })),
    openLogStream: async (
      params: Record<string, unknown>,
      signal: AbortSignal,
    ): Promise<ReadableStream<Uint8Array>> => {
      try {
        const body = await client.get(`${prefix}/logs/stream`, {
          params,
          signal,
          adapter: 'fetch',
          responseType: 'stream',
          timeout: 0,
          feedback: 'silent',
          skipConnectionTracking: true,
        })
        if (!body || typeof body.getReader !== 'function') throw new Error('未返回可读日志流，请更新主程序后重试')
        return body
      } catch (err: any) {
        const body = err?.response?.data
        if (body && typeof body.getReader === 'function') {
          try {
            err.response.data = JSON.parse(await readStreamText(body))
            err.message = errorMessage(err)
          } catch {
            /* 保留原始 HTTP 状态，不能把错误体当成 SSE */
          }
        }
        throw err
      }
    },
  }
}
export type Api = ReturnType<typeof createApi>
export function errorMessage(error: any): string {
  const payload = error?.response?.data
  const detail = payload?.detail
  if (Array.isArray(detail))
    return detail
      .map(item => item.msg)
      .filter(Boolean)
      .join('；')
  return typeof detail === 'string'
    ? detail
    : typeof payload?.message === 'string'
      ? payload.message
      : error?.message || '请求失败，请重试'
}
export function formatTime(value: number | string) {
  return new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString()
}
const upstreamTimeFormatter = new Intl.DateTimeFormat(undefined, {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})
export function formatUpstreamTime(value: number) {
  const date = new Date(value * 1000)
  return Number.isFinite(date.getTime()) ? upstreamTimeFormatter.format(date) : '—'
}
