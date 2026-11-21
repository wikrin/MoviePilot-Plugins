// 独立开发演示：所有 GET/POST 均只读写内存，不调用 fetch 或任何下载器。
import { DEFAULT_RULES } from '../api/torrentformatter.js'

const clone = value => JSON.parse(JSON.stringify(value))
const hash = value => value.repeat(40)
const identity = (name, id) => `${encodeURIComponent(name)}:${id}`
const caps = {
  qBittorrent: {
    kind: 'qbittorrent',
    save_path: true,
    rename_torrent: true,
    rename_file: true,
    file_move: true,
    auto_tmm: true,
  },
  Transmission: {
    kind: 'transmission',
    save_path: true,
    rename_torrent: false,
    rename_file: true,
    file_move: false,
    auto_tmm: false,
  },
}
const medias = [
  {
    media_source: 'themoviedb',
    media_id: '157336',
    media_type: 'movie',
    type: '电影',
    title: '星际穿越',
    year: '2014',
    poster: 'https://image.tmdb.org/t/p/w185/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg',
    overview: '一次穿越星际与时间的旅程。此处为演示媒体信息。',
  },
  {
    media_source: 'themoviedb',
    media_id: '204541',
    media_type: 'tv',
    type: '电视剧',
    title: '三体',
    year: '2023',
    poster: '',
    overview: '逐文件季集识别、目录布局与屏蔽词跳过示例。',
  },
  {
    media_source: 'themoviedb',
    media_id: '438631',
    media_type: 'movie',
    type: '电影',
    title: '沙丘',
    year: '2021',
    poster: '',
    overview: '已有处理记录与恢复的演示示例。',
  },
]
function torrent(id, name, save_path, names, extra = {}) {
  const files = names.map((name, index) => ({
    index,
    name,
    size: name.endsWith('.mkv') ? 3 * 1024 ** 3 : 1024 ** 2,
    priority: 1,
    selected: !name.startsWith('Screens/'),
  }))
  return {
    hash: hash(id),
    name,
    save_path,
    total_size: files.reduce((sum, file) => sum + file.size, 0),
    files,
    auto_tmm: false,
    category: '影视',
    tags: ['演示'],
    state: 'seeding',
    moving: false,
    metadata_complete: true,
    ...extra,
  }
}
function apiError(message, status = 409) {
  return Object.assign(new Error(message), { response: { status, data: { detail: message } } })
}
function appendPath(root, relative) {
  const base = root.replaceAll('\\', '/').split('/')
  const parts = relative.split('/').filter(Boolean)
  let overlap = 0
  for (let count = Math.min(base.length, parts.length); count > 0; count--) {
    if (base.slice(-count).join('/') === parts.slice(0, count).join('/')) {
      overlap = count
      break
    }
  }
  return [...base, ...parts.slice(overlap)].join('/')
}
function render(template, context) {
  // 仅模拟默认模板的常见 if/变量；不替代后端 Jinja2 验证。
  let value = template
  for (let iteration = 0; iteration < 8; iteration++)
    value = value.replace(/{%\s*if\s+(\w+)\s*%}([\s\S]*?){%\s*endif\s*%}/g, (_, key, body) =>
      context[key] ? body : '',
    )
  value = value
    .replace(/{{\s*(\w+)\s*}}/g, (_, key) => String(context[key] ?? ''))
    .trim()
    .replaceAll('\\', '/')
  if (/{[{%]/.test(value)) throw new Error('演示渲染器不支持此模板语法，请使用后端环境验证完整 Jinja2')
  if (!value || value === '.' || value.startsWith('/') || /^[A-Za-z]:/.test(value) || value.split('/').includes('..'))
    throw new Error('目标必须是非空相对路径，不能包含 .. 或绝对目录')
  return value
}
function countChanges(changes, total) {
  const counts = {
    total_files: total,
    files: 0,
    save_path: 0,
    name: 0,
    auto_tmm: 0,
    skipped: 0,
    unsupported: 0,
    affected_files: 0,
  }
  for (const item of changes) {
    if (item.status === 'changed') counts[item.kind === 'file' ? 'files' : item.kind]++
    if (item.status === 'skipped') counts.skipped++
    if (item.status === 'unsupported') counts.unsupported++
  }
  counts.affected_files = counts.save_path ? total : counts.files
  return counts
}

export function createMockApi() {
  let settings = {
    ...DEFAULT_RULES,
    rename_torrent: true,
    rename_file: true,
    cron_enabled: false,
    event_enabled: false,
    cron: '',
    downloader: [],
    exclude_dirs: '',
    exclude_tags: '',
  }
  let scenario = 'normal'
  const movie = torrent(
    '1',
    'Interstellar.2014.2160p.UHD.BluRay.x265-GROUP',
    '/downloads/Movies',
    ['Interstellar.2014/Interstellar.2014.2160p.mkv', 'Screens/preview.jpg'],
    { auto_tmm: true },
  )
  const tv = torrent('2', 'Three.Body.2023.S01.1080p.WEB-DL', '/downloads/TV', [
    'Three.Body.S01/Three.Body.S01E01.mkv',
    'Three.Body.S01/Three.Body.S01E02.mkv',
    'Three.Body.S01/Three.Body.S01E03.mkv',
    'Three.Body.S01/Sample.S01E01.mkv',
  ])
  const dune = torrent('3', '沙丘 (2021)', '/downloads/沙丘 (2021)', ['沙丘 (2021).mkv'])
  const tr = torrent('5', 'Interstellar.2014.Transmission.Example', 'D:/downloads', ['Interstellar.2014/movie.mkv'])
  const sharedQb = torrent('6', 'Shared.Hash.qBittorrent', '/downloads/shared-qb', ['shared/movie.mkv'])
  const sharedTr = { ...clone(sharedQb), name: 'Shared.Hash.Transmission', save_path: 'D:/downloads/shared-tr' }
  const clients = { qBittorrent: [movie, tv, dune, sharedQb], Transmission: [tr, sharedTr] }
  const records = new Map([
    [
      identity('qBittorrent', dune.hash),
      {
        downloader: 'qBittorrent',
        torrent_hash: dune.hash,
        before: {
          ...clone(dune),
          name: 'Dune.2021.2160p',
          save_path: '/downloads',
          files: [{ ...clone(dune.files[0]), name: 'Dune.2021/Dune.2021.2160p.mkv' }],
        },
        after: clone(dune),
        status: 'success',
        created_at: new Date(Date.now() - 3600000).toISOString(),
        legacy: true,
      },
    ],
    [
      identity('qBittorrent', sharedQb.hash),
      {
        downloader: 'qBittorrent',
        torrent_hash: sharedQb.hash,
        before: { ...clone(sharedQb), name: 'Shared.Before.qBittorrent' },
        after: clone(sharedQb),
        status: 'success',
        created_at: new Date(Date.now() - 2 * 3600000).toISOString(),
      },
    ],
    [
      identity('Transmission', sharedTr.hash),
      {
        downloader: 'Transmission',
        torrent_hash: sharedTr.hash,
        before: { ...clone(sharedTr), name: 'Shared.Before.Transmission' },
        after: clone(sharedTr),
        status: 'success',
        created_at: new Date(Date.now() - 3 * 3600000).toISOString(),
      },
    ],
  ])
  for (let index = 0; index < 36; index++) {
    const torrentHash = (index + 16).toString(16).padStart(40, '0')
    const batchStatus = index < 4 ? ['deleted', 'conflict', 'busy', 'failed'][index] : ''
    const name = batchStatus ? `Batch fixture ${index + 1}` : `Paged history ${String(index + 1).padStart(2, '0')}`
    const before = torrent(
      String.fromCharCode(97 + (index % 6)),
      name,
      `/archive/${index + 1}`,
      [`${name}/movie.mkv`],
    )
    before.hash = torrentHash
    records.set(`history-${String(index + 1).padStart(3, '0')}`, {
      downloader: index % 2 ? 'Transmission' : 'qBittorrent',
      torrent_hash: torrentHash,
      before,
      after: { ...clone(before), save_path: `/formatted/${index + 1}` },
      status: index % 7 === 0 ? 'partial' : 'success',
      created_at: new Date(Date.now() - (index + 1) * 86400000).toISOString(),
      mock_batch_status: batchStatus,
    })
  }
  const transfers = new Map()
  const tickets = new Map()
  const operations = new Map()
  const presenceCache = new Map()
  const deletedRecords = new Map()

  function recordFor(name, id) {
    const direct = identity(name, id)
    if (records.has(direct)) return [direct, records.get(direct)]
    return (
      [...records.entries()].find(
        ([key, row]) => row.torrent_hash === id && transfers.get(key)?.to_download === name,
      ) || [direct, null]
    )
  }
  async function historyItem(id, row, { fresh = false } = {}) {
    const transfer = transfers.get(id)
    const targetName = transfer?.to_download || row.downloader
    const current = clients[targetName]?.find(item => item.hash === row.torrent_hash)
    let state =
      scenario === 'offline'
        ? 'unavailable'
        : !row.before
          ? 'backup_missing'
          : !current
            ? 'missing'
            : current.moving
              ? 'busy'
              : transfer
                ? 'transferred'
                : 'available'
    let checkedAt = new Date().toISOString()
    let cached = false
    const cachedPresence = presenceCache.get(id)
    if (!fresh && cachedPresence && Date.now() - cachedPresence.time < 5000) {
      state = cachedPresence.state
      checkedAt = cachedPresence.checked_at
      cached = true
    } else {
      presenceCache.set(id, { state, checked_at: checkedAt, time: Date.now() })
    }
    const messages = {
      available: `种子仍存在于下载器 ${targetName}`,
      transferred: `已转种，种子当前位于下载器 ${targetName}`,
      missing: '原下载器及已知转种目标中已不存在此种子；可删除插件记录',
      unavailable: '下载器不可用，无法确认种子是否已删除',
      busy: '种子正在移动/校验，暂不允许删除备份',
      backup_missing: '原始备份缺失，不能恢复',
    }
    const value = JSON.stringify({ id, before: row.before, transfer, state, status: row.status })
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))
    const signature = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('')
    return {
      id,
      downloader: row.downloader,
      torrent_hash: row.torrent_hash,
      name: row.before?.name || row.torrent_hash,
      save_path: row.before?.save_path || '',
      target_save_path: row.after.save_path,
      files_count: row.before?.files.length || 0,
      status: row.legacy ? 'legacy' : row.status,
      created_at: row.created_at,
      legacy: Boolean(row.legacy),
      restorable: ['available', 'transferred'].includes(state),
      presence: {
        state,
        message: messages[state],
        target_downloader: targetName,
        target_hash: row.torrent_hash,
        can_delete: state !== 'busy',
        delete_signature: signature,
        checked_at: checkedAt,
        cached,
        locations: [
          { downloader: row.downloader, torrent_hash: row.torrent_hash },
          ...(transfer ? [{ downloader: targetName, torrent_hash: row.torrent_hash }] : []),
        ],
      },
    }
  }

  function find(name, id) {
    const item = clients[name]?.find(item => item.hash === id)
    if (!item) throw apiError('演示下载器中不存在此种子', 404)
    return item
  }
  function mediaFor(item, selection) {
    const known = selection
      ? medias.find(media => media.media_id === selection.media_id && media.media_type === selection.media_type)
      : medias[item.hash === hash('2') || item.hash === hash('4') ? 1 : item.hash === hash('3') ? 2 : 0]
    return {
      ...(known || {
        media_id: selection.media_id,
        media_type: selection.media_type,
        title: `指定媒体 ${selection.media_id}`,
        year: '2024',
        poster: '',
      }),
      media_source: 'themoviedb',
      source: selection ? 'manual' : item.hash === hash('2') ? 'recognized' : 'history',
    }
  }
  function makePreview(payload) {
    if (scenario === 'offline') throw apiError('下载器离线（演示），不会修改任何数据', 503)
    const mode = payload.mode || 'format'
    const [historyKey, record] = recordFor(payload.downloader, payload.torrent_hash)
    const targetName =
      mode === 'recover' ? transfers.get(historyKey)?.to_download || payload.downloader : payload.downloader
    const original = clone(find(targetName, payload.torrent_hash))
    if (mode === 'recover' && !record) throw apiError('没有可恢复备份', 404)
    const rules = Object.fromEntries(Object.keys(DEFAULT_RULES).map(key => [key, settings[key]]))
    Object.assign(rules, payload.overrides || {})
    const media = mediaFor(original, payload.media)
    const target = clone(original)
    const capability = caps[targetName]
    const changes = []
    const warnings = []
    const change = (kind, before, after, supported = true, file_id = null, reason = '') => {
      const item = {
        id: kind === 'file' ? `file:${file_id}` : kind,
        kind,
        before: String(before),
        after: String(after),
        file_id,
        status: before === after ? 'unchanged' : supported ? 'changed' : 'unsupported',
        reason: supported ? reason : reason || '此下载器不支持本项能力',
      }
      changes.push(item)
      return item
    }
    let blocked = original.moving
      ? '种子正在移动或校验，请等待后重新预览'
      : mode === 'format' && record
        ? '种子已有处理记录，请先恢复再应用新规则'
        : ''
    if (
      mode === 'format' &&
      settings.exclude_dirs &&
      settings.exclude_dirs
        .split('\n')
        .some(dir => dir && (original.save_path === dir || original.save_path.startsWith(dir + '/')))
    )
      blocked = '保存路径命中排除目录（演示）'
    if (mode === 'recover') {
      const backup = record.before
      change('save_path', original.save_path, backup.save_path)
      target.save_path = backup.save_path
      change('name', original.name, backup.name, capability.rename_torrent)
      target.name = capability.rename_torrent ? backup.name : original.name
      for (const file of original.files) {
        const old = backup.files.find(item => item.index === file.index)
        if (!old || old.size !== file.size) {
          blocked = '文件身份与备份不一致，拒绝恢复'
          continue
        }
        const supported =
          capability.file_move ||
          file.name.split('/').slice(0, -1).join('/') === old.name.split('/').slice(0, -1).join('/')
        if (change('file', file.name, old.name, supported, file.index).status === 'changed')
          target.files.find(item => item.index === file.index).name = old.name
      }
      if (
        change('auto_tmm', String(original.auto_tmm), String(backup.auto_tmm), capability.auto_tmm).status === 'changed'
      )
        target.auto_tmm = backup.auto_tmm
      if (changes.some(item => item.status === 'unsupported')) blocked = '当前下载器无法完整恢复此备份'
      warnings.push('全部布局及记录确认恢复后才清除备份。')
    } else if (!blocked) {
      const context = {
        title: media.title,
        year: media.year,
        season: '01',
        season_episode: media.media_type === 'tv' ? 'S01' : '',
        part: '',
        videoFormat: '2160p',
        fileExt: '',
      }
      try {
        if (rules.format_save_path)
          target.save_path = appendPath(original.save_path, render(rules.format_save_path, context))
        change('save_path', original.save_path, target.save_path)
        const title =
          rules.rename_torrent && rules.format_torrent_name ? render(rules.format_torrent_name, context) : original.name
        if (change('name', original.name, title, capability.rename_torrent).status === 'changed') target.name = title
        for (const file of original.files) {
          let name = file.name,
            reason = '',
            status = 'unchanged'
          const template = media.media_type === 'tv' ? rules.format_tv_path : rules.format_movie_path
          if (rules.rename_file && template) {
            if (!file.selected || /sample/i.test(file.name)) {
              status = 'skipped'
              reason = !file.selected ? '未选中下载，保留原路径' : '命中系统屏蔽词 sample'
            } else {
              const episode = file.name.match(/S(\d{1,2})E(\d{1,4})/i)
              if (media.media_type === 'tv' && !episode) {
                status = 'skipped'
                reason = '未识别集数，不按文件顺序猜测'
              } else {
                name = render(template, {
                  ...context,
                  season: episode?.[1] || String(payload.season ?? 1).padStart(2, '0'),
                  season_episode: episode ? `S${episode[1]}E${episode[2]}` : '',
                  episode: episode?.[2] || '',
                  videoFormat: media.media_type === 'tv' ? '1080p' : '2160p',
                  fileExt: '.' + file.name.split('.').at(-1),
                })
                status =
                  name === file.name
                    ? 'unchanged'
                    : capability.file_move ||
                        name.split('/').slice(0, -1).join('/') === file.name.split('/').slice(0, -1).join('/')
                      ? 'changed'
                      : 'unsupported'
                if (status === 'unsupported') reason = 'Transmission 不支持文件跨目录重排'
              }
            }
          }
          changes.push({
            id: `file:${file.index}`,
            kind: 'file',
            before: file.name,
            after: name,
            status,
            file_id: file.index,
            reason,
          })
          if (status === 'changed') target.files.find(item => item.index === file.index).name = name
        }
        if (new Set(target.files.map(file => file.name)).size !== target.files.length)
          throw new Error('文件目标重名，拒绝覆盖')
        if (target.save_path !== original.save_path && original.auto_tmm && capability.auto_tmm) {
          change('auto_tmm', 'true', 'false', true, null, '移动前关闭自动管理，恢复时还原')
          target.auto_tmm = false
        }
      } catch (error) {
        blocked = error.message
      }
      if (rules.rename_file) warnings.push('实验性文件布局修改可能影响辅种，请逐项核对。')
      if (changes.some(item => item.status === 'unsupported')) warnings.push('不支持项保留原状，不会冒充执行成功。')
    }
    const status = blocked
      ? 'blocked'
      : mode === 'recover' || changes.some(item => item.status === 'changed')
        ? 'ready'
        : 'no_changes'
    const plan = {
      status,
      mode,
      message: blocked || (status === 'ready' ? '只读演示预览已就绪' : '没有可执行变更'),
      downloader: targetName,
      torrent_hash: original.hash,
      before: original,
      after: target,
      media,
      rules,
      capabilities: capability,
      changes,
      counts: countChanges(changes, original.files.length),
      warnings,
      preview_token: null,
      expires_in: 0,
    }
    if (status === 'ready') {
      plan.preview_token = crypto.randomUUID().replaceAll('-', '')
      plan.expires_in = 600
      tickets.set(plan.preview_token, {
        plan: clone(plan),
        expires: Date.now() + 600000,
        config: JSON.stringify(settings),
        historyKey,
      })
    }
    return plan
  }
  function execute(token, mode) {
    const ticket = tickets.get(token)
    if (!ticket || ticket.expires < Date.now() || ticket.plan.mode !== mode)
      throw apiError('预览过期或类型不匹配，请重新生成')
    if (ticket.result) return clone(ticket.result)
    if (ticket.config !== JSON.stringify(settings)) throw apiError('已保存配置改变，请重新预览')
    const plan = ticket.plan
    const item = find(plan.downloader, plan.torrent_hash)
    if (JSON.stringify(item) !== JSON.stringify(plan.before)) throw apiError('种子状态已改变，请重新预览')
    const key = mode === 'recover' ? ticket.historyKey : identity(plan.downloader, plan.torrent_hash)
    if (mode === 'format' && records.has(key)) throw apiError('请先恢复')
    if (mode === 'format')
      records.set(key, {
        downloader: plan.downloader,
        torrent_hash: plan.torrent_hash,
        before: clone(plan.before),
        after: clone(plan.after),
        status: 'verifying',
        created_at: new Date().toISOString(),
      })
    const order = { file: 1, name: 2, save_path: 3 }
    const logs = plan.changes
      .filter(change => change.status === 'changed')
      .sort(
        (a, b) =>
          (a.kind === 'auto_tmm' ? (a.after === 'false' ? 0 : 4) : order[a.kind]) -
          (b.kind === 'auto_tmm' ? (b.after === 'false' ? 0 : 4) : order[b.kind]),
      )
      .map(change => ({ change: clone(change), status: 'pending', message: '' }))
    for (const [index, log] of logs.entries()) {
      if (scenario === 'partial' && mode === 'format' && index >= 1) {
        log.status = 'failed'
        log.message = '模拟下载器调用失败，停止后续操作'
        break
      }
      const change = log.change
      if (change.kind === 'file') item.files.find(file => file.index === change.file_id).name = change.after
      else if (change.kind === 'auto_tmm') item.auto_tmm = change.after === 'true'
      else item[change.kind] = change.after
      log.status = 'confirmed'
      log.message = '演示回读确认'
      if (scenario === 'moving' && mode === 'format' && change.kind === 'save_path') {
        item.moving = true
        item.state = 'moving'
        log.status = 'unknown'
        log.message = '正在移动（模拟）'
      }
    }
    let status = logs.some(item => item.status === 'failed') ? 'partial' : item.moving ? 'verifying' : 'success'
    if (mode === 'recover' && status === 'success') records.delete(key)
    else if (records.has(key)) records.get(key).status = status
    const result = {
      status,
      mode,
      operation_id: token,
      downloader: plan.downloader,
      torrent_hash: plan.torrent_hash,
      actual: clone(item),
      operations: logs,
      message:
        status === 'success'
          ? mode === 'recover'
            ? '恢复完成（内存演示），备份已清理'
            : '处理完成（内存演示），其他种子未改变'
          : status === 'verifying'
            ? '移动已提交，正在等待实际状态确认（模拟）'
            : '部分修改已生效，请先恢复后再处理（模拟）',
      can_recover: mode === 'format' || status !== 'success',
    }
    ticket.result = result
    operations.set(token, { result, checks: 0 })
    return clone(result)
  }

  return {
    getConfig: () => clone(settings),
    setConfig(value) {
      settings = clone({ ...settings, ...value })
    },
    setScenario(value) {
      scenario = value
      presenceCache.clear()
      if (value === 'history-missing') {
        for (const name of Object.keys(clients)) clients[name] = clients[name].filter(item => item.hash !== dune.hash)
        transfers.delete(identity('qBittorrent', dune.hash))
      }
      if (value === 'history-transferred') {
        const source = records.get(identity('qBittorrent', dune.hash))
        if (source) {
          clients.qBittorrent = clients.qBittorrent.filter(item => item.hash !== dune.hash)
          clients.Transmission = [...clients.Transmission.filter(item => item.hash !== dune.hash), clone(source.after)]
          transfers.set(identity('qBittorrent', dune.hash), { to_download: 'Transmission', to_download_id: dune.hash })
        }
      }
      if (value === 'large' && !clients.qBittorrent.some(item => item.hash === hash('4')))
        clients.qBittorrent.unshift(
          torrent(
            '4',
            'Large.Collection.S01 · 1000 个文件',
            '/downloads/Large',
            Array.from(
              { length: 1000 },
              (_, i) => `Collection/Season 01/Episode.S01E${String(i + 1).padStart(3, '0')}.mkv`,
            ),
          ),
        )
    },
    rebuildHistory(id) {
      const deleted = deletedRecords.get(id)
      if (!deleted) throw apiError('没有可重建的演示历史', 404)
      const rebuilt = clone(deleted)
      rebuilt.before.name = `${rebuilt.before.name} · 已重建`
      rebuilt.created_at = new Date().toISOString()
      records.set(id, rebuilt)
      presenceCache.delete(id)
    },
    async get(path, options = {}) {
      await new Promise(resolve => setTimeout(resolve, 100))
      const url = new URL(path, 'http://demo.invalid/')
      const params = { ...Object.fromEntries(url.searchParams), ...(options.params || {}) }
      if (options.signal?.aborted) throw apiError('请求已取消', 499)
      if (url.pathname.endsWith('download/clients'))
        return { success: true, data: Object.keys(clients).map(name => ({ name, type: caps[name].kind })) }
      if (url.pathname.endsWith('media/search'))
        return {
          success: true,
          data: medias
            .filter(item => !params.title || `${item.title} ${item.media_id} ${item.overview}`.includes(params.title))
            .map(item => ({ ...item, poster_path: item.poster })),
        }
      if (url.pathname === '/plugin/TorrentFormatter') return clone(settings)
      const endpoint = url.pathname.split('/').at(-1)
      if (endpoint === 'rules')
        return {
          defaults: clone(DEFAULT_RULES),
          effective: Object.fromEntries(Object.keys(DEFAULT_RULES).map(key => [key, settings[key]])),
          exclude_dirs: settings.exclude_dirs,
          exclude_tags: settings.exclude_tags,
          exclude_words: ['sample'],
        }
      if (endpoint === 'torrents') {
        if (scenario === 'offline') throw apiError('下载器离线（模拟），请切回正常场景后重试', 503)
        const items = (clients[params.downloader] || [])
          .filter(
            item =>
              !params.query ||
              `${item.name} ${item.hash}`.toLocaleLowerCase().includes(params.query.toLocaleLowerCase()),
          )
          .map(item => ({
            hash: item.hash,
            name: item.name,
            save_path: item.save_path,
            total_size: item.total_size,
            state: item.state,
            processed: Boolean(recordFor(params.downloader, item.hash)[1]),
          }))
        const page = Number(params.page) || 1,
          size = Number(params.page_size) || 25
        return clone({
          items: items.slice((page - 1) * size, page * size),
          total: items.length,
          page,
          page_size: size,
          capabilities: caps[params.downloader],
        })
      }
      if (endpoint === 'torrent_detail') {
        if (scenario === 'offline') throw apiError('下载器离线（模拟）', 503)
        const item = find(params.downloader, params.torrent_hash)
        const [key, linked] = recordFor(params.downloader, params.torrent_hash)
        return clone({
          torrent: item,
          capabilities: caps[params.downloader],
          processed: Boolean(linked),
          history_id: linked ? key : null,
          media: item.hash === hash('2') || item.hash === hash('4') ? null : mediaFor(item),
          warnings: [],
        })
      }
      if (endpoint === 'history') {
        const fresh = params.fresh === true || params.fresh === 'true'
        const source = ['history-missing', 'history-transferred'].includes(scenario)
          ? [...records.entries()].filter(([id]) => id === identity('qBittorrent', dune.hash))
          : [...records.entries()]
        let items = await Promise.all(source.map(([id, item]) => historyItem(id, item, { fresh })))
        items.sort((left, right) => Date.parse(right.created_at || 0) - Date.parse(left.created_at || 0))
        const term = (params.query || '').trim().toLocaleLowerCase()
        items = items.filter(
          item =>
            !term ||
            `${item.name} ${item.torrent_hash} ${item.downloader} ${item.presence.target_downloader} ${item.save_path} ${item.target_save_path}`
              .toLocaleLowerCase()
              .includes(term),
        )
        if (params.result_filter && params.result_filter !== 'all')
          items = items.filter(item =>
            params.result_filter === 'legacy' ? item.legacy : item.status === params.result_filter,
          )
        if (params.presence_filter && params.presence_filter !== 'all')
          items = items.filter(item =>
            params.presence_filter === 'restorable' ? item.restorable : item.presence.state === params.presence_filter,
          )
        const page = Number(params.page) || 1,
          size = Number(params.page_size) || 20
        return clone({ items: items.slice((page - 1) * size, page * size), total: items.length, page, page_size: size })
      }
      if (endpoint === 'operation_status') {
        const operation = operations.get(params.operation_id)
        if (!operation) throw apiError('操作记录不存在', 404)
        if (operation.result.status === 'verifying' && ++operation.checks >= 3) {
          const item = find(operation.result.downloader, operation.result.torrent_hash)
          item.moving = false
          item.state = 'seeding'
          operation.result.status = 'success'
          operation.result.message = '移动回读已确认（内存演示）'
          operation.result.actual = clone(item)
          operation.result.operations.forEach(log => {
            if (log.status === 'unknown') log.status = 'confirmed'
          })
          records.get(identity(operation.result.downloader, operation.result.torrent_hash)).status = 'success'
        }
        return clone(operation.result)
      }
      throw apiError(`演示 GET 不支持 ${path}`, 404)
    },
    async put(path, payload) {
      if (path !== 'plugin/TorrentFormatter') throw apiError('不支持的演示配置路径', 404)
      await new Promise(resolve => setTimeout(resolve, 100))
      if (payload.cron_enabled && payload.cron && !/^[\d\w*,/-]+(?:\s+[\d\w*,/-]+){4}$/.test(payload.cron.trim()))
        throw apiError('定时任务 Cron 表达式无效', 422)
      settings = clone({ ...settings, ...payload })
      tickets.clear()
      return { success: true, message: '演示配置已保存到内存' }
    },
    async post(path, payload) {
      await new Promise(resolve => setTimeout(resolve, 200))
      if (path.endsWith('/delete_history_batch')) {
        if (!payload?.confirmed) throw apiError('必须明确确认批量删除', 422)
        if (!Array.isArray(payload.items) || !payload.items.length || payload.items.length > 100)
          throw apiError('批量删除必须包含 1 到 100 条记录', 422)
        const ids = payload.items.map(item => String(item?.history_id || ''))
        if (ids.some(id => !id) || new Set(ids).size !== ids.length)
          throw apiError('历史 ID 不能为空或重复', 422)
        if (payload.items.some(item => !/^[a-f0-9]{64}$/i.test(item?.delete_signature || '')))
          throw apiError('删除签名格式无效', 422)
        const results = []
        const removed = []
        const invalidated = new Set()
        const affected = new Map()
        for (const request of payload.items) {
          const historyId = String(request.history_id)
          const row = records.get(historyId)
          if (!row) {
            results.push({ history_id: historyId, status: 'not_found', message: '备份记录已不存在' })
            removed.push(historyId)
            continue
          }
          const checked = await historyItem(historyId, row, { fresh: true })
          const disposition = scenario === 'history-batch-mixed' ? row.mock_batch_status : ''
          if (checked.presence.delete_signature !== request.delete_signature || disposition === 'conflict') {
            results.push({
              history_id: historyId,
              status: 'conflict',
              message: '记录已经变化，请重新核验并确认',
              presence: checked.presence,
            })
            continue
          }
          if (!checked.presence.can_delete || disposition === 'busy') {
            results.push({
              history_id: historyId,
              status: 'busy',
              message: '种子或插件正在忙碌，未删除此记录',
              presence: { ...checked.presence, state: 'busy', can_delete: false },
            })
            continue
          }
          if (disposition === 'failed') {
            results.push({ history_id: historyId, status: 'failed', message: '模拟持久化失败，记录保持不变' })
            continue
          }
          records.delete(historyId)
          deletedRecords.set(historyId, clone(row))
          presenceCache.delete(historyId)
          removed.push(historyId)
          results.push({
            history_id: historyId,
            status: 'deleted',
            message: '插件备份记录已删除，未删除种子或文件',
          })
          const target = {
            downloader: checked.presence.target_downloader || row.downloader,
            torrent_hash: checked.presence.target_hash || row.torrent_hash,
          }
          affected.set(identity(target.downloader, target.torrent_hash), target)
          for (const [remainingId, remaining] of records)
            if (remaining.torrent_hash === row.torrent_hash) invalidated.add(remainingId)
          for (const [token, ticket] of tickets)
            if (ticket.historyKey === historyId) {
              tickets.delete(token)
              operations.delete(token)
            }
        }
        const response = {
          status: results.every(item => ['deleted', 'not_found'].includes(item.status)) ? 'success' : 'partial',
          message: `${removed.length} 条记录已确认移除，其余记录保持不变`,
          results,
          removed_history_ids: removed,
          invalidated_history_ids: [...invalidated],
          affected_targets: [...affected.values()],
        }
        if (scenario === 'history-response-lost') {
          const error = new Error('模拟提交完成后响应丢失')
          error.code = 'ECONNRESET'
          throw error
        }
        return clone(response)
      }
      if (path.endsWith('/history_lookup')) {
        if (!Array.isArray(payload?.history_ids) || !payload.history_ids.length || payload.history_ids.length > 100)
          throw apiError('历史核验必须包含 1 到 100 个 ID', 422)
        const ids = payload.history_ids.map(String)
        if (new Set(ids).size !== ids.length) throw apiError('历史核验 ID 不能重复', 422)
        const results = []
        for (const historyId of ids) {
          const row = records.get(historyId)
          if (!row) {
            results.push({ history_id: historyId, status: 'not_found', message: '备份记录不存在' })
            continue
          }
          results.push({
            history_id: historyId,
            status: 'found',
            message: '已实时核验当前备份和种子位置',
            item: await historyItem(historyId, row, { fresh: true }),
          })
        }
        return clone({ results })
      }
      if (path.endsWith('/preview')) return clone(makePreview(payload))
      if (path.endsWith('/process')) return execute(payload.preview_token, 'format')
      if (path.endsWith('/recover_from_history')) {
        const result = execute(payload.preview_token, 'recover')
        return [result.status === 'success', result.message]
      }
      throw apiError(`演示 POST 不支持 ${path}`, 404)
    },
  }
}
