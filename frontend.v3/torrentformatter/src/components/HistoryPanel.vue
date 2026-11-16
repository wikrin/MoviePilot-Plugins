<script setup>
import { computed, ref, watchEffect } from 'vue'
const props = defineProps({
  mobile: Boolean,
  scrollElement: { type: Object, default: null },
  paginationTarget: { type: Object, default: null },
  items: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  loading: Boolean,
  busy: Boolean,
  error: { type: String, default: '' },
  query: { type: String, default: '' },
  resultFilter: { type: String, default: 'all' },
  presenceFilter: { type: String, default: 'all' },
  selectedItems: { type: Array, default: () => [] },
  selectionReady: Boolean,
  selectionError: { type: String, default: '' },
  allCurrentSelected: Boolean,
  someCurrentSelected: Boolean,
  selectableCount: { type: Number, default: 0 },
  lookupLoading: Boolean,
  selectionLimit: { type: Number, default: 100 },
})
const emit = defineEmits([
  'update:page',
  'update:query',
  'update:resultFilter',
  'update:presenceFilter',
  'refresh',
  'preview',
  'request-delete',
  'toggle-selection',
  'toggle-page-selection',
  'request-delete-selected',
  'verify-selected',
])
const pages = computed(() => Math.max(1, Math.ceil(props.total / 20)))
const filtered = computed(() => Boolean(props.query || props.resultFilter !== 'all' || props.presenceFilter !== 'all'))
const filterSheet = ref(false)
const filterButton = ref(null)
const historyIntro = ref(null)
const records = ref(null)
const frozenHeader = ref(null)
const toolbarStuck = ref(false)
watchEffect(onCleanup => {
  const scroller = props.mobile ? props.scrollElement : records.value
  const intro = historyIntro.value
  const toolbar = frozenHeader.value
  if (!scroller || !intro || !toolbar) return
  const previousPadding = scroller.style.scrollPaddingTop
  function updateToolbar() {
    toolbarStuck.value = intro.getBoundingClientRect().bottom <= scroller.getBoundingClientRect().top
    scroller.style.scrollPaddingTop = `${toolbar.offsetHeight + 12}px`
  }
  updateToolbar()
  scroller.addEventListener('scroll', updateToolbar, { passive: true })
  const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateToolbar) : null
  observer?.observe(intro)
  observer?.observe(toolbar)
  observer?.observe(scroller)
  onCleanup(() => {
    scroller.removeEventListener('scroll', updateToolbar)
    observer?.disconnect()
    scroller.style.scrollPaddingTop = previousPadding
  })
}, { flush: 'post' })
const activeFilters = computed(() => Number(props.presenceFilter !== 'all') + Number(props.resultFilter !== 'all'))
const labels = { success: '已处理', partial: '部分执行', verifying: '待确认', failed: '有失败记录', legacy: '旧版备份' }
const presenceLabels = {
  available: '仍在原下载器',
  transferred: '已转种',
  missing: '种子已不存在',
  unavailable: '下载器不可用',
  conflict: '转种/备份异常',
  busy: '种子忙碌中',
  backup_missing: '备份缺失',
}
const presenceColors = {
  available: 'success',
  transferred: 'info',
  missing: 'warning',
  unavailable: 'warning',
  conflict: 'error',
  busy: 'info',
  backup_missing: 'error',
}
const presenceOptions = [
  { title: '全部种子状态', value: 'all' },
  { title: '可恢复', value: 'restorable' },
  ...Object.entries(presenceLabels).map(([value, title]) => ({ value, title })),
]
const resultOptions = [
  { title: '全部处理结果', value: 'all' },
  ...Object.entries(labels).map(([value, title]) => ({ value, title })),
]
const selectedMap = computed(() => new Map(props.selectedItems.map(item => [item.id || item.history_id, item])))
function date(value) {
  return value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString() : '旧版备份'
}
function checkedAt(value) {
  return value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString() : '时间未知'
}
function selectable(item) {
  return Boolean(
    !item.history_stale && item.presence?.can_delete && item.presence?.delete_signature && item.presence?.state !== 'busy',
  )
}
function resetFilters() {
  emit('update:query', '')
  emit('update:resultFilter', 'all')
  emit('update:presenceFilter', 'all')
}
</script>

<template>
  <section class="tf-history-panel" aria-label="处理历史">
    <div ref="records" class="tf-history-records" tabindex="0" role="region" aria-label="处理历史记录列表">
    <div ref="historyIntro" class="tf-history-intro">
      <div class="tf-heading">
        <div>
          <span class="tf-eyebrow">05 / HISTORY & RESTORE</span>
          <h2>处理历史与恢复</h2>
          <p>按名称、Hash、下载器或路径搜索，核验原位置和已知转种目标。</p>
        </div>
      </div>
      <v-alert variant="tonal" type="warning" density="compact" class="tf-inline-alert tf-history-warning"
        >备份默认保留到完整恢复。手动删除只清理插件记录，不删种子或文件，但会失去恢复能力。</v-alert>
    </div>
    <div ref="frozenHeader" class="tf-history-frozen-header" :class="{ 'tf-history-frozen-header--stuck': toolbarStuck }">
    <div class="tf-card tf-history-filters">
      <v-text-field
        :model-value="query"
        label="搜索历史"
        placeholder="名称 / Hash / 下载器 / 路径"
        prepend-inner-icon="mdi-magnify"
        clearable
        maxlength="200"
        density="compact"
        variant="outlined"
        hide-details
        :disabled="busy"
        @update:model-value="emit('update:query', $event || '')"
      />
        <v-select
          class="tf-history-desktop-filter"
          :model-value="presenceFilter"
          :items="presenceOptions"
          label="种子状态"
          aria-label="种子状态"
          density="compact"
          variant="outlined"
          hide-details
          :disabled="busy"
          @update:model-value="emit('update:presenceFilter', $event)"
        /><v-select
          class="tf-history-desktop-filter"
          :model-value="resultFilter"
          :items="resultOptions"
          label="处理结果"
          aria-label="处理结果"
          density="compact"
          variant="outlined"
          hide-details
          :disabled="busy"
          @update:model-value="emit('update:resultFilter', $event)"
        />
      <v-btn ref="filterButton" class="tf-history-mobile-filter" size="small" variant="tonal" :disabled="busy"
        :aria-label="`筛选历史（${activeFilters} 项状态筛选）`" @click="filterSheet = true">
        筛选<span v-if="activeFilters"> · {{ activeFilters }}</span>
      </v-btn>
      <v-btn icon="mdi-refresh" size="small" variant="text" aria-label="刷新历史" title="刷新历史"
        :disabled="busy" :loading="loading" @click="emit('refresh')" />
      <v-btn v-if="filtered" class="tf-history-clear-filter" icon="mdi-filter-remove-outline" size="small"
        variant="text" aria-label="清除筛选" title="清除筛选" :disabled="busy" @click="resetFilters" />
    </div>
    <div class="tf-card tf-history-batchbar" aria-label="历史批量删除工具栏">
      <div class="tf-history-batch-select">
        <v-checkbox-btn
          :model-value="allCurrentSelected"
          :indeterminate="someCurrentSelected"
          :disabled="busy || loading || !selectableCount"
          color="primary"
          aria-label="选择或取消当前页全部可删除记录"
          @update:model-value="emit('toggle-page-selection')"
        />
        <div>
          <strong class="tf-history-desktop-label">{{ allCurrentSelected ? '取消本页可删除项' : '选择本页可删除项' }}</strong>
          <strong class="tf-history-mobile-label" aria-hidden="true">本页</strong>
          <small class="tf-history-desktop-label">仅影响当前页，不会选择全部筛选结果</small>
        </div>
      </div>
      <div class="tf-history-batch-actions">
        <span :aria-label="`已选 ${selectedItems.length} / ${selectionLimit} 条记录`"><strong>{{ selectedItems.length }}</strong><span aria-hidden="true">/{{ selectionLimit }}</span><span class="tf-history-desktop-label"> 条已选</span></span>
        <v-btn
          v-if="selectedItems.length && !selectionReady"
          size="small"
          variant="text"
          class="tf-history-verify-selected"
          aria-label="实时核验所选"
          title="实时核验所选记录"
          :loading="lookupLoading"
          :disabled="busy || loading"
          @click="emit('verify-selected')"
          ><v-icon icon="mdi-shield-refresh-outline" class="tf-history-mobile-label" /><span class="tf-history-desktop-label">实时核验所选</span></v-btn
        ><v-btn
          color="error"
          variant="tonal"
          size="small"
          aria-label="删除所选记录"
          title="删除所选插件备份记录，不删种子或文件"
          :disabled="busy || loading || !selectionReady"
          prepend-icon="mdi-delete-sweep-outline"
          @click="emit('request-delete-selected')"
          ><span class="tf-history-desktop-label">删除所选记录</span><span class="tf-history-mobile-label" aria-hidden="true">删除</span></v-btn
        >
      </div>
    </div>
    <v-progress-linear v-if="loading" indeterminate color="primary" class="tf-history-progress" aria-label="正在核验历史种子位置" />
    </div>
    <v-alert v-if="error" variant="tonal" type="error" density="compact" class="tf-inline-alert"
      >{{ error }}<v-btn variant="text" size="small" :disabled="busy || loading" @click="emit('refresh')">重试</v-btn></v-alert>
    <v-alert v-if="selectionError" variant="tonal" type="warning" density="compact" class="tf-inline-alert">{{ selectionError }}</v-alert>
    <div
      v-for="item in items"
      :key="item.id"
      class="tf-card tf-history-card"
      :class="{ 'tf-history-card--selected': selectedMap.has(item.id) }"
      :data-history-id="item.id"
    >
      <div class="tf-card-body">
        <div class="tf-history-title-row">
          <v-checkbox-btn
            :model-value="selectedMap.has(item.id)"
            :disabled="busy || loading || !selectable(item)"
            color="primary"
            :aria-label="`${selectedMap.has(item.id) ? '取消选择' : '选择'}历史记录 ${item.name}`"
            @update:model-value="emit('toggle-selection', item, $event)"
          />
          <div class="tf-history-title">
            <h3>{{ item.name }}</h3>
            <v-chip :color="item.status === 'success' ? 'success' : 'warning'" size="x-small" variant="tonal">{{
              labels[item.status] || item.status
            }}</v-chip>
          </div>
        </div>
        <div class="tf-meta-line mt-2">
          <span>原下载器：{{ item.downloader }}</span
          ><span>{{ item.files_count }} 个文件</span><span>{{ date(item.created_at) }}</span>
        </div>
        <div v-if="item.presence" class="tf-history-presence" :class="`tf-history-presence--${item.presence.state}`">
          <v-chip :color="presenceColors[item.presence.state]" size="x-small" variant="tonal">{{
            presenceLabels[item.presence.state]
          }}</v-chip>
          <span class="tf-history-presence-message">{{ item.presence.message }}</span>
          <small v-if="item.presence.target_downloader && item.presence.target_downloader !== item.downloader"
            >已知当前目标：{{ item.presence.target_downloader }} · {{ item.presence.target_hash }}</small
          >
          <small class="tf-history-checked"
            >核验于 {{ checkedAt(item.presence.checked_at) }} · {{ item.presence.cached ? '5 秒缓存' : '实时查询' }}</small
          >
        </div>
        <v-alert
          v-if="
            item.history_stale ||
            (selectedMap.get(item.id)?.verification || 'ready') !== 'ready' ||
            selectedMap.get(item.id)?.selection_message
          "
          variant="tonal"
          type="warning"
          density="compact"
          class="mt-3"
          >{{
            selectedMap.get(item.id)?.selection_message || '关联备份状态已变化，请实时核验后重新确认'
          }}</v-alert
        >
        <div class="tf-history-path">
          <span class="tf-summary-label">原始保存路径</span
          ><code class="tf-code-path">{{ item.save_path || '备份缺失' }}</code
          ><template v-if="item.target_save_path && item.target_save_path !== item.save_path"
            ><span class="tf-summary-label mt-2">计划处理路径</span
            ><code class="tf-code-path">{{ item.target_save_path }}</code></template
          >
        </div>
        <div class="tf-action-row">
          <span class="tf-mono tf-muted">{{ item.torrent_hash }}</span>
          <div class="tf-history-actions">
            <v-btn
              color="primary"
              variant="tonal"
              size="small"
              :disabled="busy || loading || (!item.restorable && !item.history_stale)"
              prepend-icon="mdi-backup-restore"
              @click="emit('preview', item)"
              >{{ item.history_stale ? '实时核验后预览' : item.restorable ? '查看恢复预览' : '暂不能恢复' }}</v-btn
            ><v-btn
              color="error"
              variant="text"
              size="small"
              :disabled="busy || loading || !selectable(item)"
              prepend-icon="mdi-delete-outline"
              @click="emit('request-delete', item)"
              >删除记录</v-btn
            >
          </div>
        </div>
      </div>
    </div>
    <div v-if="!items.length && !loading && !error" class="tf-card tf-empty">
      <div class="tf-empty-icon"><v-icon icon="mdi-history" size="28" /></div>
      <h3>{{ filtered ? '没有匹配的历史记录' : '没有处理历史' }}</h3>
      <p>
        {{
          filtered
            ? '尝试修改关键词，或清除种子状态与处理结果筛选。'
            : '确认处理后，原始快照与操作日志会显示在这里。单纯预览不会生成历史。'
        }}
      </p>
      <v-btn v-if="filtered" variant="tonal" color="primary" size="small" class="mt-4" :disabled="busy" @click="resetFilters"
        >清除筛选</v-btn
      >
    </div>
    </div>
    <Teleport :to="paginationTarget || 'body'" :disabled="!mobile || !paginationTarget">
    <div class="tf-pagination tf-history-pagination" aria-label="处理历史分页">
      <span>{{ total }} 条匹配记录 · {{ page }} / {{ pages }} 页</span>
      <div>
        <v-btn
          size="small"
          variant="text"
          :disabled="busy || loading || page <= 1"
          @click="emit('update:page', page - 1)"
          >上一页</v-btn
        ><v-btn
          size="small"
          variant="text"
          :disabled="busy || loading || page >= pages"
          @click="emit('update:page', page + 1)"
          >下一页</v-btn
        >
      </div>
    </div>
    </Teleport>
    <v-bottom-sheet v-model="filterSheet" :activator="filterButton" :open-on-click="false"
      content-class="tf-history-filter-sheet" :persistent="busy">
      <v-card class="tf-shell tf-history-filter-modal" role="dialog" aria-label="历史筛选">
        <v-card-title tag="h3">历史筛选</v-card-title>
        <v-card-text class="tf-history-sheet-fields">
          <v-select :model-value="presenceFilter" :items="presenceOptions" label="种子状态" aria-label="种子状态"
            density="compact" variant="outlined" hide-details :disabled="busy"
            @update:model-value="emit('update:presenceFilter', $event)" />
          <v-select :model-value="resultFilter" :items="resultOptions" label="处理结果" aria-label="处理结果"
            density="compact" variant="outlined" hide-details :disabled="busy"
            @update:model-value="emit('update:resultFilter', $event)" />
        </v-card-text>
        <v-card-actions>
          <v-btn variant="text" :disabled="busy || !filtered" @click="resetFilters">清除筛选</v-btn>
          <v-spacer />
          <v-btn color="primary" variant="tonal" @click="filterSheet = false">完成</v-btn>
        </v-card-actions>
      </v-card>
    </v-bottom-sheet>
  </section>
</template>
