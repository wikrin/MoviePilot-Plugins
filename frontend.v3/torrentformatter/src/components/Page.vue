<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import FormatterShell from './FormatterShell.vue'
import TorrentSelector from './TorrentSelector.vue'
import MediaSelector from './MediaSelector.vue'
import RuleEditor from './RuleEditor.vue'
import TorrentPreview from './TorrentPreview.vue'
import HistoryPanel from './HistoryPanel.vue'
import { useTorrentFormatter } from '../composables/useTorrentFormatter.js'
import { formatSize, shortHash } from '../api/torrentformatter.js'
import { RESULT_COLORS, RESULT_LABELS } from '../utils/torrentTree.js'

const props = defineProps({
  api: { type: Object, required: true },
  initialConfig: { type: Object, default: () => ({}) },
  show_switch: { type: Boolean, default: true },
  plugin_id: { type: String, default: '' },
})
const emit = defineEmits(['close', 'switch', 'save', 'layout'])
const state = reactive(useTorrentFormatter(() => props.api))
const section = ref('select')
const confirmation = ref(false)
const acknowledged = ref(false)
const confirmedPreview = ref(null)
const deleteConfirmation = ref(false)
const deleteAcknowledged = ref(false)
const historyToDelete = ref([])
const working = computed(() => state.loading.execute || state.loading.historyDelete || state.loading.historyLookup)
const deleteAttempted = computed(() => state.historyDeleteResults.length > 0)
const deleteHasUnknown = computed(() => state.historyDeleteResults.some(item => item.status === 'unknown'))
const canDeleteHistory = computed(
  () =>
    deleteAcknowledged.value &&
    historyToDelete.value.length > 0 &&
    historyToDelete.value.every(
      item => state.isHistorySelectable(item) && (item.verification || 'ready') === 'ready',
    ) &&
    !deleteAttempted.value &&
    !working.value &&
    !state.errors.historyDelete,
)
const snackbar = reactive({ show: false, text: '', color: 'success' })
const sections = computed(() => [
  { id: 'select', title: '选择种子', subtitle: '一个明确的处理目标', badge: state.selected ? '1' : '' },
  { id: 'media', title: '媒体信息', subtitle: '自动识别或手动指定' },
  { id: 'rules', title: '本次规则', subtitle: '不影响全局配置', badge: state.overrideCount || '' },
  { id: 'preview', title: '变更预览', subtitle: '目录树与前后差异' },
  { id: 'history', title: '处理历史', subtitle: '核对备份与恢复', number: '↺', badge: state.history.total || '' },
])
const recovering = computed(() => state.previewMode === 'recover')
const canContinue = computed(() =>
  Boolean(
    state.selected &&
    state.detail?.torrent.hash === state.selected.hash &&
    !state.loading.initial &&
    !state.loading.list &&
    !state.loading.detail &&
    !state.loading.preview &&
    !state.errors.detail &&
    !working.value,
  ),
)
const selectionStatus = computed(() =>
  state.loading.detail
    ? '正在读取种子详情…'
    : state.errors.detail
      ? '详情读取失败，请重试'
      : state.selected ? `已选：${state.selected.name}` : '请选择一个处理目标',
)
const displayedTorrent = computed(
  () => section.value === 'select'
    ? state.detail?.torrent || state.selected
    : state.preview?.before || (recovering.value ? state.recoveryTarget : state.detail?.torrent),
)
const displayedDownloader = computed(
  () => section.value === 'select'
    ? state.selectedDownloader
    : state.preview?.downloader || (recovering.value ? state.recoveryTarget?.downloader : state.selectedDownloader),
)
const displayedMedia = computed(
  () => state.preview?.media || (!recovering.value ? state.manualMedia || state.detail?.media : null),
)
const canGenerate = computed(() =>
  recovering.value
    ? Boolean(state.recoveryTarget && !state.loading.execute && !state.loading.preview)
    : state.canPreview,
)
const confirmReady = computed(
  () =>
    acknowledged.value && state.canExecute && confirmedPreview.value?.preview_token === state.preview?.preview_token,
)
const changedCount = computed(() =>
  state.preview ? state.preview.changes.filter(item => item.status === 'changed').length : 0,
)
const confirmedCount = computed(() =>
  state.result ? state.result.operations.filter(item => item.status === 'confirmed').length : 0,
)
const deleteResultLabels = {
  deleted: '已删除',
  not_found: '已不存在',
  conflict: '记录变化',
  busy: '忙碌未删',
  failed: '删除失败',
  unknown: '结果未知',
  found: '仍然存在',
}
const deleteResultColors = {
  deleted: 'success',
  not_found: 'info',
  conflict: 'warning',
  busy: 'warning',
  failed: 'error',
  unknown: 'warning',
  found: 'info',
}
const deleteResultMap = computed(
  () => new Map(state.historyDeleteResults.map(item => [String(item.history_id), item])),
)

async function continueToMedia() {
  if (!canContinue.value || section.value !== 'select') return
  const identity = state.targetKey
  if (await state.ensureSelectedTargetFresh()) {
    if (section.value === 'select' && identity === state.targetKey && canContinue.value) navigate('media')
  }
}
async function preview() {
  const data = recovering.value
    ? await state.generatePreview('recover', state.recoveryTarget)
    : await state.generatePreview()
  section.value = 'preview'
  return data
}
async function previewHistory(item) {
  if (item.history_stale) {
    const refreshed = await state.refreshHistoryItem(item)
    if (!refreshed) {
      snackbar.text = state.errors.historyLookup || '此备份记录已不存在或暂时无法核验'
      snackbar.color = 'warning'
      snackbar.show = true
      return
    }
    item = refreshed
  }
  section.value = 'preview'
  await state.generatePreview('recover', item)
}
async function recoverResult() {
  if (state.result?.mode === 'recover' && state.recoveryTarget) return previewHistory(state.recoveryTarget)
  if (state.result)
    return previewHistory({
      downloader: state.result.downloader,
      torrent_hash: state.result.torrent_hash,
      name: state.result.actual?.name || state.preview.before.name,
    })
}
function navigate(value) {
  if (working.value) return
  if (recovering.value && ['select', 'media', 'rules'].includes(value)) state.invalidatePreview()
  section.value = value
  if (value === 'history') void state.enterHistory()
  else if (value === 'select') void state.ensureTorrentsFresh()
  else if (['media', 'rules'].includes(value)) void state.ensureSelectedTargetFresh()
}
function askConfirmation() {
  if (!state.canExecute) return
  acknowledged.value = false
  confirmedPreview.value = state.preview
  confirmation.value = true
}
async function execute() {
  if (!confirmReady.value) return
  await state.executePreview()
  confirmation.value = false
  section.value = 'preview'
}
function askHistoryDeletion(item) {
  openHistoryDeletion([item])
}
function askSelectedHistoryDeletion() {
  openHistoryDeletion(state.selectedHistory)
}
function openHistoryDeletion(items) {
  const candidates = items.filter(
    item => state.isHistorySelectable(item) && (item.verification || 'ready') === 'ready',
  )
  if (working.value || !candidates.length || candidates.length > 100) return
  historyToDelete.value = JSON.parse(JSON.stringify(candidates))
  deleteAcknowledged.value = false
  state.errors.historyDelete = ''
  state.errors.historyLookup = ''
  state.historyDeleteResults = []
  deleteConfirmation.value = true
}
async function deleteHistory() {
  if (!canDeleteHistory.value) return
  const response = await state.deleteHistoryBatch(historyToDelete.value)
  deleteAcknowledged.value = false
  if (response) {
    const failures = response.results.filter(item => !['deleted', 'not_found'].includes(item.status)).length
    snackbar.text = response.message || `${response.results.length - failures} 条记录已确认移除`
    snackbar.color = failures ? 'warning' : 'success'
    snackbar.show = true
    if (!failures) deleteConfirmation.value = false
  }
}
async function verifyDeletionOutcome() {
  await state.verifyHistorySelection(historyToDelete.value.map(item => item.id || item.history_id))
  deleteAcknowledged.value = false
}
async function copy(value) {
  try {
    await navigator.clipboard.writeText(value)
    snackbar.text = '路径已复制'
    snackbar.color = 'success'
  } catch {
    snackbar.text = '当前浏览器无法复制，请手动选择路径'
    snackbar.color = 'warning'
  }
  snackbar.show = true
}
function close() {
  state.invalidatePreview()
  state.stop()
  emit('close')
}
function switchConfig() {
  state.invalidatePreview()
  state.stop()
  emit('switch')
}
watch(
  () => [props.api, props.plugin_id],
  () => {
    state.resetWorkspace()
    section.value = 'select'
    confirmation.value = false
    deleteConfirmation.value = false
    historyToDelete.value = []
    void state.initialize()
  },
)
onMounted(() => {
  emit('layout', { maxWidth: '96rem' })
  void state.initialize()
})
</script>

<template>
  <FormatterShell
    :section="section"
    :sections="sections"
    :busy="working"
    :show-switch="show_switch"
    @update:section="navigate"
    @close="close"
    @switch="switchConfig"
    @refresh="state.refresh"
  >
    <template #default="{ mobile, scrollElement, paginationTarget }">
    <v-alert v-if="state.errors.initial" type="error" variant="tonal" density="compact" class="tf-inline-alert"
      >{{ state.errors.initial }}<v-btn size="small" variant="text" @click="state.initialize">重试</v-btn></v-alert
    >
    <v-progress-linear
      v-if="state.loading.initial"
      indeterminate
      color="primary"
      class="mb-4"
      aria-label="正在初始化工作台"
    />
    <template v-if="section === 'select'">
    <TorrentSelector
      :downloaders="state.downloaders"
      :downloader="state.selectedDownloader"
      :query="state.query"
      :items="state.torrents.items"
      :total="state.torrents.total"
      :page="state.page"
      :page-size="state.pageSize"
      :selected-hash="state.selected?.hash || ''"
      :loading="state.loading.list"
      :busy="working"
      :error="state.errors.list"
      @update:downloader="state.selectedDownloader = $event"
      @update:query="state.query = $event"
      @update:page="state.page = $event"
      @update:page-size="state.pageSize = $event"
      @select="state.selectTorrent"
      @refresh="state.loadTorrents"
    />
    <v-alert v-if="state.errors.detail" type="error" variant="tonal" density="compact" class="mt-3">
      <span>{{ state.errors.detail }}</span>
      <v-btn size="small" variant="text" :disabled="working || state.loading.detail" @click="state.loadSelectedDetail">重试详情</v-btn>
    </v-alert>
    <v-alert v-if="state.detail?.processed || state.selected?.processed" type="warning" variant="tonal" density="compact" class="mt-3">
      此种子已有处理记录，需要先恢复。
      <v-btn size="small" variant="text" :disabled="working" @click="navigate('history')">查看处理历史</v-btn>
    </v-alert>
    <div class="tf-action-row tf-selection-actions">
      <span class="tf-selection-status" role="status">{{ selectionStatus }}</span>
      <v-btn v-if="!mobile" color="primary" variant="tonal" :disabled="!canContinue" :loading="state.loading.detail"
        @click="continueToMedia">下一步：媒体信息</v-btn>
    </div>
    </template>
    <template v-else-if="section === 'media' || section === 'rules'">
      <v-alert v-if="state.errors.detail" type="error" variant="tonal" density="compact" class="tf-inline-alert">{{
        state.errors.detail
      }}</v-alert>
      <div v-if="state.loading.detail" class="tf-card tf-empty">
        <v-progress-circular indeterminate color="primary" />
        <p class="mt-3">正在读取选中种子的完整布局…</p>
      </div>
      <div v-else-if="!state.detail" class="tf-card tf-empty">
        <div class="tf-empty-icon"><v-icon icon="mdi-cursor-default-click-outline" size="26" /></div>
        <h3>先选择一个种子</h3>
        <p>本次媒体信息和临时规则只属于这个明确的目标。</p>
        <v-btn color="primary" variant="tonal" class="mt-4" @click="navigate('select')">选择种子</v-btn>
      </div>
      <template v-else>
        <v-alert v-if="state.detail.processed" type="warning" variant="tonal" density="compact" class="tf-inline-alert"
          >此种子已有处理记录，需要先恢复。<v-btn size="small" variant="text" @click="navigate('history')"
            >查看处理历史</v-btn
          ></v-alert
        >
        <MediaSelector
          v-if="section === 'media'"
          v-model="state.manualMedia"
          v-model:season="state.season"
          :api="api"
          :hint="state.detail.media"
          :busy="working"
          :torrent-name="state.detail.torrent.name"
        />
        <template v-else>
          <div class="tf-heading">
            <div>
              <span class="tf-eyebrow">03 / ONE-TIME RULES</span>
              <h2>本次处理规则</h2>
              <p>从已保存配置开始，只覆盖这次请求。自动任务不会使用这些临时改动。</p>
            </div>
            <v-btn
              size="small"
              variant="text"
              color="primary"
              :disabled="!state.overrideCount || state.loading.execute"
              @click="state.resetRules"
              >恢复已保存规则</v-btn
            >
          </div>
          <RuleEditor v-model="state.draftRules" :capabilities="state.detail.capabilities" :disabled="working" />
          <div class="tf-card">
            <div class="tf-card-body">
              <h3>仍然遵守排除与屏蔽规则</h3>
              <p class="mb-0">
                {{
                  state.rulesInfo.exclude_dirs || state.rulesInfo.exclude_tags || state.rulesInfo.exclude_words.length
                    ? '已配置的排除目录、标签及系统整理屏蔽词仍会生效。具体命中原因在预览中解释。'
                    : '不会绕过目录/标签排除或系统整理屏蔽词。模板错误、越界及重名会阻止执行。'
                }}
              </p>
            </div>
          </div>
        </template>
        <div class="tf-action-row">
          <v-btn variant="text" size="small" @click="navigate(section === 'media' ? 'select' : 'media')">上一步</v-btn
          ><v-btn v-if="section === 'media'" color="primary" variant="tonal" @click="navigate('rules')"
            >下一步：本次规则</v-btn
          ><v-btn
            v-else
            color="primary"
            variant="flat"
            :loading="state.loading.preview"
            :disabled="!state.canPreview"
            @click="preview"
            >生成变更预览</v-btn
          >
        </div>
      </template>
    </template>
    <template v-else-if="section === 'preview'">
      <v-alert
        v-if="state.errors.preview || state.errors.execute"
        type="error"
        variant="tonal"
        density="compact"
        class="tf-inline-alert"
        >{{ state.errors.preview || state.errors.execute
        }}<v-btn size="small" variant="text" @click="navigate('history')">检查处理历史</v-btn></v-alert
      >
      <div v-if="state.loading.preview" class="tf-card tf-empty">
        <v-progress-circular indeterminate color="primary" />
        <h3 class="mt-4">正在生成只读预览</h3>
        <p>识别媒体、计算路径与逐文件差异。此过程不会修改下载器或保存处理记录。</p>
      </div>
      <TorrentPreview
        v-else-if="state.preview"
        :plan="state.preview"
        :result="state.result"
        :status-loading="state.loading.status"
        :status-error="state.errors.status"
        @refresh-result="state.refreshResult"
        @copy="copy"
        @recover="recoverResult"
        @history="navigate('history')"
      />
      <div v-else class="tf-card tf-empty">
        <div class="tf-empty-icon"><v-icon icon="mdi-file-tree-outline" size="26" /></div>
        <h3>{{ recovering ? '恢复预览尚未生成' : '准备查看处理差异' }}</h3>
        <p>
          {{
            recovering
              ? '请刷新重试或返回历史检查备份与下载器状态。'
              : '选择种子并确认媒体及本次规则后，生成只读目录树与前后差异。'
          }}
        </p>
        <v-btn color="primary" variant="tonal" class="mt-4" :disabled="!canGenerate" @click="preview">生成预览</v-btn>
      </div>
    </template>
    <HistoryPanel
      v-else
      :mobile="mobile"
      :scroll-element="scrollElement"
      :pagination-target="paginationTarget"
      :items="state.history.items"
      :total="state.history.total"
      :page="state.historyPage"
      :page-size="state.historyPageSize"
      :loading="state.loading.history"
      :busy="working || state.loading.preview"
      :error="state.errors.history"
      :query="state.historyQuery"
      :result-filter="state.historyResultFilter"
      :presence-filter="state.historyPresenceFilter"
      :selected-items="state.selectedHistory"
      :selection-ready="state.selectedHistoryReady"
      :selection-error="state.historySelectionError || state.errors.historyLookup"
      :all-current-selected="state.allCurrentHistorySelected"
      :some-current-selected="state.someCurrentHistorySelected"
      :selectable-count="state.currentHistorySelectable.length"
      :lookup-loading="state.loading.historyLookup"
      :selection-limit="100"
      @update:page="state.historyPage = $event"
      @update:page-size="state.historyPageSize = $event"
      @update:query="state.historyQuery = $event"
      @update:result-filter="state.historyResultFilter = $event"
      @update:presence-filter="state.historyPresenceFilter = $event"
      @refresh="state.refreshHistory"
      @preview="previewHistory"
      @request-delete="askHistoryDeletion"
      @toggle-selection="state.toggleHistorySelection"
      @toggle-page-selection="state.toggleCurrentHistoryPage"
      @request-delete-selected="askSelectedHistoryDeletion"
      @verify-selected="state.verifyHistorySelection"
    />
    </template>
    <template #summary>
      <div v-if="section === 'history'" class="tf-summary">
        <div class="tf-summary-heading">
          <h2>历史核验摘要</h2>
          <span class="tf-source-badge">当前筛选</span>
        </div>
        <div class="tf-stat-row">
          <span>匹配记录</span><strong>{{ state.history.total }} 条</strong>
        </div>
        <div class="tf-stat-row" :class="{ 'tf-stat-row--active': state.selectedHistoryCount }">
          <span>跨页已选择</span><strong>{{ state.selectedHistoryCount }} / 100 条</strong>
        </div>
        <div class="tf-summary-block">
          <span class="tf-summary-label">当前页的位置状态</span>
          <div
            v-for="[value, label] in [
              ['available', '仍在原位置'],
              ['transferred', '已知转种目标'],
              ['missing', '种子已不存在'],
              ['unavailable', '下载器不可用'],
            ]"
            :key="value"
            class="tf-stat-row"
          >
            <span>{{ label }}</span
            ><strong>{{ state.history.items.filter(item => item.presence?.state === value).length }}</strong>
          </div>
        </div>
        <div class="tf-summary-block">
          <span class="tf-summary-label">核验规则</span>
          <p class="tf-summary-note">
            优先追踪已知转种目标。离线或查询失败不代表种子已删除；原记录身份和 Hash 始终保留。
          </p>
        </div>
        <div class="tf-summary-block">
          <span class="tf-summary-label">删除仅清理插件记录</span>
          <p class="tf-summary-note">不删除下载任务或文件，不还原已修改路径。确认删除后会失去此备份的恢复能力。</p>
        </div>
        <v-btn
          block
          color="primary"
          variant="tonal"
          class="mt-4"
          :loading="state.loading.history"
          :disabled="working"
          @click="state.refreshHistory"
          >重新核验历史</v-btn
        >
      </div>
      <div v-else class="tf-summary">
        <div class="tf-summary-heading">
          <h2>{{ section === 'select' ? '当前处理目标' : recovering ? '本次恢复摘要' : '本次影响摘要' }}</h2>
          <span class="tf-source-badge">{{ section === 'select' ? '单种子' : recovering ? '恢复原状' : '单种子' }}</span>
        </div>
        <div v-if="displayedTorrent" class="tf-summary-block">
          <span class="tf-summary-name">{{ displayedTorrent.name }}</span>
          <div class="tf-meta-line mt-2">
            {{ displayedDownloader }} ·
            {{ displayedTorrent.total_size !== undefined ? formatSize(displayedTorrent.total_size) : '等待实时查询' }}
          </div>
          <span class="tf-code-path mt-2">{{ displayedTorrent.save_path || '请查看预览中的实时状态' }}</span
          ><span class="tf-mono tf-muted" :title="displayedTorrent.hash || displayedTorrent.torrent_hash">{{
            shortHash(displayedTorrent.hash || displayedTorrent.torrent_hash)
          }}</span>
          <div class="tf-stat-row">
            <span>种子文件</span
            ><strong>{{ section === 'select' && !state.detail ? '等待读取' : displayedTorrent.files?.length ?? displayedTorrent.files_count ?? '—' }}</strong>
          </div>
        </div>
        <div v-else class="tf-summary-note">从左侧选择一个种子。这里会持续显示明确目标、处理范围及风险。</div>
        <div v-if="section === 'select'" class="tf-summary-block">
          <span class="tf-summary-label">先核对目标，再继续</span>
          <p class="tf-summary-note">请确认名称、下载器与保存路径。下一步核对媒体身份，此处不会修改下载任务或文件。</p>
        </div>
        <template v-else>
        <div v-if="displayedMedia" class="tf-summary-block">
          <span class="tf-summary-label">{{
            state.preview?.media?.source === 'manual' || state.manualMedia ? '本次手动指定媒体' : '已识别媒体'
          }}</span
          ><span class="tf-summary-name">{{ displayedMedia.title }} {{ displayedMedia.year }}</span>
          <div class="tf-meta-line mt-1">
            {{ displayedMedia.media_type === 'tv' ? '电视剧' : '电影' }} · {{ displayedMedia.media_id || '待解析' }}
          </div>
        </div>
        <div v-if="state.preview" class="tf-summary-block">
          <span class="tf-summary-label">计划范围，不等同于已完成</span>
          <div class="tf-stat-row" :class="{ 'tf-stat-row--active': state.preview.counts.save_path }">
            <span>保存目录移动</span><strong>{{ state.preview.counts.save_path }} 项</strong>
          </div>
          <div class="tf-stat-row" :class="{ 'tf-stat-row--active': state.preview.counts.files }">
            <span>文件相对路径改名</span><strong>{{ state.preview.counts.files }} 个</strong>
          </div>
          <div class="tf-stat-row" :class="{ 'tf-stat-row--active': state.preview.counts.name }">
            <span>独立显示名修改</span><strong>{{ state.preview.counts.name }} 项</strong>
          </div>
          <div class="tf-stat-row">
            <span>全路径影响文件</span><strong>{{ state.preview.counts.affected_files }} 个</strong>
          </div>
          <div v-if="state.preview.counts.auto_tmm" class="tf-stat-row">
            <span>自动管理步骤</span><strong>{{ state.preview.counts.auto_tmm }} 项</strong>
          </div>
          <div class="tf-stat-row">
            <span>跳过 / 不支持</span
            ><strong>{{ state.preview.counts.skipped }} / {{ state.preview.counts.unsupported }}</strong>
          </div>
        </div>
        <div v-else class="tf-summary-block">
          <div class="tf-stat-row">
            <span>本次临时覆盖</span><strong>{{ state.overrideCount }} 项</strong>
          </div>
          <p class="tf-summary-note">全局配置不会改变。预览冻结目标路径，执行前再次核验。</p>
        </div>
        <div v-if="state.preview?.warnings.length" class="tf-summary-block">
          <span class="tf-summary-label">需要注意</span>
          <ul class="tf-summary-warnings">
            <li v-for="warning in state.preview.warnings.slice(0, 2)" :key="warning">{{ warning }}</li>
          </ul>
        </div>
        <div v-if="state.result" class="tf-summary-status">
          <v-chip :color="RESULT_COLORS[state.result.status]" variant="tonal" size="small"
            >{{ RESULT_LABELS[state.result.status] }} · {{ confirmedCount }} /
            {{ state.result.operations.length }}</v-chip
          >
          <p class="tf-summary-note">{{ state.result.message }}</p>
        </div>
        <v-alert v-if="state.expired && !state.result" variant="tonal" type="warning" density="compact" class="mt-3"
          >预览已过期，请重新生成。</v-alert
        >
        <div class="tf-summary-action">
          <template v-if="state.result"
            ><v-btn
              v-if="state.result.can_recover"
              block
              color="primary"
              variant="tonal"
              :disabled="state.result.actual?.moving"
              @click="recoverResult"
              >核对恢复预览</v-btn
            ><v-btn v-else block color="primary" variant="tonal" @click="navigate('select')">再次选择种子</v-btn
            ><v-btn block variant="text" class="mt-2" @click="navigate('history')">处理历史</v-btn></template
          >
          <template v-else
            ><v-btn
              v-if="state.canExecute"
              block
              :color="recovering ? 'warning' : 'primary'"
              variant="flat"
              @click="askConfirmation"
              >{{ recovering ? '确认恢复此种子' : '确认处理此种子' }}</v-btn
            ><v-btn
              v-else
              block
              color="primary"
              variant="flat"
              :disabled="!canGenerate"
              :loading="state.loading.preview || state.loading.execute"
              @click="preview"
              >{{ state.preview ? '重新生成预览' : '生成只读预览' }}</v-btn
            ><v-btn
              v-if="state.detail?.processed && !recovering"
              block
              color="warning"
              variant="text"
              class="mt-2"
              @click="navigate('history')"
              >先恢复已处理种子</v-btn
            ><v-btn
              v-else-if="state.preview && state.canExecute"
              block
              variant="text"
              size="small"
              class="mt-2"
              @click="preview"
              >重新计算</v-btn
            ></template
          >
        </div>
        <p class="tf-summary-note">
          {{
            state.result
              ? '未执行项不会自动继续，部分失败请先恢复。'
              : state.preview?.status === 'blocked'
                ? state.preview.message
                : '只有明确确认后才会修改。备份在第一次调用前保存。'
          }}
        </p>
        </template>
      </div>
    </template>
    <template #actions>
      <template v-if="section === 'history'"
        ><span>{{ state.history.total }} 条匹配记录</span
        ><v-btn
          color="primary"
          variant="tonal"
          :disabled="working"
          :loading="state.loading.history"
          @click="state.refreshHistory"
          >刷新历史</v-btn
        ></template
      >
      <template v-else-if="section === 'select'">
        <span class="tf-selection-status">{{ state.loading.detail ? '正在读取详情' : state.errors.detail ? '请重试详情' : state.selected ? '已选择 1 个种子' : '请选择种子' }}</span>
        <v-btn color="primary" variant="tonal" :disabled="!canContinue" :loading="state.loading.detail"
          @click="continueToMedia">下一步：媒体信息</v-btn>
      </template>
      <template v-else>
        <span>{{
          state.result
            ? RESULT_LABELS[state.result.status]
            : state.preview
              ? `${changedCount} 项计划变化`
              : '先预览，再执行'
        }}</span>
        <v-btn
          v-if="state.canExecute"
          :color="recovering ? 'warning' : 'primary'"
          variant="flat"
          @click="askConfirmation"
          >{{ recovering ? '确认恢复' : '确认处理' }}</v-btn
        >
        <v-btn v-else-if="state.result" color="primary" variant="tonal" @click="navigate('history')">处理历史</v-btn>
        <v-btn
          v-else
          color="primary"
          variant="flat"
          :disabled="!canGenerate"
          :loading="state.loading.preview || state.loading.execute"
          @click="preview"
          >生成预览</v-btn
        >
      </template>
    </template>
  </FormatterShell>
  <v-dialog v-model="confirmation" max-width="560" :persistent="state.loading.execute" aria-label="确认单种子操作">
    <v-card v-if="confirmedPreview" class="tf-shell tf-confirm">
      <v-card-title class="pt-5 px-5">{{
        confirmedPreview.mode === 'recover' ? '确认恢复原始布局' : '确认修改这个种子'
      }}</v-card-title>
      <v-card-text class="px-5">
        <p class="text-body-2">
          {{
            confirmedPreview.mode === 'recover'
              ? '将按下方预览还原名称、目录及文件路径。全部确认恢复后才清理备份。'
              : '这是源下载目录操作，不是媒体库副本。执行前会重新核对种子与规则。'
          }}
        </p>
        <div class="tf-confirm-details">
          <strong>{{ confirmedPreview.before.name }}</strong
          ><span class="tf-meta-line">{{ confirmedPreview.downloader }}</span
          ><code class="tf-code-path mt-2">{{ confirmedPreview.torrent_hash }}</code
          ><span class="tf-summary-label mt-3">目标保存路径</span
          ><code class="tf-code-path">{{ confirmedPreview.after.save_path }}</code>
          <div v-if="confirmedPreview.media" class="mt-3 tf-summary-name">
            {{ confirmedPreview.media.title }} {{ confirmedPreview.media.year }} ·
            {{ confirmedPreview.media.media_source }} {{ confirmedPreview.media.media_id }}
          </div>
          <div class="tf-confirm-counts">
            <v-chip size="small" variant="tonal">目录 {{ confirmedPreview.counts.save_path }}</v-chip
            ><v-chip size="small" variant="tonal">文件 {{ confirmedPreview.counts.files }}</v-chip
            ><v-chip size="small" variant="tonal">显示名 {{ confirmedPreview.counts.name }}</v-chip>
          </div>
        </div>
        <v-alert v-if="confirmedPreview.counts.files" type="warning" variant="tonal" density="compact"
          >文件布局修改可能影响辅种与已整理记录关联，请确认目录树和每项路径。</v-alert
        ><v-checkbox
          v-model="acknowledged"
          :disabled="state.loading.execute"
          color="primary"
          hide-details
          label="我已核对媒体、原始路径与前后差异，确认执行此单种子操作"
        />
        <p v-if="!state.canExecute && !state.loading.execute" class="tf-muted text-caption mt-2">
          预览已失效，请关闭此窗口并重新生成。
        </p>
      </v-card-text>
      <v-card-actions class="pa-4"
        ><v-spacer /><v-btn :disabled="state.loading.execute" variant="text" @click="confirmation = false">取消</v-btn
        ><v-btn
          :color="confirmedPreview.mode === 'recover' ? 'warning' : 'primary'"
          variant="flat"
          :loading="state.loading.execute"
          :disabled="!confirmReady"
          @click="execute"
          >{{ confirmedPreview.mode === 'recover' ? '确认恢复' : '确认处理' }}</v-btn
        ></v-card-actions
      >
    </v-card>
  </v-dialog>
  <v-dialog
    v-model="deleteConfirmation"
    max-width="720"
    content-class="tf-history-delete-dialog"
    scrollable
    :persistent="state.loading.historyDelete || state.loading.historyLookup"
    aria-label="删除插件备份记录"
  >
    <v-card v-if="historyToDelete.length" class="tf-shell tf-confirm tf-history-delete-modal">
      <v-card-title class="tf-history-delete-title pt-5 px-5">
        {{ historyToDelete.length === 1 ? '删除插件备份记录' : `删除 ${historyToDelete.length} 条插件备份记录` }}
      </v-card-title>
      <v-card-text class="tf-history-delete-content px-5">
        <p class="text-body-2">
          以下是本次冻结的完整清单，共 {{ historyToDelete.length }} 条。跨页选择不会扩大到其他筛选结果，单条删除也使用同一批次接口。
        </p>
        <div class="tf-history-delete-list" role="list" aria-label="待删除历史记录完整清单" tabindex="0">
          <div
            v-for="item in historyToDelete"
            :key="item.id || item.history_id"
            class="tf-history-delete-row"
            role="listitem"
          >
            <div class="tf-history-delete-heading">
              <strong>{{ item.name }}</strong>
              <v-chip
                v-if="deleteResultMap.get(String(item.id || item.history_id))"
                :color="deleteResultColors[deleteResultMap.get(String(item.id || item.history_id)).status]"
                size="x-small"
                variant="tonal"
                >{{ deleteResultLabels[deleteResultMap.get(String(item.id || item.history_id)).status] }}</v-chip
              >
            </div>
            <span class="tf-meta-line">原下载器：{{ item.downloader }}</span>
            <code class="tf-code-path mt-1">{{ item.torrent_hash }}</code>
            <p
              v-if="deleteResultMap.get(String(item.id || item.history_id))"
              class="tf-change-reason"
            >
              {{ deleteResultMap.get(String(item.id || item.history_id)).message }}
            </p>
            <p v-else class="tf-change-reason">{{ item.presence.message }}</p>
          </div>
        </div>
        <v-alert
          variant="tonal"
          type="warning"
          density="compact"
          >只删除当前插件实例的原始备份及关联日志，<strong>不会删除下载任务或文件，也不会还原已修改的路径</strong>。确认移除的记录将永久失去恢复能力。</v-alert
        >
        <v-checkbox
          v-if="!deleteAttempted"
          v-model="deleteAcknowledged"
          :disabled="state.loading.historyDelete"
          color="error"
          hide-details
          :label="`我已核对以上 ${historyToDelete.length} 条完整清单，确认只清理插件记录并放弃恢复能力`"
        />
        <v-alert v-if="state.errors.historyDelete" type="error" variant="tonal" density="compact" class="mt-3"
          >{{ state.errors.historyDelete }}
          <p class="mb-0 mt-2">不会自动重发删除请求。结果未知时只能使用只读按 ID 核验。</p></v-alert
        >
        <v-alert
          v-else-if="deleteAttempted"
          :type="state.historyDeleteResults.every(item => ['deleted', 'not_found'].includes(item.status)) ? 'success' : 'warning'"
          variant="tonal"
          density="compact"
          class="mt-3"
          >逐项结果已列在完整清单中。失败、冲突或忙碌项仍保留，不会自动重试。</v-alert
        >
      </v-card-text>
      <v-card-actions class="tf-history-delete-actions pa-4">
        <v-spacer />
        <v-btn
          variant="text"
          :disabled="state.loading.historyDelete || state.loading.historyLookup"
          @click="deleteConfirmation = false"
          >{{ deleteAttempted ? '关闭' : '取消' }}</v-btn
        ><v-btn
          v-if="deleteHasUnknown"
          color="warning"
          variant="tonal"
          :loading="state.loading.historyLookup"
          :disabled="state.loading.historyDelete"
          @click="verifyDeletionOutcome"
          >只读核验结果</v-btn
        ><v-btn
          v-if="!deleteAttempted"
          color="error"
          variant="flat"
          :disabled="!canDeleteHistory"
          :loading="state.loading.historyDelete"
          @click="deleteHistory"
          >{{ historyToDelete.length === 1 ? '删除备份记录' : `删除 ${historyToDelete.length} 条备份记录` }}</v-btn
        >
      </v-card-actions>
    </v-card>
  </v-dialog>
  <v-snackbar v-model="snackbar.show" :color="snackbar.color" :timeout="2500">{{ snackbar.text }}</v-snackbar>
</template>
