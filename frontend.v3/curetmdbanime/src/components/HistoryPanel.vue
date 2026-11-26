<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, useId, watch, watchEffect } from 'vue'
import {
  errorMessage,
  formatTime,
  resultColor,
  resultLabel,
  results,
  type Api,
  type Group,
  type Result,
  type HistoryStatus,
} from '../api'
import HistoryRecords from './HistoryRecords.vue'
import SettingRow from './SettingRow.vue'
import SurfaceCard from './SurfaceCard.vue'
const props = defineProps<{
  api: Api
  busy: boolean
  maxRecords: number
  integer: (min: number, max: number) => (v: unknown) => string | boolean
  mobile?: boolean
  scrollElement?: HTMLElement | null
  paginationTarget?: HTMLElement | null
}>()
const emit = defineEmits<{ 'update:maxRecords': [value: number] }>()
const query = ref('')
const result = ref<Result>('')
const page = ref(1)
const pageSize = ref(10)
const groups = ref<Group[]>([])
const total = ref(0)
const counts = ref<Record<string, number>>({})
const status = ref<HistoryStatus | null>(null)
const loading = ref(false)
const deleting = ref(false)
const error = ref('')
const notice = ref('')
const noticeWarning = ref(false)
const filterOpen = ref(false)
const filterActivator = ref<HTMLElement | null>(null)
const resultTarget = ref<HTMLElement | null>(null)
const countsTarget = ref<HTMLElement | null>(null)
const sizeTarget = ref<HTMLElement | null>(null)
const sheetTarget = ref<HTMLElement | null>(null)
const selected = ref('')
const cleanupScope = ref('expired')
const pendingCleanup = ref<any>(null)
const records = ref<HTMLElement | null>(null)
const historyList = ref<HTMLElement | null>(null)
const historyBody = ref<HTMLElement | null>(null)
const frozenHeader = ref<HTMLElement | null>(null)
const toolbarStuck = ref(false)
const detailsId = `ct-history-details-${useId()}`
const resultCategories = results.slice(1)
const detailsTarget = (mediaKey: string) => `${detailsId}-${encodeURIComponent(mediaKey)}`
const groupResults = (group: Group) => resultCategories.filter(item => group.counts[item.value] > 0)
const confirm = inject<((options: any) => Promise<boolean>) | null>('moviepilot:confirm', null)
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
const cleanupDescription = (body: any) =>
  body.scope === 'all'
    ? '将删除全部资源及其全部分类和累计处理次数，无法恢复。'
    : body.scope === 'filtered'
      ? `将清理当前筛选的所有页（${body.query || '全部资源'} / ${resultLabel(body.result)}）：${body.result ? '仅删除选中分类及该分类累计处理次数，保留其他分类；分类全空才移除资源' : '删除匹配资源及其全部分类和累计处理次数'}，无法恢复。`
      : '将删除最后一次剩余分类处理后超过 21 天的资源及其分类和累计处理次数。'
let requestId = 0
let debounce: ReturnType<typeof setTimeout> | undefined
const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const rangeStart = computed(() => (total.value ? (page.value - 1) * pageSize.value + 1 : 0))
const rangeEnd = computed(() => Math.min(page.value * pageSize.value, total.value))
watchEffect(
  onCleanup => {
    const scroller = props.mobile ? props.scrollElement : records.value
    const list = historyList.value
    const toolbar = frozenHeader.value
    const body = historyBody.value
    toolbarStuck.value = false
    if (!scroller || !list || !toolbar || !body) return
    const previousPadding = scroller.style.scrollPaddingTop
    const previousClip = body.style.getPropertyValue('--ct-history-body-clip')
    const previousClipPriority = body.style.getPropertyPriority('--ct-history-body-clip')
    const restoreClip = () => {
      if (previousClip) body.style.setProperty('--ct-history-body-clip', previousClip, previousClipPriority)
      else body.style.removeProperty('--ct-history-body-clip')
    }
    const updateToolbar = () => {
      const stuck =
        list.getBoundingClientRect().top + list.clientTop <=
        scroller.getBoundingClientRect().top + scroller.clientTop
      toolbarStuck.value = stuck
      scroller.style.scrollPaddingTop = `${toolbar.offsetHeight + 12}px`
      // 只裁剪滚到工具栏背后的正文，防止文字和背景从透明圆角处透出。
      const hiddenHeight = toolbar.getBoundingClientRect().bottom - body.getBoundingClientRect().top
      if (stuck && hiddenHeight > 0)
        body.style.setProperty('--ct-history-body-clip', `inset(${hiddenHeight}px -100vmax -100vmax)`)
      else restoreClip()
    }
    updateToolbar()
    scroller.addEventListener('scroll', updateToolbar, { passive: true })
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateToolbar) : null
    observer?.observe(list)
    observer?.observe(toolbar)
    observer?.observe(scroller)
    observer?.observe(body)
    onCleanup(() => {
      scroller.removeEventListener('scroll', updateToolbar)
      observer?.disconnect()
      scroller.style.scrollPaddingTop = previousPadding
      restoreClip()
    })
  },
  { flush: 'post' },
)
function scrollToResults() {
  const scroller = props.mobile ? props.scrollElement : records.value
  const list = historyList.value
  if (scroller && list)
    scroller.scrollTop = Math.max(
      0,
      scroller.scrollTop + list.getBoundingClientRect().top + list.clientTop -
        scroller.getBoundingClientRect().top - scroller.clientTop,
    )
}
async function load() {
  const id = ++requestId
  loading.value = true
  error.value = ''
  try {
    const data = await props.api.get('history', {
      page: page.value,
      page_size: pageSize.value,
      query: query.value,
      result: result.value,
    })
    if (id !== requestId) return
    total.value = data.total
    groups.value = data.items
    counts.value = data.counts
    status.value = data.status
    if (page.value > pages.value) page.value = pages.value
  } catch (err) {
    if (id === requestId) error.value = errorMessage(err)
  } finally {
    if (id === requestId) loading.value = false
  }
}
function resetPage() {
  selected.value = ''
  if (page.value !== 1) page.value = 1
  else {
    scrollToResults()
    void load()
  }
}
watch(
  query,
  () => {
    requestId++
    selected.value = ''
    clearTimeout(debounce)
    debounce = setTimeout(resetPage, 300)
  },
  { flush: 'sync' },
)
watch(
  [result, pageSize],
  () => {
    requestId++
    clearTimeout(debounce)
    resetPage()
  },
  { flush: 'sync' },
)
watch(page, () => {
  selected.value = ''
  scrollToResults()
  void load()
})
async function cleanup() {
  const body = { scope: cleanupScope.value, query: query.value, result: result.value, confirmed: true }
  if (confirm) {
    if (
      !(await confirm({
        title: '清理修正历史',
        content: cleanupDescription(body),
        type: 'warn',
      }))
    )
      return
    await remove(body)
  } else pendingCleanup.value = body
}
async function remove(body: any) {
  pendingCleanup.value = null
  deleting.value = true
  error.value = ''
  notice.value = ''
  try {
    const data = await props.api.post('history/cleanup', body)
    noticeWarning.value = !data.success
    notice.value = `${data.success ? '清理完成' : '部分清理失败'}：影响 ${data.deleted} 个资源，删除 ${data.deleted_results} 个分类，移除 ${data.deleted_calls} 次累计处理。${data.success ? '' : `失败媒体 ${data.failed_media.length} 部，请重试未完成范围。`}`
    selected.value = ''
    await load()
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    deleting.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => {
  requestId++
  clearTimeout(debounce)
})
</script>
<template>
  <section class="ct-history-panel" aria-label="修正历史">
    <div ref="records" class="ct-history-records" tabindex="0" role="region" aria-label="修正历史记录列表">
      <div class="ct-history-intro">
        <slot name="intro" />
        <SurfaceCard
          class="ct-history-retention"
          kicker="RETENTION"
          title="历史保留策略"
          icon="mdi-history"
          body-class="ct-history-retention-body"
        >
          <div class="ct-settings-list">
              <SettingRow
                title="历史资源上限"
                description="标题与副标题相同视为一个资源。"
                last
              >
                <v-text-field
                  :model-value="maxRecords"
                  @update:model-value="emit('update:maxRecords', Number($event))"
                  type="number"
                  hide-details="auto"
                  density="compact"
                  variant="outlined"
                  aria-label="历史资源上限"
                  :rules="[integer(100, 100000)]"
                />
              </SettingRow>
            </div>
            <div class="ct-cleanup">
              <v-select
                v-model="cleanupScope"
                :items="[
                  { title: '清理过期', value: 'expired' },
                  { title: '清理当前筛选的所有页', value: 'filtered' },
                  { title: '清空全部历史', value: 'all' },
                ]"
                label="手动清理范围"
                aria-label="手动清理范围"
                variant="outlined"
                hide-details
                density="compact"
                :disabled="busy || deleting"
              />
              <v-btn
                color="warning"
                variant="tonal"
                size="small"
                prepend-icon="mdi-delete-sweep-outline"
                :loading="deleting"
                :disabled="busy || deleting || loading"
                @click="cleanup"
                >清理历史</v-btn
              >
            </div>
            <v-alert v-if="pendingCleanup" type="warning" variant="tonal" density="compact" class="mt-3"
              >{{ cleanupDescription(pendingCleanup) }}
              <div class="mt-2">
                <v-btn
                  color="warning"
                  size="small"
                  :disabled="busy || deleting || loading"
                  @click="remove(pendingCleanup)"
                  >确认清理</v-btn
                ><v-btn variant="text" size="small" @click="pendingCleanup = null">取消</v-btn>
              </div></v-alert
            >
        </SurfaceCard>
      </div>
      <div ref="historyList" class="ct-card ct-history-list ct-surface ct-surface--composite app-surface app-surface-static">
        <div
          ref="frozenHeader"
          class="ct-history-frozen-header ct-surface-header"
          :class="{ 'ct-history-frozen-header--stuck': toolbarStuck }"
        >
          <div class="ct-history-filters">
            <v-text-field
              class="ct-history-search"
              v-model="query"
              label="搜索媒体名 / 原标题 / 副标题"
              aria-label="搜索媒体名 / 原标题 / 副标题"
              :mobile-layout="false"
              prepend-inner-icon="mdi-magnify"
              clearable
              @click:clear="query = ''"
              variant="outlined"
              hide-details
              density="compact"
              :disabled="busy || deleting"
            />
            <div ref="resultTarget" class="ct-history-result-target" />
            <Teleport :to="mobile && sheetTarget ? sheetTarget : resultTarget || 'body'" :disabled="!resultTarget">
              <v-select
                class="ct-history-result-filter"
                v-model="result"
                :items="results"
                label="结果分类"
                aria-label="结果分类"
                :mobile-layout="false"
                variant="outlined"
                hide-details
                density="compact"
                :disabled="busy || deleting"
              />
            </Teleport>
            <button
              ref="filterActivator"
              type="button"
              class="ct-history-filter-button ct-filter-button"
              :disabled="busy || deleting"
              aria-label="历史筛选"
              @click="filterOpen = true"
            >
              筛选<span v-if="result">·1</span>
            </button>
            <v-btn
              icon="mdi-refresh"
              variant="text"
              size="small"
              aria-label="刷新修正历史"
              title="刷新修正历史"
              :loading="loading"
              :disabled="busy || deleting"
              @click="load"
            />
          </div>
          <div ref="countsTarget" />
          <Teleport :to="mobile && sheetTarget ? sheetTarget : countsTarget || 'body'" :disabled="!countsTarget">
            <div class="ct-history-counts" aria-label="修正结果统计">
              <v-chip
                v-for="item in resultCategories"
                :key="item.value"
                :color="resultColor(item.value)"
                size="x-small"
                variant="tonal"
                >{{ item.title }} {{ counts[item.value] || 0 }}</v-chip
              >
            </div>
          </Teleport>
          <v-progress-linear
            v-if="loading"
            class="ct-history-progress"
            indeterminate
            color="primary"
            aria-label="正在读取修正历史"
          />
        </div>
        <div ref="historyBody" class="ct-card-body ct-history-list-body ct-surface-body">
          <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-3"
            >{{ error
            }}<v-btn variant="text" size="small" :disabled="busy || deleting || loading" @click="load">重试</v-btn></v-alert
          >
          <v-alert
            v-if="notice"
            :type="noticeWarning ? 'warning' : 'success'"
            variant="tonal"
            density="compact"
            class="mb-3"
            >{{ notice }}</v-alert
          >
          <v-alert
            v-if="status && (status.pending_objects || !status.scheduler_available || status.last_error || status.rejected)"
            type="warning"
            variant="tonal"
            density="compact"
            class="mb-3"
          >
            <span v-if="status.pending_objects">{{ status.pending_objects }} 个资源尚未落库。 </span>
            <span v-if="!status.scheduler_available">批量调度不可用。 </span>
            <span v-if="status.last_error">最近刷写错误：{{ status.last_error }}。 </span>
            <span v-if="status.rejected">已拒收 {{ status.rejected }} 次历史调用，不影响主修正。 </span>
          </v-alert>
          <p v-if="!loading && !error && !groups.length" class="pa-5 text-center text-medium-emphasis">
            暂无符合条件的修正记录
          </p>
          <article
            v-for="group in groups"
            :key="group.media_key"
            class="ct-card ct-history-card ct-surface ct-surface--nested app-surface app-surface-static"
            :data-media-key="group.media_key"
          >
            <div class="ct-card-body">
              <div class="ct-history-title-line">
                <h3>{{ group.title }}</h3>
                <span v-if="group.tmdb_id" class="ct-history-tmdb">TMDB {{ group.tmdb_id }}</span>
                <v-chip class="ct-history-total" size="x-small" variant="tonal"
                  >共 {{ group.count }} 次</v-chip
                >
                <v-chip
                  v-for="item in groupResults(group)"
                  :key="item.value"
                  :color="resultColor(item.value)"
                  size="x-small"
                  variant="tonal"
                  >{{ item.title }} {{ group.counts[item.value] }}</v-chip
                >
              </div>
              <p class="ct-history-latest">
                <v-icon icon="mdi-clock-outline" size="14" aria-hidden="true" /><span
                  >{{ result ? '分类摘要时间' : '最近处理' }}
                  {{ formatTime(result ? group.last.created_at : group.latest) }}</span
                >
              </p>
              <p class="ct-history-reason">{{ group.last.reason || '无额外原因' }}</p>
            </div>
            <button
              type="button"
              class="ct-history-details-toggle"
              :disabled="busy || deleting"
              :aria-label="`${selected === group.media_key ? '收起' : '展开'} ${group.title} 的资源明细`"
              :title="selected === group.media_key ? '收起资源明细' : '展开资源明细'"
              :aria-expanded="selected === group.media_key"
              :aria-controls="detailsTarget(group.media_key)"
              @click="selected = selected === group.media_key ? '' : group.media_key"
            >
              <v-icon
                :icon="selected === group.media_key ? 'mdi-chevron-up' : 'mdi-chevron-down'"
                size="16"
                aria-hidden="true"
              />
            </button>
            <div :id="detailsTarget(group.media_key)" class="ct-history-details" :hidden="selected !== group.media_key">
              <HistoryRecords
                v-if="selected === group.media_key"
                :key="`${group.media_key}:${result}:${query}`"
                :api="api"
                :media-key="group.media_key"
                :result="result"
                :query="query"
              />
            </div>
          </article>
        </div>
      </div>
    </div>
    <Teleport :to="paginationTarget || 'body'" :disabled="!mobile || !paginationTarget">
      <div class="ct-history-pagination" aria-label="修正历史分页">
        <div class="ct-history-page-summary">
          <span>{{ rangeStart }}–{{ rangeEnd }} / {{ total }} 部媒体</span>
          <div ref="sizeTarget" />
          <Teleport :to="mobile && sheetTarget ? sheetTarget : sizeTarget || 'body'" :disabled="!sizeTarget">
            <v-select
              class="ct-history-page-size"
              v-model="pageSize"
              :items="[10, 20, 50]"
              aria-label="每页媒体数量"
              variant="outlined"
              density="compact"
              hide-details
              :disabled="busy || deleting || loading"
            />
          </Teleport>
        </div>
        <v-pagination
          class="ct-history-page-buttons"
          v-model="page"
          :length="pages"
          :total-visible="mobile ? 3 : 4"
          density="compact"
          aria-label="媒体历史分页"
          :disabled="busy || deleting || loading"
        />
      </div>
    </Teleport>
    <v-bottom-sheet v-model="filterOpen" :disabled="!mobile" :persistent="busy || deleting" eager>
      <SurfaceCard vuetify class="ct-filter-sheet" aria-label="历史筛选" title="历史筛选">
        <div ref="sheetTarget" class="ct-filter-sheet-controls" />
        <template #actions>
          <v-btn :disabled="busy || deleting || loading" @click="result = ''">重置结果筛选</v-btn>
          <v-spacer /><v-btn @click="filterOpen = false">完成</v-btn>
        </template>
      </SurfaceCard>
    </v-bottom-sheet>
  </section>
</template>
