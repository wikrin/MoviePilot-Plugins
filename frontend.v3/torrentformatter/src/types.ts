export interface FormatterRules {
  rename_torrent: boolean
  rename_file: boolean
  format_torrent_name: string
  format_save_path: string
  format_movie_path: string
  format_tv_path: string
}

export interface TorrentFile {
  index: number | null
  name: string
  size: number
  priority: number
  selected: boolean
}

export interface TorrentInfo {
  hash: string
  name: string
  save_path: string
  total_size: number
  tags?: string[]
  files: TorrentFile[]
  auto_tmm: boolean
  category: string
  tags: string[]
  state: string
  moving: boolean
  metadata_complete: boolean
}

export interface Capabilities {
  kind: 'qbittorrent' | 'transmission'
  save_path: boolean
  rename_torrent: boolean
  rename_file: boolean
  file_move: boolean
  auto_tmm: boolean
}

export interface MediaBrief {
  media_source: string
  media_id: string
  media_type: 'movie' | 'tv'
  title: string
  year: string
  poster: string
  source?: 'manual' | 'history' | 'assisted' | 'recognized'
  /** 仅用于显示已应用的输入方式，不提交给后端媒体身份模型。 */
  selection_method?: 'search' | 'id'
}

export interface Change {
  id: string
  kind: 'save_path' | 'name' | 'file' | 'auto_tmm'
  before: string
  after: string
  status: 'changed' | 'unchanged' | 'skipped' | 'unsupported' | 'invalid'
  reason: string
  file_id: number | null
}

export interface Preview {
  status: 'ready' | 'blocked' | 'no_changes'
  mode: 'format' | 'recover'
  message: string
  downloader: string
  torrent_hash: string
  preview_token: string | null
  expires_in: number
  before: TorrentInfo
  after: TorrentInfo
  media: MediaBrief | null
  rules: FormatterRules
  capabilities: Capabilities
  changes: Change[]
  counts: {
    total_files: number
    files: number
    save_path: number
    name: number
    auto_tmm: number
    skipped: number
    unsupported: number
    affected_files: number
  }
  warnings: string[]
}

export interface OperationLog {
  change: Change
  status: 'pending' | 'confirmed' | 'failed' | 'unknown'
  message: string
}

export interface ExecutionResult {
  status: 'success' | 'partial' | 'failed' | 'verifying'
  message: string
  operation_id: string
  mode: 'format' | 'recover'
  downloader: string
  torrent_hash: string
  operations: OperationLog[]
  actual: TorrentInfo | null
  can_recover: boolean
}

export interface HistoryPresence {
  state: 'available' | 'transferred' | 'missing' | 'unavailable' | 'conflict' | 'busy' | 'backup_missing'
  message: string
  target_downloader: string
  target_hash: string
  locations: { downloader: string; torrent_hash: string }[]
  can_delete: boolean
  delete_signature: string
  checked_at: string
  cached: boolean
}

export interface HistoryItem {
  id: string
  downloader: string
  torrent_hash: string
  name: string
  save_path: string
  target_save_path: string
  files_count: number
  status: string
  created_at: string
  legacy: boolean
  restorable: boolean
  presence?: HistoryPresence
  /** 前端收到关联失效回执后，在按 ID 实时核验前禁止复用旧签名或恢复入口。 */
  history_stale?: boolean
}

export interface HistoryDeleteItem {
  history_id: string
  delete_signature: string
}

export interface HistoryDeleteBatchRequest {
  items: HistoryDeleteItem[]
  confirmed: true
}

export type HistoryDeleteStatus = 'deleted' | 'not_found' | 'conflict' | 'busy' | 'failed'

export interface HistoryDeleteResult {
  history_id: string
  status: HistoryDeleteStatus
  message: string
  presence?: HistoryPresence
}

export interface HistoryAffectedTarget {
  downloader: string
  torrent_hash: string
}

export interface HistoryDeleteBatchResponse {
  status: string
  message: string
  results: HistoryDeleteResult[]
  removed_history_ids: string[]
  invalidated_history_ids: string[]
  affected_targets: HistoryAffectedTarget[]
}

export interface HistoryLookupRequest {
  history_ids: string[]
}

export interface HistoryLookupResult {
  history_id: string
  status: 'found' | 'not_found'
  message?: string
  item?: HistoryItem
}

export interface HistoryLookupResponse {
  results: HistoryLookupResult[]
}
