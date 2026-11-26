<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  errorMessage,
  formatTime,
  isLogEntry,
  isLogId,
  isLogPage,
  isLogRetention,
  logLevels,
  type Api,
  type LogEntry,
  type LogPage,
  type Status,
} from '../api'
import { SseParser, type SseEvent } from '../sse'
import SurfaceCard from './SurfaceCard.vue'

const props = defineProps<{ api: Api; mobile?: boolean }>()
const filterOpen = ref(false)
const filterActivator = ref<HTMLElement | null>(null)
const levelTarget = ref<HTMLElement | null>(null)
const autoTarget = ref<HTMLElement | null>(null)
const actionsTarget = ref<HTMLElement | null>(null)
const helpTarget = ref<HTMLElement | null>(null)
const sheetTarget = ref<HTMLElement | null>(null)
watch(
  () => props.mobile,
  mobile => {
    if (!mobile) filterOpen.value = false
  },
)
watch(filterOpen, async open => {
  if (!open && props.mobile) {
    await nextTick()
    filterActivator.value?.focus()
  }
})
const items = ref<LogEntry[]>([])
const level = ref('info')
const query = ref('')
const auto = ref(true)
// 接收与滚动跟随独立：上翻只暂停自动滚底，仍接收日志并累计未读条数。
const followTail = ref(true)
const pendingLogCount = ref(0)
const history = ref(false)
const loading = ref(false)
const connected = ref(false)
const error = ref('')
const notice = ref('')
const liveOnly = ref(false)
const streamSupported = ref<boolean | null>(null)
const historySupported = ref(false)
const hasOlder = ref(false)
const nextBefore = ref<number | null>(null)
const logContainer = ref<HTMLElement | null>(null)
let liveAfter: number | undefined
let snapshotReady = false
let disposed = false
let requestVersion = 0
let streamVersion = 0
let retries = 0
let controller: AbortController | undefined
let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
let retryTimer: ReturnType<typeof setTimeout> | undefined
let idleTimer: ReturnType<typeof setTimeout> | undefined
let filterTimer: ReturnType<typeof setTimeout> | undefined
let pendingLiveAnchor: { anchor: ReturnType<typeof visibleAnchor>; scrollTop: number } | undefined
let readingAnchor: ReturnType<typeof visibleAnchor>
let viewportSize = { width: 0, height: 0 }
let resizeObserver: ResizeObserver | undefined
const liveOnlyNotice =
  '持久化日志或补发暂不可用：仅接收后续实时输出，断连期间的日志无法补回；修复存储并重启后可刷新最新重试。'
const filters = () => ({ limit: 200, level: level.value.toLowerCase(), keyword: (query.value || '').trim() })
const statusCode = (err: any): number | undefined => err?.response?.status ?? err?.status
const retryable = (err: unknown) => ![400, 401, 403, 422].includes(statusCode(err) || 0)
const currentRequest = (version: number) => !disposed && requestVersion === version
const currentStream = (version: number) => !disposed && streamVersion === version
const canReceive = () =>
  !disposed &&
  auto.value &&
  !history.value &&
  !document.hidden &&
  !filterTimer &&
  !loading.value &&
  streamSupported.value !== false
const canLoadOlder = computed(
  () => !loading.value && !liveOnly.value && historySupported.value && hasOlder.value && nextBefore.value !== null,
)
const emptyMessage = computed(() => {
  if (loading.value) return '正在读取日志…'
  if (error.value) return '日志读取暂不可用，请查看上方原因'
  if (connected.value) return liveOnly.value ? '尚未收到符合条件的实时日志' : '暂无符合条件的日志'
  return auto.value ? '正在连接实时日志…' : '已暂停接收日志'
})

function isNearBottom() {
  const container = logContainer.value
  return !container || container.scrollHeight - container.clientHeight - container.scrollTop <= 32
}
function scrollToBottom() {
  const container = logContainer.value
  if (container) container.scrollTop = container.scrollHeight
}
function handleScroll() {
  const container = logContainer.value
  // 尺寸变化会先触发浏览器 scroll，不能覆盖旧尺寸下的阅读锚点。
  if (container && viewportSize.width && container.clientWidth !== viewportSize.width) return
  readingAnchor = visibleAnchor()
  if (history.value) return
  followTail.value = isNearBottom()
  if (followTail.value) pendingLogCount.value = 0
}
function resetFollow(follow = true) {
  followTail.value = follow
  pendingLogCount.value = 0
  pendingLiveAnchor = undefined
  readingAnchor = undefined
}
function jumpToTail() {
  resetFollow()
  scrollToBottom()
}

function closeStream() {
  ++streamVersion
  clearTimeout(retryTimer)
  retryTimer = undefined
  clearTimeout(idleTimer)
  idleTimer = undefined
  controller?.abort()
  controller = undefined
  const previous = reader
  reader = undefined
  if (previous) void previous.cancel().catch(() => {})
  connected.value = false
}
function scheduleRetry(version: number) {
  if (!currentStream(version) || !canReceive()) return
  const delay = Math.min(1000 * 2 ** Math.min(retries++, 5), 30000)
  retryTimer = setTimeout(() => {
    retryTimer = undefined
    if (!currentStream(version) || !canReceive()) return
    if (snapshotReady) void connectStream()
    else void loadLatest()
  }, delay)
}
function setLiveOnly() {
  liveOnly.value = true
  liveAfter = undefined
  nextBefore.value = null
  hasOlder.value = false
  historySupported.value = false
  notice.value = liveOnlyNotice
}
function applySnapshot(page: LogPage) {
  items.value = page.items.slice(-600)
  liveAfter = page.next_after
  historySupported.value = page.next_before !== undefined && page.has_older !== undefined
  nextBefore.value = page.next_before || null
  hasOlder.value = page.has_older === true
  if (!historySupported.value) notice.value = '服务尚未提供反向分页，加载更早日志需要更新可执行文件。'
  if (page.gap)
    notice.value =
      page.reset === 'cursor_ahead'
        ? '服务日志 ID 已重置，已切换到当前保留记录。'
        : '部分旧日志已被轮转删除，只能查看仍保留的记录。'
}
async function loadLatest(message = '') {
  if (disposed) return
  closeStream()
  resetFollow()
  const version = ++requestVersion
  loading.value = true
  history.value = false
  error.value = ''
  notice.value = message
  snapshotReady = false
  liveOnly.value = false
  streamSupported.value = null
  let failed = false
  try {
    try {
      const page = await props.api.get('logs', filters())
      if (!currentRequest(version)) return
      if (!isLogPage(page)) throw new Error('日志分页响应无效，请更新服务后重试')
      applySnapshot(page)
    } catch (err) {
      if (!currentRequest(version)) return
      if (statusCode(err) !== 503) throw err
      setLiveOnly()
    }
    const status: Status = await props.api.get('status')
    if (!currentRequest(version)) return
    streamSupported.value = status.log_stream_available ?? status.health?.capabilities?.log_stream === true
    snapshotReady = true
    auto.value = true
    retries = 0
    if (!streamSupported.value)
      error.value = '当前服务不支持实时日志流，请部署包含日志管理接口的新构建；已有查询结果仍可查看。'
  } catch (err) {
    if (currentRequest(version)) {
      error.value = errorMessage(err)
      failed = retryable(err)
    }
  } finally {
    if (currentRequest(version)) {
      loading.value = false
      if (snapshotReady && canReceive()) void connectStream()
      else if (failed) scheduleRetry(streamVersion)
    }
  }
}
function appendLive(entry: LogEntry) {
  if (items.value.some(item => item.id === entry.id)) return
  if (!followTail.value) pendingLogCount.value++
  // 入口已排除重复 ID，只需按序合并，避免为每条实时日志重复构建去重 Map。
  const merged = [...items.value, entry].sort((a, b) => a.id - b.id)
  const evicted = merged.length > 600
  if (evicted && !followTail.value && !pendingLiveAnchor && logContainer.value) {
    pendingLiveAnchor = { anchor: visibleAnchor(), scrollTop: logContainer.value.scrollTop }
  }
  items.value = merged.slice(-600)
  if (evicted && historySupported.value) {
    nextBefore.value = items.value[0].id
    hasOlder.value = true // 被挤出显示窗口的匹配记录可通过 before 重新查询。
  }
  if (nextBefore.value === null && historySupported.value) nextBefore.value = items.value[0]?.id || null
}
function applyEvent(event: SseEvent) {
  if (!['log', 'cursor', 'reset', 'error'].includes(event.event)) return
  let data: any
  try {
    data = JSON.parse(event.data)
  } catch {
    throw new Error('日志流 JSON 无效，请更新服务后重试')
  }
  if (!data || typeof data !== 'object') throw new Error('日志流事件无效')
  if (event.event === 'error') throw new Error(typeof data.message === 'string' ? data.message : '服务日志流报告错误')
  const id = event.id !== undefined && /^[0-9]+$/.test(event.id) ? Number(event.id) : NaN
  if (!isLogId(id)) throw new Error('日志流游标无效')
  if (event.event === 'log') {
    if (!isLogEntry(data) || data.id !== id) throw new Error('日志流记录无效')
    if (liveAfter === undefined || id > liveAfter) {
      appendLive(data)
      liveAfter = id
    }
  } else if (event.event === 'cursor') {
    if (data.after !== id) throw new Error('日志流进度无效')
    if (liveOnly.value && liveAfter !== undefined && id < liveAfter) {
      resetFollow()
      items.value = []
    }
    liveAfter = liveOnly.value ? id : Math.max(liveAfter ?? 0, id)
  } else {
    if (!['cursor_ahead', 'history_pruned'].includes(data.reason) || !isLogRetention(data.retention))
      throw new Error('日志流重置事件无效')
    if (data.reason === 'cursor_ahead') resetFollow()
    items.value =
      data.reason === 'cursor_ahead'
        ? []
        : items.value.filter(item => item.id >= data.retention.oldest_id && item.id <= data.retention.newest_id)
    liveAfter = id
    nextBefore.value = items.value[0]?.id || null
    hasOlder.value = false
    notice.value =
      data.reason === 'cursor_ahead'
        ? '服务日志 ID 已重置，旧序列已清空。'
        : '断连期间部分历史已被轮转删除，已从可恢复位置接续。'
  }
  connected.value = true
  error.value = ''
  retries = 0
}
async function connectStream() {
  if (!snapshotReady || !canReceive()) return
  closeStream()
  const version = streamVersion
  const active = new AbortController()
  controller = active
  let localReader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let localIdle: ReturnType<typeof setTimeout> | undefined
  let timedOut = false
  let retry = false
  const armIdle = () => {
    clearTimeout(localIdle)
    localIdle = setTimeout(() => {
      if (!currentStream(version)) return
      timedOut = true
      active.abort()
      if (localReader) void localReader.cancel().catch(() => {})
    }, 45000)
    idleTimer = localIdle
  }
  try {
    const params: Record<string, unknown> = filters()
    if (!liveOnly.value && liveAfter !== undefined) params.after = liveAfter
    const body = await props.api.openLogStream(params, active.signal)
    if (!currentStream(version) || !canReceive()) {
      await body.cancel()
      return
    }
    localReader = body.getReader()
    reader = localReader
    const parser = new SseParser(event => {
      if (currentStream(version) && canReceive()) applyEvent(event)
    })
    armIdle()
    while (currentStream(version) && canReceive()) {
      const result = await localReader.read()
      if (!currentStream(version) || !canReceive()) return
      if (result.done) {
        parser.finish()
        throw new Error(timedOut ? '日志流心跳超时，正在重连。' : '日志连接已中断，正在重连。')
      }
      armIdle()
      parser.feed(result.value)
    }
  } catch (err) {
    if (currentStream(version) && canReceive()) {
      if (statusCode(err) === 503 && !liveOnly.value) setLiveOnly()
      if (statusCode(err) === 502) {
        try {
          const status: Status = await props.api.get('status')
          if (currentStream(version) && status.health && status.health.capabilities?.log_stream !== true)
            streamSupported.value = false
        } catch {
          /* 不把连接失败冒充能力缺失 */
        }
      }
      if (currentStream(version)) {
        error.value = timedOut ? '日志流心跳超时，正在重连。' : errorMessage(err)
        retry = retryable(err)
      }
    }
  } finally {
    clearTimeout(localIdle)
    if (idleTimer === localIdle) idleTimer = undefined
    active.abort()
    if (localReader) {
      try {
        await localReader.cancel()
      } catch {
        /* 原连接可能已关闭 */
      }
      localReader.releaseLock()
    }
    if (controller === active) controller = undefined
    if (reader === localReader) reader = undefined
    if (currentStream(version)) connected.value = false
  }
  if (retry) scheduleRetry(version)
}
// 记录首个可见日志及其像素偏移，翻页、窗口裁剪和响应式换行后恢复阅读位置。
function visibleAnchor() {
  const container = logContainer.value
  if (!container) return undefined
  const bounds = container.getBoundingClientRect()
  for (const row of Array.from(container.querySelectorAll<HTMLElement>('[data-log-id]'))) {
    const rect = row.getBoundingClientRect()
    if (rect.bottom > bounds.top && rect.top < bounds.bottom) {
      // 部分浏览器会量化 scrollTop，固定像素锚点避免连续裁剪累积亚像素漂移。
      return { id: row.dataset.logId!, top: Math.round(rect.top - bounds.top) }
    }
  }
  return undefined
}
function restoreVisibleAnchor(anchor: ReturnType<typeof visibleAnchor>) {
  const container = logContainer.value
  const row = anchor && container?.querySelector<HTMLElement>(`[data-log-id="${anchor.id}"]`)
  if (!container || !anchor || !row) return false
  container.scrollTop = Math.max(
    0,
    container.scrollTop + row.getBoundingClientRect().top - container.getBoundingClientRect().top - anchor.top,
  )
  return true
}
async function loadOlder() {
  if (!canLoadOlder.value || nextBefore.value === null || disposed) return
  const before = nextBefore.value
  closeStream()
  auto.value = false
  history.value = true
  resetFollow(false)
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  const container = logContainer.value
  const anchor = visibleAnchor()
  const oldHeight = container?.scrollHeight || 0
  const oldTop = container?.scrollTop || 0
  try {
    const page = await props.api.get('logs', { ...filters(), before })
    if (!currentRequest(version)) return
    if (
      !isLogPage(page) ||
      page.next_before === undefined ||
      page.has_older === undefined ||
      page.items.some(item => item.id >= before) ||
      (page.items.length > 0 && page.next_before >= before) ||
      (page.has_older && page.next_before === 0)
    )
      throw new Error('更早日志分页响应无效，请更新服务后重试')
    if (page.reset === 'cursor_ahead') {
      await loadLatest('服务日志 ID 已重置，已退出旧历史并读取最新快照。')
      return
    }
    if (page.items.length)
      items.value = [...new Map([...page.items, ...items.value].map(item => [item.id, item])).values()]
        .sort((a, b) => a.id - b.id)
        .slice(0, 600)
    nextBefore.value = page.next_before || null
    hasOlder.value = page.has_older
    if (page.reset === 'history_pruned') notice.value = '请求的旧历史已被轮转删除，无法补回；当前显示内容仍保留。'
    await nextTick()
    if (!currentRequest(version) || !container) return
    if (!restoreVisibleAnchor(anchor)) container.scrollTop = Math.max(0, oldTop + container.scrollHeight - oldHeight)
  } catch (err) {
    if (currentRequest(version)) error.value = errorMessage(err)
  } finally {
    if (currentRequest(version)) loading.value = false
  }
}

function visibilityChanged() {
  if (document.hidden) closeStream()
  else if (canReceive()) {
    if (snapshotReady) void connectStream()
    else void loadLatest()
  }
}
watch(
  items,
  () => {
    const saved = pendingLiveAnchor
    pendingLiveAnchor = undefined
    if (history.value) return
    if (followTail.value) scrollToBottom()
    else if (saved && logContainer.value) {
      if (!restoreVisibleAnchor(saved.anchor)) logContainer.value.scrollTop = saved.scrollTop
    }
    readingAnchor = visibleAnchor()
  },
  { flush: 'post' },
)
watch(auto, enabled => {
  if (!enabled) closeStream()
  else if (!loading.value) {
    if (history.value || !snapshotReady) void loadLatest()
    else void connectStream()
  }
})
watch([level, query], () => {
  closeStream()
  resetFollow()
  ++requestVersion
  loading.value = false
  items.value = []
  snapshotReady = false
  liveAfter = undefined
  nextBefore.value = null
  hasOlder.value = false
  clearTimeout(filterTimer)
  filterTimer = setTimeout(() => {
    filterTimer = undefined
    void loadLatest()
  }, 300)
})
watch(
  () => props.api,
  () => {
    closeStream()
    resetFollow()
    ++requestVersion
    clearTimeout(filterTimer)
    filterTimer = undefined
    items.value = []
    liveAfter = undefined
    snapshotReady = false
    retries = 0
    void loadLatest()
  },
)
onMounted(() => {
  document.addEventListener('visibilitychange', visibilityChanged)
  if (logContainer.value && typeof ResizeObserver !== 'undefined') {
    const container = logContainer.value
    viewportSize = { width: container.clientWidth, height: container.clientHeight }
    resizeObserver = new ResizeObserver(() => {
      if (container.clientWidth !== viewportSize.width || container.clientHeight !== viewportSize.height) {
        if (followTail.value && !history.value) scrollToBottom()
        else restoreVisibleAnchor(readingAnchor)
        viewportSize = { width: container.clientWidth, height: container.clientHeight }
      }
      readingAnchor = visibleAnchor()
    })
    resizeObserver.observe(container)
  }
  void loadLatest()
})
onBeforeUnmount(() => {
  disposed = true
  ++requestVersion
  closeStream()
  resetFollow()
  clearTimeout(filterTimer)
  document.removeEventListener('visibilitychange', visibilityChanged)
  resizeObserver?.disconnect()
})
</script>
<template>
  <section class="ct-card ct-log-view ct-surface app-surface app-surface-static" aria-label="运行日志">
    <div class="ct-card-header">
      <h3>运行日志</h3>
      <v-icon icon="mdi-text-box-outline" color="primary" />
    </div>
    <div class="ct-card-body ct-log-body">
      <div class="ct-log-toolbar" role="group" aria-label="日志筛选与接收">
        <div ref="levelTarget" />
        <Teleport :to="mobile && sheetTarget ? sheetTarget : levelTarget || 'body'" :disabled="!levelTarget">
          <v-select
            v-model="level"
            class="ct-log-filter-level"
            label="日志级别"
            aria-label="日志级别"
            :mobile-layout="false"
            :items="[{ title: '全部', value: '' }, ...logLevels.map(value => ({ title: value.toUpperCase(), value }))]"
            variant="outlined"
            density="compact"
            hide-details
          />
        </Teleport>
        <v-text-field
          v-model="query"
          class="ct-log-search"
          label="搜索日志"
          aria-label="搜索日志"
          :mobile-layout="false"
          prepend-inner-icon="mdi-magnify"
          clearable
          variant="outlined"
          density="compact"
          hide-details
          @update:model-value="query = $event || ''"
        />
        <button
          ref="filterActivator"
          type="button"
          class="ct-log-filter-button ct-filter-button"
          aria-label="日志筛选"
          @click="filterOpen = true"
        >
          {{ level ? level.toUpperCase() : '全部' }}
        </button>
        <div ref="autoTarget" />
        <Teleport :to="mobile && sheetTarget ? sheetTarget : autoTarget || 'body'" :disabled="!autoTarget">
          <v-switch
            v-model="auto"
            class="ct-log-auto"
            label="自动刷新"
            :mobile-layout="false"
            hide-details
            color="primary"
            density="compact"
          />
        </Teleport>
        <div ref="actionsTarget" />
        <Teleport :to="mobile && sheetTarget ? sheetTarget : actionsTarget || 'body'" :disabled="!actionsTarget">
          <div class="ct-log-actions">
            <v-btn size="small" variant="tonal" :loading="loading && !history" :disabled="loading" @click="loadLatest()"
              >刷新最新</v-btn
            >
            <v-btn
              size="small"
              variant="text"
              :loading="loading && history"
              :disabled="!canLoadOlder"
              @click="loadOlder"
              >加载更早</v-btn
            >
          </div>
        </Teleport>
      </div>
      <div ref="helpTarget" />
      <Teleport :to="mobile && sheetTarget ? sheetTarget : helpTarget || 'body'" :disabled="!helpTarget">
        <p class="ct-log-help">
          仅显示应用日志，与 MoviePilot 插件日志分离；不含完整
          stdout/stderr。自动刷新控制接收，滚动跟随独立。历史模式可返回最新继续接收。
        </p>
      </Teleport>
      <p class="ct-log-status">
        {{
          history
            ? '正在阅读历史记录，实时连接已暂停；达到显示上限时移出较新记录，可返回最新继续接收。'
            : connected
              ? liveOnly
                ? '仅接收后续实时输出，不补发历史。'
                : 'SSE 实时接收中，断线后按事件游标补发。'
              : auto
                ? '实时连接未建立。'
                : '自动接收已暂停，显示内容仍保留。'
        }}
      </p>
      <v-alert v-if="notice" type="warning" variant="tonal" density="compact">{{ notice }}</v-alert>
      <v-alert v-if="error" type="error" variant="tonal" density="compact">{{ error }}</v-alert>
      <div class="ct-log-viewport">
        <div
          ref="logContainer"
          class="ct-logs"
          tabindex="0"
          role="region"
          aria-label="日志内容"
          aria-live="off"
          @scroll.passive="handleScroll"
        >
          <p v-if="!items.length" class="text-medium-emphasis pa-3">{{ emptyMessage }}</p>
          <div v-for="item in items" :key="item.id" :data-log-id="item.id" class="ct-log">
            <time class="ct-log-time" :datetime="item.timestamp">{{ formatTime(item.timestamp) }}</time>
            <span class="ct-log-level" :class="`ct-level-${item.level}`">[{{ item.level.toUpperCase() }}]</span>
            <span class="ct-log-message">{{ item.message }}</span>
          </div>
        </div>
        <div v-if="items.length && !history && !followTail" class="ct-log-latest">
          <v-btn size="small" color="primary" variant="elevated" prepend-icon="mdi-arrow-down" @click="jumpToTail">{{
            pendingLogCount ? `跳到底部（${pendingLogCount} 条新日志）` : '跳到底部'
          }}</v-btn>
        </div>
      </div>
    </div>
    <v-bottom-sheet v-model="filterOpen" :disabled="!mobile" eager>
      <SurfaceCard vuetify class="ct-filter-sheet" aria-label="日志筛选" title="日志筛选">
        <div ref="sheetTarget" class="ct-filter-sheet-controls" />
        <template #actions>
          <v-btn @click="level = 'info'">恢复 INFO</v-btn>
          <v-spacer />
          <v-btn @click="filterOpen = false">完成</v-btn>
        </template>
      </SurfaceCard>
    </v-bottom-sheet>
  </section>
</template>
<style scoped>
.ct-log-view {
  container: ct-logs / inline-size;
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-block-size: 0;
  margin-block-end: 0;
}

.ct-log-body {
  display: flex;
  flex: 1 1 auto;
  min-block-size: 0;
  flex-direction: column;
  gap: 12px;
}

.ct-log-body > :not(.ct-log-viewport) {
  flex: 0 0 auto;
}

.ct-log-help,
.ct-log-status {
  margin: 0;
  color: var(--ct-muted);
  font-size: 11px;
  line-height: 1.6;
}

.ct-log-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-inline-size: 0;
}

.ct-log-filter-level {
  flex: 0 0 120px;
  min-inline-size: 0;
}

.ct-log-search {
  flex: 1 1 160px;
  min-inline-size: 0;
}

.ct-log-auto {
  flex: 0 0 auto;
}

.ct-log-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
}

.ct-log-actions > button {
  min-inline-size: 0;
  padding-inline: 10px;
}

.ct-log-viewport {
  position: relative;
  display: flex;
  min-inline-size: 0;
  min-block-size: 120px;
  flex: 1 1 auto;
}

/* 日志只在视口内滚动；关闭浏览器自动锚定，由可见记录锚点统一控制位置。 */
.ct-logs {
  flex: 1 1 auto;
  min-inline-size: 0;
  min-block-size: 0;
  overflow: auto;
  overflow-anchor: none;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  background: rgba(var(--v-theme-on-surface), 0.025);
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 8px;
}

.ct-logs:focus-visible {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: 2px;
}

.ct-log-latest {
  position: absolute;
  inset-block-end: 12px;
  inset-inline-end: 12px;
  max-inline-size: calc(100% - 24px);
  pointer-events: none;
}

.ct-log-latest > button {
  max-inline-size: 100%;
  pointer-events: auto;
}

.ct-log {
  display: grid;
  grid-template-areas: 'time level message';
  grid-template-columns: 10.5rem 4.25rem minmax(0, 1fr);
  align-items: start;
  gap: 8px;
  padding: 10px;
  border-block-end: 1px solid rgba(var(--v-theme-on-surface), 0.06);
  font:
    12px/1.6 ui-monospace,
    SFMono-Regular,
    Menlo,
    Consolas,
    monospace;
}

.ct-log:nth-child(even) {
  background: rgba(var(--v-theme-on-surface), 0.025);
}

.ct-log-time {
  grid-area: time;
  color: rgb(var(--v-theme-primary));
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.ct-log-level {
  grid-area: level;
  white-space: nowrap;
  font-weight: 700;
}

.ct-log-message {
  grid-area: message;
  min-inline-size: 0;
  color: rgba(var(--v-theme-on-surface), 0.88);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.ct-level-debug {
  color: rgb(var(--v-theme-secondary));
}

.ct-level-info {
  color: rgb(var(--v-theme-success));
}

.ct-level-warn {
  color: rgb(var(--v-theme-warning));
}

.ct-level-error,
.ct-level-fatal {
  color: rgb(var(--v-theme-error));
}

@container ct-workbench (width >=720px) {
  .ct-log-viewport {
    flex: 1 1 0;
    block-size: 240px;
  }
}

@container ct-logs (width < 420px) {
  .ct-log-filter-level {
    flex: 1 1 110px;
  }

  .ct-log-actions {
    margin-inline-start: auto;
  }
}

/* 窄日志区将正文移到第二行，保留时间与级别并允许中文、堆栈和长链接换行。 */
@container ct-logs (width < 560px) {
  .ct-log {
    grid-template-areas: 'time level' 'message message';
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 4px 8px;
  }
}
</style>
