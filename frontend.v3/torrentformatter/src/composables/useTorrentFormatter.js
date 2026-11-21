import { computed, getCurrentScope, onScopeDispose, ref, shallowRef, watch } from 'vue'
import {
  createTorrentFormatterApi,
  DEFAULT_RULES,
  errorMessage,
  mediaIdentity,
  ruleOverrides,
} from '../api/torrentformatter.js'

export const HISTORY_SELECTION_LIMIT = 100
export const PAGE_SIZE_OPTIONS = [20, 50, 100]
export const MOBILE_PAGE_SIZE = 20
const DEFAULT_PAGE_SIZE = MOBILE_PAGE_SIZE

function cloneValue(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value))
}

function historyIdentity(item) {
  return String(item?.history_id || item?.id || '')
}

function targetIdentity(downloader, torrentHash) {
  return downloader && torrentHash ? `${downloader}:${torrentHash}` : ''
}

function requestWasAborted(error) {
  return error?.name === 'AbortError' || error?.code === 'ERR_CANCELED' || error?.response?.status === 499
}

function outcomeIsUnknown(error) {
  const status = Number(error?.response?.status || 0)
  return requestWasAborted(error) || !status || status === 408 || status === 499 || status >= 500
}

/** 页面实例自己的状态机；请求 ID 与目标身份共同阻止旧响应串用。 */
export function useTorrentFormatter(
  client,
  { pollInterval = 2000, maxPolls = 40, historyReuseMs = 5000, now = () => Date.now() } = {},
) {
  const currentClient = () => (typeof client === 'function' ? client() : client)
  const api = createTorrentFormatterApi({
    get: (...args) => currentClient().get(...args),
    post: (...args) => currentClient().post(...args),
  })
  const downloaders = ref([])
  const selectedDownloader = ref('')
  const query = ref('')
  const page = ref(1)
  const pageSize = ref(DEFAULT_PAGE_SIZE)
  const torrents = shallowRef({ items: [], total: 0, capabilities: null })
  const selected = shallowRef(null)
  const detail = shallowRef(null)
  const rulesInfo = shallowRef({
    effective: { ...DEFAULT_RULES },
    defaults: { ...DEFAULT_RULES },
    exclude_dirs: '',
    exclude_tags: '',
    exclude_words: [],
  })
  const draftRules = ref({ ...DEFAULT_RULES })
  const manualMedia = ref(null)
  const season = ref(null)
  const preview = shallowRef(null)
  const previewMode = ref('format')
  const result = shallowRef(null)
  const recoveryTarget = shallowRef(null)
  const history = shallowRef({ items: [], total: 0 })
  const historyPage = ref(1)
  const historyPageSize = ref(DEFAULT_PAGE_SIZE)
  const historyQuery = ref('')
  const historyResultFilter = ref('all')
  const historyPresenceFilter = ref('all')
  const deletingHistoryId = ref('')
  const historySelection = shallowRef(new Map())
  const historyDeleteResults = shallowRef([])
  const historySelectionError = ref('')
  const invalidatedHistoryIds = shallowRef(new Set())
  const unknownHistoryIds = shallowRef(new Set())
  const staleTargets = shallowRef(new Set())
  const staleDetailTargets = shallowRef(new Set())
  const torrentsStale = ref(false)
  const detailStale = ref(false)
  const loading = ref({
    initial: false,
    list: false,
    detail: false,
    preview: false,
    execute: false,
    history: false,
    status: false,
    historyDelete: false,
    historyLookup: false,
  })
  const errors = ref({
    initial: '',
    list: '',
    detail: '',
    preview: '',
    execute: '',
    history: '',
    status: '',
    historyDelete: '',
    historyLookup: '',
  })
  const clock = ref(Date.now())
  const expiresAt = ref(0)
  let initialized = false
  let disposed = false
  let silenceInvalidate = false
  let initId = 0,
    listId = 0,
    detailId = 0,
    previewId = 0,
    historyId = 0,
    executeId = 0,
    deleteId = 0,
    lookupId = 0
  let listAbort, detailAbort, searchTimer, historyTimer, pollTimer, expiryTimer, historyInFlight
  let historyLoadedKey = ''
  let historyLoadedAt = 0
  let historyDeleteEpoch = 0
  let historySnapshotVersion = 0
  const unknownHistoryVersions = new Map()
  const unknownHistorySnapshots = new Map()
  const locallyRemovedHistoryIds = new Set()
  let skipHistoryPageWatch = false
  let pollCount = 0

  const targetKey = computed(() => (selected.value ? `${selectedDownloader.value}:${selected.value.hash}` : ''))
  const overrides = computed(() => ruleOverrides(draftRules.value, rulesInfo.value.effective))
  const overrideCount = computed(() => Object.keys(overrides.value).length)
  const expired = computed(() => Boolean(preview.value?.preview_token && clock.value >= expiresAt.value))
  const validSeason = computed(
    () =>
      season.value === null ||
      season.value === '' ||
      (Number.isInteger(Number(season.value)) && Number(season.value) >= 0 && Number(season.value) <= 99),
  )
  const canPreview = computed(() =>
    Boolean(
      detail.value &&
      validSeason.value &&
      !loading.value.execute &&
      !loading.value.historyDelete &&
      !loading.value.preview &&
      !loading.value.detail,
    ),
  )
  const canExecute = computed(() =>
    Boolean(
      preview.value?.status === 'ready' &&
      preview.value.preview_token &&
      !expired.value &&
      !loading.value.execute &&
      !loading.value.historyDelete &&
      !result.value &&
      !errors.value.execute,
    ),
  )
  const selectedHistory = computed(() => [...historySelection.value.values()])
  const selectedHistoryCount = computed(() => historySelection.value.size)
  const selectedHistoryReady = computed(
    () =>
      selectedHistory.value.length > 0 &&
      selectedHistory.value.every(
        item =>
          item.verification === 'ready' && item.presence?.can_delete && item.delete_signature && !item.history_stale,
      ),
  )
  const currentHistorySelectable = computed(() => history.value.items.filter(isHistorySelectable))
  const allCurrentHistorySelected = computed(
    () =>
      currentHistorySelectable.value.length > 0 &&
      currentHistorySelectable.value.every(item => historySelection.value.has(historyIdentity(item))),
  )
  const someCurrentHistorySelected = computed(
    () =>
      !allCurrentHistorySelected.value &&
      currentHistorySelectable.value.some(item => historySelection.value.has(historyIdentity(item))),
  )

  function isHistorySelectable(item) {
    const id = historyIdentity(item)
    const presence = item?.presence
    return Boolean(
      id &&
      !item.history_stale &&
      !invalidatedHistoryIds.value.has(id) &&
      !unknownHistoryIds.value.has(id) &&
      presence?.can_delete &&
      presence.delete_signature &&
      presence.state !== 'busy',
    )
  }

  function selectedSnapshot(item, extra = {}) {
    const snapshot = cloneValue(item)
    const id = historyIdentity(snapshot)
    return {
      ...snapshot,
      id,
      history_id: id,
      delete_signature: snapshot.presence?.delete_signature || snapshot.delete_signature || '',
      verification: extra.verification || snapshot.verification || 'ready',
      selection_message: extra.selection_message ?? snapshot.selection_message ?? '',
      requires_confirmation: Boolean(extra.requires_confirmation ?? snapshot.requires_confirmation),
    }
  }

  function replaceHistorySelection(next) {
    historySelection.value = new Map(next)
  }

  function clearHistorySelection({ results = true } = {}) {
    replaceHistorySelection()
    historySelectionError.value = ''
    if (results) historyDeleteResults.value = []
  }

  function toggleHistorySelection(item, checked) {
    if (loading.value.historyDelete || loading.value.historyLookup) return false
    const id = historyIdentity(item)
    if (!id) return false
    const next = new Map(historySelection.value)
    const shouldSelect = checked === undefined ? !next.has(id) : Boolean(checked)
    if (!shouldSelect) {
      next.delete(id)
      replaceHistorySelection(next)
      historySelectionError.value = ''
      return true
    }
    if (!isHistorySelectable(item)) {
      historySelectionError.value = '此记录当前不可删除，请先实时核验位置与备份状态'
      return false
    }
    if (!next.has(id) && next.size >= HISTORY_SELECTION_LIMIT) {
      historySelectionError.value = `每次最多选择 ${HISTORY_SELECTION_LIMIT} 条不同历史记录`
      return false
    }
    next.set(id, selectedSnapshot(item))
    replaceHistorySelection(next)
    historySelectionError.value = ''
    return true
  }

  function toggleCurrentHistoryPage() {
    if (loading.value.historyDelete || loading.value.historyLookup) return false
    const next = new Map(historySelection.value)
    if (allCurrentHistorySelected.value) {
      for (const item of currentHistorySelectable.value) next.delete(historyIdentity(item))
      replaceHistorySelection(next)
      historySelectionError.value = ''
      return true
    }
    for (const item of currentHistorySelectable.value) {
      const id = historyIdentity(item)
      if (next.has(id)) continue
      if (next.size >= HISTORY_SELECTION_LIMIT) {
        historySelectionError.value = `每次最多选择 ${HISTORY_SELECTION_LIMIT} 条；已保留先前选择`
        replaceHistorySelection(next)
        return false
      }
      next.set(id, selectedSnapshot(item))
    }
    replaceHistorySelection(next)
    historySelectionError.value = ''
    return true
  }

  function syncSelectedHistory(items, { verified = false } = {}) {
    if (!historySelection.value.size) return
    const next = new Map(historySelection.value)
    let changed = false
    for (const item of items) {
      const id = historyIdentity(item)
      const previous = next.get(id)
      if (!previous) continue
      let verification = previous.verification
      let selectionMessage = previous.selection_message
      if (verified) {
        const selectable = Boolean(
          item?.presence?.can_delete && item.presence.delete_signature && item.presence.state !== 'busy',
        )
        verification = selectable ? 'ready' : 'changed'
        selectionMessage = selectable
          ? '已实时核验；再次删除仍需重新确认完整清单'
          : item.presence?.message || '当前状态不允许删除'
      } else if (invalidatedHistoryIds.value.has(id) || item.history_stale) {
        verification = 'stale'
        selectionMessage = '关联记录已变化，请显式重新核验'
      } else if (previous.verification !== 'unknown' && previous.delete_signature !== item.presence?.delete_signature) {
        verification = 'changed'
        selectionMessage = '删除签名已变化，请显式重新核验并重新确认'
      }
      next.set(
        id,
        selectedSnapshot(item, {
          verification,
          selection_message: selectionMessage,
          requires_confirmation: previous.requires_confirmation || verification !== 'ready',
        }),
      )
      changed = true
    }
    if (changed) replaceHistorySelection(next)
  }

  function setHistoryPageSilently(value) {
    if (historyPage.value === value) return
    skipHistoryPageWatch = true
    historyPage.value = value
  }

  function currentHistoryKey(pageNumber = historyPage.value) {
    return JSON.stringify([
      Number(pageNumber),
      historyPageSize.value,
      historyQuery.value,
      historyResultFilter.value,
      historyPresenceFilter.value,
    ])
  }

  function stopHistoryRequest() {
    historyId++
    historyInFlight?.controller.abort()
    historyInFlight = undefined
    loading.value.history = false
  }

  function refreshStaleFlags() {
    torrentsStale.value = staleTargets.value.size > 0
    detailStale.value = Boolean(targetKey.value && staleDetailTargets.value.has(targetKey.value))
  }

  function clearStaleTargetsForDownloader(downloader) {
    if (!downloader || !staleTargets.value.size) return
    const next = new Set([...staleTargets.value].filter(key => !key.startsWith(`${downloader}:`)))
    staleTargets.value = next
    refreshStaleFlags()
  }

  function clearStaleTarget(downloader, torrentHash) {
    const key = targetIdentity(downloader, torrentHash)
    if (!key) return
    if (staleDetailTargets.value.has(key)) {
      const next = new Set(staleDetailTargets.value)
      next.delete(key)
      staleDetailTargets.value = next
    }
    refreshStaleFlags()
  }

  function stopPolling() {
    clearTimeout(pollTimer)
    clearInterval(expiryTimer)
    pollTimer = expiryTimer = undefined
  }

  function invalidatePreview() {
    if (silenceInvalidate) return
    previewId++
    preview.value = null
    previewMode.value = 'format'
    result.value = null
    recoveryTarget.value = null
    errors.value.preview = ''
    errors.value.execute = ''
    loading.value.preview = false
    stopPolling()
  }

  function resetSelection() {
    detailId++
    detailAbort?.abort()
    detail.value = selected.value = null
    refreshStaleFlags()
    manualMedia.value = null
    season.value = null
    draftRules.value = { ...rulesInfo.value.effective }
    errors.value.detail = ''
    loading.value.detail = false
    invalidatePreview()
  }

  function resetWorkspace() {
    stop()
    initialized = false
    resetSelection()
    clearHistorySelection()
    invalidatedHistoryIds.value = new Set()
    unknownHistoryIds.value = new Set()
    unknownHistoryVersions.clear()
    unknownHistorySnapshots.clear()
    locallyRemovedHistoryIds.clear()
    historySnapshotVersion = 0
    staleTargets.value = new Set()
    staleDetailTargets.value = new Set()
    torrentsStale.value = false
    detailStale.value = false
    historyLoadedKey = ''
    historyLoadedAt = 0
    downloaders.value = []
    torrents.value = { items: [], total: 0, capabilities: null }
    history.value = { items: [], total: 0 }
    loading.value = {
      initial: false,
      list: false,
      detail: false,
      preview: false,
      execute: false,
      history: false,
      status: false,
      historyDelete: false,
      historyLookup: false,
    }
    errors.value = {
      initial: '',
      list: '',
      detail: '',
      preview: '',
      execute: '',
      history: '',
      status: '',
      historyDelete: '',
      historyLookup: '',
    }
    deletingHistoryId.value = ''
    historyQuery.value = ''
    historyResultFilter.value = historyPresenceFilter.value = 'all'
    query.value = ''
    page.value = historyPage.value = 1
    selectedDownloader.value = ''
  }

  async function initialize() {
    const id = ++initId
    loading.value.initial = true
    errors.value.initial = ''
    try {
      const [clients, rules] = await Promise.all([api.downloaders(), api.rules()])
      if (disposed || id !== initId) return
      if (!rules?.effective) throw new Error('有效规则响应无效，请确认后端已更新')
      downloaders.value = clients
      rulesInfo.value = rules
      if (!selected.value) draftRules.value = { ...rules.effective }
      const previous = selectedDownloader.value
      selectedDownloader.value = clients.some(item => item.name === previous) ? previous : ''
      initialized = true
      if (selectedDownloader.value) await loadTorrents()
    } catch (error) {
      if (id === initId && !disposed) errors.value.initial = errorMessage(error, '工作台初始化失败')
    } finally {
      if (id === initId) loading.value.initial = false
    }
  }

  async function refresh() {
    if (loading.value.execute) return
    invalidatePreview()
    const previous = overrides.value
    const identity = targetKey.value
    initialized = false
    await initialize()
    if (identity === targetKey.value) {
      silenceInvalidate = true
      draftRules.value = { ...rulesInfo.value.effective, ...previous }
      silenceInvalidate = false
      if (selected.value) await loadSelectedDetail()
    }
    await refreshHistory()
  }

  async function loadTorrents() {
    if (!selectedDownloader.value || disposed) return
    const id = ++listId
    listAbort?.abort()
    listAbort = new AbortController()
    loading.value.list = true
    errors.value.list = ''
    try {
      const data = await api.torrents(
        { downloader: selectedDownloader.value, query: query.value, page: page.value, page_size: pageSize.value },
        listAbort.signal,
      )
      if (id !== listId || disposed) return
      if (!Array.isArray(data?.items)) throw new Error('种子列表响应无效')
      torrents.value = data
      clearStaleTargetsForDownloader(selectedDownloader.value)
    } catch (error) {
      if (id === listId && !disposed) errors.value.list = errorMessage(error, '种子列表加载失败')
    } finally {
      if (id === listId) loading.value.list = false
    }
  }

  function setTorrentPagination({ page: nextPage, pageSize: nextSize }) {
    if (loading.value.execute || loading.value.historyDelete || disposed) return false
    if (!PAGE_SIZE_OPTIONS.includes(nextSize) || !Number.isInteger(nextPage) || nextPage < 1) return false
    const pages = Math.max(1, Math.ceil(torrents.value.total / nextSize))
    if (nextPage > pages) return false
    // 数量变更先同步重置页码，再应用指定页；组合 watcher 只提交一次列表请求。
    pageSize.value = nextSize
    page.value = nextPage
    return true
  }

  async function selectTorrent(item) {
    if (loading.value.execute) return false
    // 重复核对当前目标不清空已编辑的媒体、规则及预览。
    if (selected.value?.hash === item.hash) {
      if (loading.value.detail) return false
      return detail.value ? ensureSelectedTargetFresh() : loadSelectedDetail()
    }
    resetSelection()
    selected.value = item
    return loadSelectedDetail()
  }

  async function loadSelectedDetail() {
    if (!selected.value || disposed) return false
    const hash = selected.value.hash
    const id = ++detailId
    const identity = targetKey.value
    detailAbort?.abort()
    detailAbort = new AbortController()
    loading.value.detail = true
    errors.value.detail = ''
    try {
      const data = await api.detail({ downloader: selectedDownloader.value, torrent_hash: hash }, detailAbort.signal)
      if (id !== detailId || identity !== targetKey.value || disposed) return false
      if (data?.torrent?.hash !== hash || !Array.isArray(data?.torrent?.files)) throw new Error('种子详情响应无效')
      detail.value = data
      clearStaleTarget(selectedDownloader.value, hash)
      return true
    } catch (error) {
      if (id === detailId && !disposed) {
        detail.value = null
        errors.value.detail = errorMessage(error, '种子详情加载失败')
      }
      return false
    } finally {
      if (id === detailId) loading.value.detail = false
    }
  }

  async function ensureTorrentsFresh() {
    if (
      !selectedDownloader.value ||
      ![...staleTargets.value].some(key => key.startsWith(`${selectedDownloader.value}:`))
    )
      return true
    await loadTorrents()
    return !errors.value.list
  }

  async function ensureSelectedTargetFresh() {
    if (!selected.value || (!detailStale.value && !staleDetailTargets.value.has(targetKey.value))) return true
    return loadSelectedDetail()
  }

  async function generatePreview(mode = 'format', target = null) {
    if (loading.value.execute || loading.value.historyDelete || disposed) return null
    const requestedTargetKey = targetKey.value
    if (
      mode === 'format' &&
      (detailStale.value || staleDetailTargets.value.has(requestedTargetKey)) &&
      (!(await ensureSelectedTargetFresh()) || requestedTargetKey !== targetKey.value)
    )
      return null
    const identity =
      mode === 'recover'
        ? target
        : selected.value
          ? { downloader: selectedDownloader.value, torrent_hash: selected.value.hash }
          : null
    if (!identity) return null
    invalidatePreview()
    previewMode.value = mode
    recoveryTarget.value = mode === 'recover' ? { ...identity } : null
    const id = ++previewId
    const selectedKey = targetKey.value
    loading.value.preview = true
    const payload = { downloader: identity.downloader, torrent_hash: identity.torrent_hash, mode }
    if (mode === 'format') {
      payload.overrides = overrides.value
      if (manualMedia.value) payload.media = mediaIdentity(manualMedia.value)
      if (season.value !== null && season.value !== '') payload.season = Number(season.value)
    }
    try {
      const data = await api.preview(payload)
      if (id !== previewId || disposed || (mode === 'format' && selectedKey !== targetKey.value)) return null
      if (!['ready', 'blocked', 'no_changes'].includes(data?.status)) throw new Error('预览响应无效')
      recoveryTarget.value = mode === 'recover' ? { ...identity } : null
      if (mode === 'format') {
        silenceInvalidate = true
        draftRules.value = { ...data.rules }
        if (detail.value && data.media) detail.value = { ...detail.value, media: data.media }
        silenceInvalidate = false
      }
      preview.value = data
      clock.value = Date.now()
      expiresAt.value = clock.value + Math.max(0, Number(data.expires_in || 0)) * 1000
      if (data.preview_token) {
        expiryTimer = setInterval(() => {
          clock.value = Date.now()
        }, 1000)
        expiryTimer.unref?.()
      }
      return data
    } catch (error) {
      if (id === previewId && !disposed) errors.value.preview = errorMessage(error, '预览生成失败')
      return null
    } finally {
      if (id === previewId) loading.value.preview = false
    }
  }

  function applyResult(data) {
    result.value = data
    if (
      detail.value &&
      data.actual &&
      selected.value?.hash === data.torrent_hash &&
      selectedDownloader.value === data.downloader
    ) {
      detail.value = {
        ...detail.value,
        torrent: data.actual,
        processed: data.mode === 'format' || data.status !== 'success',
      }
    }
  }

  async function executePreview() {
    if (!canExecute.value) return null
    const id = ++executeId
    const frozen = preview.value
    const source = recoveryTarget.value
    loading.value.execute = true
    errors.value.execute = ''
    stopPolling()
    try {
      let data
      if (frozen.mode === 'recover') {
        const tuple = await api.recover(source, frozen.preview_token)
        if (id !== executeId || disposed) return null
        try {
          data = await api.status(frozen.preview_token)
        } catch (error) {
          throw new Error(Array.isArray(tuple) ? tuple[1] : errorMessage(error), { cause: error })
        }
      } else {
        data = await api.process(frozen.preview_token)
      }
      if (!disposed && id === executeId) {
        applyResult(data)
        pollCount = 0
        if (data.status === 'verifying') schedulePoll()
      }
      return data
    } catch (error) {
      // 请求超时可能已发生修改，只回查同一操作，绝不自动重新提交。
      if (!disposed && id === executeId) {
        errors.value.execute = errorMessage(error, '执行结果不确定，请检查处理历史')
        try {
          const data = await api.status(frozen.preview_token)
          if (!disposed && id === executeId) {
            applyResult(data)
            if (data.status === 'verifying') schedulePoll()
          }
          return data
        } catch {
          /* 保留错误与历史入口，不把未知状态提示为成功。 */
        }
      }
      return null
    } finally {
      if (id === executeId && !disposed) {
        loading.value.execute = false
        await loadHistory()
        await loadTorrents()
      }
    }
  }

  async function refreshResult() {
    if (!result.value || loading.value.status || disposed) return
    const operation = result.value.operation_id
    loading.value.status = true
    errors.value.status = ''
    try {
      const data = await api.status(operation)
      if (result.value?.operation_id === operation && !disposed) {
        applyResult(data)
        if (data.status !== 'verifying') {
          stopPolling()
          await loadHistory()
        }
      }
    } catch (error) {
      if (!disposed && result.value?.operation_id === operation)
        errors.value.status = errorMessage(error, '结果回查失败，备份仍保留')
    } finally {
      loading.value.status = false
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer)
    if (disposed || result.value?.status !== 'verifying' || pollCount >= maxPolls) return
    pollTimer = setTimeout(async () => {
      pollCount++
      await refreshResult()
      if (!errors.value.status) schedulePoll()
    }, pollInterval)
    pollTimer.unref?.()
  }

  function loadHistory(options = {}) {
    const settings = options && typeof options === 'object' && !('currentTarget' in options) ? options : {}
    if (
      disposed ||
      (loading.value.historyDelete && !settings.afterDelete) ||
      (loading.value.historyLookup && !settings.afterLookup)
    )
      return Promise.resolve(null)
    clearTimeout(historyTimer)
    historyTimer = undefined
    const requestedPage = Number(settings.page || historyPage.value)
    const key = currentHistoryKey(requestedPage)
    const fresh = settings.fresh === true
    if (!fresh && !settings.force && historyInFlight?.key === key) return historyInFlight.promise
    historyInFlight?.controller.abort()
    const controller = new AbortController()
    const id = ++historyId
    const epoch = historyDeleteEpoch
    loading.value.history = true
    errors.value.history = ''
    const promise = (async () => {
      let retryLastPage = false
      let accepted = null
      try {
        const data = await api.history(
          requestedPage,
          historyPageSize.value,
          {
            query: historyQuery.value,
            result_filter: historyResultFilter.value,
            presence_filter: historyPresenceFilter.value,
          },
          { signal: controller.signal, fresh },
        )
        if (id !== historyId || epoch !== historyDeleteEpoch || disposed) return null
        if (!Array.isArray(data?.items) || !Number.isFinite(Number(data.total))) throw new Error('历史列表响应无效')
        if (fresh && invalidatedHistoryIds.value.size) {
          const next = new Set(invalidatedHistoryIds.value)
          for (const item of data.items) next.delete(historyIdentity(item))
          invalidatedHistoryIds.value = next
        }
        historySnapshotVersion++
        locallyRemovedHistoryIds.clear()
        const items = data.items.map(item => ({
          ...item,
          history_stale:
            invalidatedHistoryIds.value.has(historyIdentity(item)) ||
            unknownHistoryIds.value.has(historyIdentity(item)),
          deletion_unknown: unknownHistoryIds.value.has(historyIdentity(item)),
        }))
        const lastPage = Math.max(1, Math.ceil(Number(data.total) / historyPageSize.value))
        if (requestedPage > lastPage && Number(data.total) > 0) {
          setHistoryPageSilently(lastPage)
          retryLastPage = true
        } else {
          history.value = { ...data, items, total: Number(data.total) }
          syncSelectedHistory(items)
          historyLoadedKey = key
          historyLoadedAt = now()
          accepted = history.value
        }
      } catch (error) {
        if (id === historyId && epoch === historyDeleteEpoch && !disposed && !requestWasAborted(error))
          errors.value.history = errorMessage(error, '处理历史加载失败')
      } finally {
        if (historyInFlight?.id === id) historyInFlight = undefined
        if (id === historyId) loading.value.history = false
      }
      if (retryLastPage && id === historyId && !disposed) return loadHistory({ force: true })
      return accepted
    })()
    historyInFlight = { id, key, controller, promise }
    return promise
  }

  function enterHistory() {
    const key = currentHistoryKey()
    if (historyLoadedKey === key && now() - historyLoadedAt <= historyReuseMs) return Promise.resolve(history.value)
    return loadHistory()
  }

  function refreshHistory() {
    return loadHistory({ fresh: true, force: true })
  }

  function markHistoryInvalidated(ids) {
    if (!ids.length) return
    const nextInvalidated = new Set(invalidatedHistoryIds.value)
    for (const id of ids) nextInvalidated.add(id)
    invalidatedHistoryIds.value = nextInvalidated
    history.value = {
      ...history.value,
      items: history.value.items.map(item =>
        nextInvalidated.has(historyIdentity(item)) ? { ...item, history_stale: true } : item,
      ),
    }
    const nextSelection = new Map(historySelection.value)
    for (const id of ids) {
      const selectedItem = nextSelection.get(id)
      if (!selectedItem) continue
      nextSelection.set(
        id,
        selectedSnapshot(selectedItem, {
          verification: selectedItem.verification === 'unknown' ? 'unknown' : 'stale',
          selection_message: selectedItem.selection_message || '关联备份状态已变化，请显式重新核验',
          requires_confirmation: true,
        }),
      )
    }
    replaceHistorySelection(nextSelection)
  }

  function markHistoryUnknown(results) {
    const entries = (results || []).filter(item => item?.status === 'unknown' && item.history_id)
    if (!entries.length) return
    const nextUnknown = new Set(unknownHistoryIds.value)
    const messages = new Map()
    for (const entry of entries) {
      const id = String(entry.history_id)
      nextUnknown.add(id)
      messages.set(id, entry.message || '删除结果未知，请显式核验')
      if (!unknownHistoryVersions.has(id)) unknownHistoryVersions.set(id, historySnapshotVersion)
      const selectedItem = historySelection.value.get(id)
      if (selectedItem) unknownHistorySnapshots.set(id, selectedSnapshot(selectedItem))
    }
    unknownHistoryIds.value = nextUnknown
    history.value = {
      ...history.value,
      items: history.value.items.map(item =>
        nextUnknown.has(historyIdentity(item)) ? { ...item, history_stale: true, deletion_unknown: true } : item,
      ),
    }
    const nextSelection = new Map(historySelection.value)
    for (const [id, message] of messages) {
      const selectedItem = nextSelection.get(id)
      if (!selectedItem) continue
      nextSelection.set(
        id,
        selectedSnapshot(selectedItem, {
          verification: 'unknown',
          selection_message: message,
          requires_confirmation: true,
        }),
      )
    }
    replaceHistorySelection(nextSelection)
  }

  function markAffectedTargets(response, submitted) {
    const targets = new Set(staleTargets.value)
    for (const target of response?.affected_targets || []) {
      const key = targetIdentity(target?.downloader, target?.torrent_hash)
      if (key) targets.add(key)
    }
    const changedIds = new Set(
      (response?.results || [])
        .filter(item => ['deleted', 'not_found'].includes(item?.status))
        .map(item => String(item.history_id)),
    )
    for (const item of submitted) {
      if (!changedIds.has(historyIdentity(item))) continue
      for (const location of item.presence?.locations || []) {
        const key = targetIdentity(location?.downloader, location?.torrent_hash)
        if (key) targets.add(key)
      }
      const targetKey = targetIdentity(item.presence?.target_downloader, item.presence?.target_hash)
      const sourceKey = targetIdentity(item.downloader, item.torrent_hash)
      if (targetKey) targets.add(targetKey)
      if (sourceKey) targets.add(sourceKey)
    }
    staleTargets.value = targets
    staleDetailTargets.value = new Set([...staleDetailTargets.value, ...targets])
    refreshStaleFlags()
    const invalidated = [...new Set((response?.invalidated_history_ids || []).map(String).filter(Boolean))]
    markHistoryInvalidated(invalidated)
    const previewKey = targetIdentity(preview.value?.downloader, preview.value?.torrent_hash)
    if (
      (previewKey && targets.has(previewKey)) ||
      invalidated.includes(historyIdentity(recoveryTarget.value)) ||
      submitted.some(item => invalidated.includes(historyIdentity(item)))
    )
      invalidatePreview()
  }

  function removeHistoryLocally(ids, { skipTotalIds = new Set() } = {}) {
    const removed = new Set(ids)
    if (!removed.size) return false
    let decrement = 0
    for (const id of removed) {
      if (!skipTotalIds.has(id) && !locallyRemovedHistoryIds.has(id)) decrement++
      locallyRemovedHistoryIds.add(id)
    }
    history.value = {
      ...history.value,
      items: history.value.items.filter(item => !removed.has(historyIdentity(item))),
      total: Math.max(0, Number(history.value.total) - decrement),
    }
    const nextSelection = new Map(historySelection.value)
    for (const id of removed) nextSelection.delete(id)
    replaceHistorySelection(nextSelection)
    const lastPage = Math.max(1, Math.ceil(history.value.total / historyPageSize.value))
    if (!history.value.items.length && history.value.total > 0) {
      setHistoryPageSilently(Math.min(historyPage.value, lastPage))
      return true
    }
    return false
  }

  function updateHistoryFromDeleteResult(result) {
    if (['deleted', 'not_found'].includes(result.status)) return
    const id = String(result.history_id)
    let current
    if (result.presence)
      history.value = {
        ...history.value,
        items: history.value.items.map(item => {
          if (historyIdentity(item) !== id) return item
          current = { ...item, presence: result.presence, history_stale: false }
          return current
        }),
      }
    const previous = historySelection.value.get(id)
    if (!previous) return
    current ||= result.presence ? { ...previous, presence: result.presence, history_stale: false } : previous
    const next = new Map(historySelection.value)
    next.set(
      id,
      selectedSnapshot(current, {
        verification:
          result.status === 'conflict' || (result.presence && !isHistorySelectable(current))
            ? 'changed'
            : previous.verification,
        selection_message: result.message,
        requires_confirmation: true,
      }),
    )
    replaceHistorySelection(next)
  }

  function normalizeDeleteResults(response, submitted) {
    const allowed = new Set(['deleted', 'not_found', 'conflict', 'busy', 'failed'])
    const byId = new Map(
      (Array.isArray(response?.results) ? response.results : [])
        .filter(item => item && allowed.has(item.status) && item.history_id)
        .map(item => [String(item.history_id), { ...item, history_id: String(item.history_id) }]),
    )
    return submitted.map(item => {
      const id = historyIdentity(item)
      return (
        byId.get(id) || {
          history_id: id,
          status: 'unknown',
          message: '批次响应缺少此记录的逐项结果，请显式核验',
        }
      )
    })
  }

  async function deleteHistoryBatch(items) {
    if (loading.value.execute || loading.value.historyDelete || loading.value.historyLookup || disposed) return null
    const unique = new Map()
    for (const item of Array.isArray(items) ? items : []) unique.set(historyIdentity(item), selectedSnapshot(item))
    unique.delete('')
    const submitted = [...unique.values()]
    if (!submitted.length || submitted.length > HISTORY_SELECTION_LIMIT) {
      errors.value.historyDelete = submitted.length
        ? `每次最多删除 ${HISTORY_SELECTION_LIMIT} 条不同历史记录`
        : '没有可删除的历史记录'
      return null
    }
    if (
      submitted.some(
        item =>
          item.verification !== 'ready' ||
          !item.presence?.can_delete ||
          !item.delete_signature ||
          item.history_stale ||
          invalidatedHistoryIds.value.has(historyIdentity(item)) ||
          unknownHistoryIds.value.has(historyIdentity(item)),
      )
    ) {
      errors.value.historyDelete = '所选记录包含未知或已变化状态，请先显式核验并重新确认'
      return null
    }
    const nextSelection = new Map(historySelection.value)
    for (const item of submitted) nextSelection.set(historyIdentity(item), item)
    replaceHistorySelection(nextSelection)
    const id = ++deleteId
    historyDeleteEpoch++
    stopHistoryRequest()
    deletingHistoryId.value = submitted.length === 1 ? historyIdentity(submitted[0]) : '__batch__'
    loading.value.historyDelete = true
    errors.value.historyDelete = ''
    errors.value.historyLookup = ''
    historyDeleteResults.value = []
    let normalizedResponse = null
    let refill = false
    try {
      const response = await api.deleteHistoryBatch(submitted)
      if (id !== deleteId || disposed) return null
      const results = normalizeDeleteResults(response, submitted)
      historyDeleteResults.value = results
      const removed = [
        ...new Set(results.filter(item => ['deleted', 'not_found'].includes(item.status)).map(item => item.history_id)),
      ]
      for (const result of results) updateHistoryFromDeleteResult(result)
      refill = removeHistoryLocally(removed)
      markAffectedTargets({ ...response, results }, submitted)
      markHistoryUnknown(results)
      if (results.some(item => item.status === 'unknown'))
        errors.value.historyDelete = '部分逐项回执缺失，结果未知；不会自动重发，请显式核验'
      normalizedResponse = { ...response, results }
    } catch (error) {
      if (id !== deleteId || disposed) return null
      const unknown = outcomeIsUnknown(error)
      const message = unknown
        ? '删除请求响应丢失，结果未知；已保留选择且不会自动重发，请显式核验'
        : errorMessage(error, '备份记录批量删除失败')
      errors.value.historyDelete = message
      const status = unknown ? 'unknown' : error?.response?.status === 409 ? 'busy' : 'failed'
      historyDeleteResults.value = submitted.map(item => ({
        history_id: historyIdentity(item),
        status,
        message,
      }))
      if (unknown) markHistoryUnknown(historyDeleteResults.value)
      else {
        const next = new Map(historySelection.value)
        for (const item of submitted) {
          const selectedItem = next.get(historyIdentity(item))
          next.set(
            historyIdentity(item),
            selectedSnapshot(selectedItem, {
              verification: selectedItem.verification,
              selection_message: message,
              requires_confirmation: true,
            }),
          )
        }
        replaceHistorySelection(next)
      }
    } finally {
      if (id === deleteId) {
        historyDeleteEpoch++
        loading.value.historyDelete = false
        deletingHistoryId.value = ''
      }
    }
    if (refill && id === deleteId && !disposed) await loadHistory({ force: true, afterDelete: true })
    return normalizedResponse
  }

  async function lookupHistoryIds(ids) {
    if (loading.value.historyDelete || loading.value.historyLookup || disposed) return null
    const historyIds = [...new Set(ids.map(String).filter(Boolean))].slice(0, HISTORY_SELECTION_LIMIT)
    if (!historyIds.length) return { results: [] }
    const id = ++lookupId
    historyDeleteEpoch++
    stopHistoryRequest()
    loading.value.historyLookup = true
    errors.value.historyLookup = ''
    historySelectionError.value = ''
    let refill = false
    try {
      const response = await api.historyLookup(historyIds)
      if (id !== lookupId || disposed || !Array.isArray(response?.results)) return null
      const byId = new Map(response.results.map(item => [String(item.history_id), item]))
      const removed = []
      const visible = new Map(history.value.items.map(item => [historyIdentity(item), item]))
      const lookupSources = new Map(
        historyIds.map(historyId => [
          historyId,
          historySelection.value.get(historyId) || visible.get(historyId) || unknownHistorySnapshots.get(historyId),
        ]),
      )
      const nextInvalidated = new Set(invalidatedHistoryIds.value)
      const resolvedUnknown = new Set()
      const lookupResults = []
      for (const historyId of historyIds) {
        const result = byId.get(historyId)
        if (!result) {
          lookupResults.push({ history_id: historyId, status: 'unknown', message: '核验响应缺少此记录' })
          continue
        }
        lookupResults.push(result)
        if (result.status === 'not_found') {
          removed.push(historyId)
          resolvedUnknown.add(historyId)
          continue
        }
        if (result.status !== 'found' || !result.item) continue
        resolvedUnknown.add(historyId)
        const item = {
          ...result.item,
          id: historyIdentity(result.item) || historyId,
          history_stale: false,
          deletion_unknown: false,
        }
        visible.set(historyId, item)
        nextInvalidated.delete(historyId)
        locallyRemovedHistoryIds.delete(historyId)
        syncSelectedHistory([item], { verified: true })
      }
      markAffectedTargets({ results: lookupResults }, [...lookupSources.values()].filter(Boolean))
      const skipTotalIds = new Set(
        removed.filter(historyId => {
          const unknownVersion = unknownHistoryVersions.get(historyId)
          return unknownVersion !== undefined && historySnapshotVersion > unknownVersion && !visible.has(historyId)
        }),
      )
      const nextUnknown = new Set(unknownHistoryIds.value)
      for (const historyId of resolvedUnknown) {
        nextUnknown.delete(historyId)
        unknownHistoryVersions.delete(historyId)
        unknownHistorySnapshots.delete(historyId)
        nextInvalidated.delete(historyId)
      }
      unknownHistoryIds.value = nextUnknown
      invalidatedHistoryIds.value = nextInvalidated
      history.value = {
        ...history.value,
        items: history.value.items.map(item => visible.get(historyIdentity(item)) || item),
      }
      refill = removeHistoryLocally(removed, { skipTotalIds })
      historyDeleteResults.value = lookupResults
      if (!lookupResults.some(item => item.status === 'unknown')) errors.value.historyDelete = ''
      else errors.value.historyLookup = '部分核验回执缺失，仍保留未知选择'
      return { ...response, results: lookupResults }
    } catch (error) {
      if (id === lookupId && !disposed)
        errors.value.historyLookup = errorMessage(error, '所选历史记录核验失败，未知选择仍保留')
      return null
    } finally {
      if (id === lookupId) loading.value.historyLookup = false
      if (refill && id === lookupId && !disposed) await loadHistory({ force: true })
    }
  }

  function verifyHistorySelection(ids = selectedHistory.value.map(item => historyIdentity(item))) {
    return lookupHistoryIds(ids)
  }

  async function refreshHistoryItem(item) {
    const id = historyIdentity(item)
    if (!id) return null
    const response = await lookupHistoryIds([id])
    const result = response?.results?.find(entry => String(entry.history_id) === id)
    return result?.status === 'found' ? result.item : null
  }

  function deleteHistory(item) {
    return deleteHistoryBatch([item])
  }

  function resetRules() {
    draftRules.value = { ...rulesInfo.value.effective }
  }

  function stop() {
    stopPolling()
    clearTimeout(searchTimer)
    clearTimeout(historyTimer)
    historyTimer = undefined
    listAbort?.abort()
    detailAbort?.abort()
    historyInFlight?.controller.abort()
    historyInFlight = undefined
    historyDeleteEpoch++
    initId++
    listId++
    detailId++
    previewId++
    historyId++
    executeId++
    deleteId++
    lookupId++
    clearHistorySelection()
    invalidatedHistoryIds.value = new Set()
    unknownHistoryIds.value = new Set()
    unknownHistoryVersions.clear()
    unknownHistorySnapshots.clear()
    locallyRemovedHistoryIds.clear()
    historySnapshotVersion = 0
    staleTargets.value = new Set()
    staleDetailTargets.value = new Set()
    torrentsStale.value = false
    detailStale.value = false
  }

  watch(
    selectedDownloader,
    () => {
      resetSelection()
      torrents.value = { items: [], total: 0, capabilities: null }
      page.value = 1
      if (initialized) void loadTorrents()
    },
    { flush: 'sync' },
  )
  watch(query, () => {
    clearTimeout(searchTimer)
    page.value = 1
    searchTimer = setTimeout(() => {
      if (initialized) void loadTorrents()
    }, 300)
  })
  watch(pageSize, () => { page.value = 1 }, { flush: 'sync' })
  watch([page, pageSize], () => {
    if (initialized) void loadTorrents()
  })
  watch([historyQuery, historyResultFilter, historyPresenceFilter], (values, previous) => {
    if (loading.value.historyDelete) return
    clearTimeout(historyTimer)
    stopHistoryRequest()
    clearHistorySelection()
    historyLoadedKey = ''
    historyLoadedAt = 0
    setHistoryPageSilently(1)
    if (!initialized) return
    historyTimer = setTimeout(
      () => {
        historyTimer = undefined
        void loadHistory()
      },
      values[0] !== previous[0] ? 300 : 0,
    )
  })
  watch(historyPageSize, () => {
    clearTimeout(historyTimer)
    stopHistoryRequest()
    historyLoadedKey = ''
    historyLoadedAt = 0
    setHistoryPageSilently(1)
    if (initialized) void loadHistory()
  })
  watch(historyPage, () => {
    if (skipHistoryPageWatch) {
      skipHistoryPageWatch = false
      return
    }
    if (initialized && !historyTimer) void loadHistory()
  })
  watch([manualMedia, season, draftRules], invalidatePreview, { deep: true, flush: 'sync' })
  if (getCurrentScope())
    onScopeDispose(() => {
      disposed = true
      stop()
    })

  return {
    api,
    downloaders,
    selectedDownloader,
    query,
    page,
    pageSize,
    setTorrentPagination,
    torrents,
    selected,
    detail,
    rulesInfo,
    draftRules,
    manualMedia,
    season,
    preview,
    previewMode,
    result,
    history,
    historyPage,
    historyPageSize,
    historyQuery,
    historyResultFilter,
    historyPresenceFilter,
    deletingHistoryId,
    historySelection,
    selectedHistory,
    selectedHistoryCount,
    selectedHistoryReady,
    currentHistorySelectable,
    allCurrentHistorySelected,
    someCurrentHistorySelected,
    historyDeleteResults,
    historySelectionError,
    invalidatedHistoryIds,
    torrentsStale,
    detailStale,
    recoveryTarget,
    loading,
    errors,
    targetKey,
    overrideCount,
    expired,
    canPreview,
    canExecute,
    initialize,
    refresh,
    resetWorkspace,
    selectTorrent,
    loadSelectedDetail,
    loadTorrents,
    ensureTorrentsFresh,
    ensureSelectedTargetFresh,
    generatePreview,
    executePreview,
    refreshResult,
    loadHistory,
    enterHistory,
    refreshHistory,
    toggleHistorySelection,
    toggleCurrentHistoryPage,
    clearHistorySelection,
    isHistorySelectable,
    deleteHistoryBatch,
    deleteHistory,
    verifyHistorySelection,
    refreshHistoryItem,
    resetRules,
    invalidatePreview,
    stop,
  }
}
