// 共享契约与开发预览默认值；正式工作台以 /rules 返回的有效配置为准。
export const DEFAULT_RULES = Object.freeze({
  rename_torrent: false,
  rename_file: false,
  format_torrent_name:
    '{{ title }}{% if year %} ({{ year }}){% endif %}{% if season_episode %} - {{season_episode}}{% endif %}',
  format_save_path: '{{title}}{% if year %} ({{year}}){% endif %}',
  format_movie_path:
    '{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}',
  format_tv_path:
    'Season {{season}}/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}',
})

/** 宿主系统 API 使用 envelope，插件响应保持自行声明的 payload。 */
export function unwrapPayload(response) {
  if (response && typeof response === 'object' && typeof response.success === 'boolean' && 'data' in response) {
    if (!response.success) throw new Error(response.message || '请求失败')
    return response.data
  }
  return response
}

/** 不把技术对象 stringify 到界面，优先显示服务端明确原因。 */
export function errorMessage(error, fallback = '请求失败，请稍后重试') {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail))
    return (
      detail
        .map(item => item.msg)
        .filter(Boolean)
        .join('；') || fallback
    )
  return error?.response?.data?.message || error?.message || fallback
}

export function formatSize(bytes = 0) {
  if (!Number.isFinite(Number(bytes)) || Number(bytes) < 0) return '—'
  if (!Number(bytes)) return '0 B'
  const unit = Math.min(Math.floor(Math.log(Number(bytes)) / Math.log(1024)), 4)
  return `${(Number(bytes) / 1024 ** unit).toFixed(unit ? 1 : 0)} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][unit]}`
}

export function shortHash(hash = '') {
  return hash.length > 20 ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : hash
}

/** 前端选择媒体只提交可信身份，标题/海报不进入后端命名请求。 */
export function mediaIdentity(media) {
  if (!media) return null
  return { media_source: 'themoviedb', media_id: String(media.media_id), media_type: media.media_type }
}

/** 仅差量覆盖，保留 false、空字符串及用户有意清空的模板。 */
export function ruleOverrides(draft, base) {
  return Object.fromEntries(
    Object.keys(DEFAULT_RULES)
      .filter(key => draft[key] !== base[key])
      .map(key => [key, draft[key] ?? (key.startsWith('format_') ? '' : false)]),
  )
}

/** 同时适配当前 canonical 身份与旧宿主 TMDB 字段。 */
export function normalizeMedia(item) {
  const id = String(item.media_id || item.tmdb_id || item.tmdbid || '')
  const type = { 电影: 'movie', 电视剧: 'tv', movie: 'movie', tv: 'tv' }[item.type || item.media_type]
  if (!/^[1-9]\d{0,11}$/.test(id) || !type) return null
  if (item.media_source && !['themoviedb', 'tmdb'].includes(item.media_source)) return null
  let poster = item.poster_path || item.poster || ''
  if (poster.startsWith('/')) poster = `https://image.tmdb.org/t/p/w185${poster}`
  return {
    media_id: id,
    media_type: type,
    media_source: 'themoviedb',
    title: item.title || item.name || `TMDB ${id}`,
    year: String(item.year || ''),
    poster,
    overview: item.overview || '',
  }
}

/** 使用宿主注入的实例 API，常量源插件路径可由宿主自动映射到分身。 */
export function createTorrentFormatterApi(client) {
  const prefix = 'plugin/TorrentFormatter'
  const get = async (path, params = {}, signal) =>
    unwrapPayload(await client.get(path, { params, signal, feedback: 'silent' }))
  const post = async (path, payload) =>
    unwrapPayload(await client.post(`${prefix}/${path}`, payload, { feedback: 'silent' }))
  // 新历史接口本身就是响应契约，不接受宿主 success/data envelope。
  const historyPost = (path, payload) => client.post(`${prefix}/${path}`, payload, { feedback: 'silent' })
  const deleteHistoryBatch = items =>
    historyPost('delete_history_batch', {
      items: items.map(item => ({
        history_id: item.history_id || item.id,
        delete_signature: item.presence?.delete_signature || item.delete_signature,
      })),
      confirmed: true,
    })
  return {
    async loadConfig(pluginId, signal) {
      const value = await get(pluginId ? `plugin/${encodeURIComponent(pluginId)}` : prefix, {}, signal)
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('全局配置响应无效')
      return value
    },
    async saveConfig(pluginId, value) {
      const path = pluginId ? `plugin/${encodeURIComponent(pluginId)}` : prefix
      const response = await client.put(path, value, { feedback: 'silent' })
      if (response?.success !== true) throw new Error(response?.message || '全局配置保存失败')
    },
    async downloaders() {
      const items = await get('download/clients')
      if (!Array.isArray(items)) throw new Error('下载器列表响应无效')
      return items.filter(item => !item.type || ['qbittorrent', 'transmission'].includes(item.type))
    },
    rules: () => get(`${prefix}/rules`),
    torrents: (params, signal) => get(`${prefix}/torrents`, params, signal),
    detail: (target, signal) => get(`${prefix}/torrent_detail`, target, signal),
    preview: payload => post('preview', payload),
    process: token => post('process', { preview_token: token }),
    recover: (target, token) =>
      post('recover_from_history', {
        downloader: target.downloader,
        torrent_hash: target.torrent_hash,
        preview_token: token,
      }),
    status: operation_id => get(`${prefix}/operation_status`, { operation_id }),
    history: (page = 1, page_size = 20, filters = {}, options = {}) =>
      get(
        `${prefix}/history`,
        { ...filters, page, page_size, fresh: Boolean(options.fresh) },
        options.signal,
      ),
    deleteHistoryBatch,
    deleteHistory: item => deleteHistoryBatch([item]),
    historyLookup: history_ids => historyPost('history_lookup', { history_ids }),
    async searchMedia(title, signal) {
      const items = await get(
        'media/search',
        { title, type: 'media', page: 1, count: 20, media_source: 'themoviedb' },
        signal,
      )
      if (!Array.isArray(items)) throw new Error('媒体搜索响应无效')
      return items.map(normalizeMedia).filter(Boolean)
    },
  }
}
