<script setup>
import { computed, getCurrentScope, onScopeDispose, ref, watch } from 'vue'
import { createTorrentFormatterApi, errorMessage } from '../api/torrentformatter.js'

const props = defineProps({
  api: { type: Object, required: true },
  modelValue: { type: Object, default: null },
  hint: { type: Object, default: null },
  season: { type: [Number, String], default: null },
  busy: Boolean,
  torrentName: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue', 'update:season'])
const inputMethod = ref('search')
const keyword = ref('')
const id = ref('')
const type = ref('movie')
const results = ref([])
const loading = ref(false)
const searched = ref(false)
const error = ref('')
let requestId = 0
let abort
const current = computed(() => props.modelValue || props.hint)
const isTv = computed(() => current.value?.media_type === 'tv')
const validId = computed(() => /^[1-9]\d{0,11}$/.test(id.value))
const sourceLabels = { manual: '本次指定', history: '下载记录', assisted: '辅种源记录', recognized: '名称识别' }
const manualSourceLabel = computed(() =>
  props.modelValue?.selection_method === 'search'
    ? '本次指定 · 搜索选择'
    : props.modelValue?.selection_method === 'id'
      ? '本次指定 · TMDB ID'
      : '本次手动指定',
)

async function search() {
  const title = (keyword.value || '').trim()
  if (!title || props.busy || inputMethod.value !== 'search') return
  const version = ++requestId
  abort?.abort()
  abort = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const data = await createTorrentFormatterApi(props.api).searchMedia(title, abort.signal)
    if (version === requestId) {
      results.value = data
      searched.value = true
    }
  } catch (err) {
    if (version === requestId) error.value = errorMessage(err, '媒体搜索失败')
  } finally {
    if (version === requestId) loading.value = false
  }
}

function choose(item, method = 'search') {
  if (props.busy) return
  emit('update:modelValue', { ...item, source: 'manual', selection_method: method })
}
function applyId() {
  if (inputMethod.value === 'id' && validId.value)
    choose({ media_id: id.value, media_type: type.value, title: `TMDB ${id.value}`, year: '', poster: '' }, 'id')
}
watch(
  inputMethod,
  method => {
    // 取消退出的搜索，不让隐藏表单的旧响应改变当前显示。
    requestId++
    abort?.abort()
    loading.value = false
    error.value = ''
    if (method === 'id' && !id.value && props.modelValue) {
      id.value = props.modelValue.media_id
      type.value = props.modelValue.media_type
    }
  },
  { flush: 'sync' },
)
watch(
  () => props.torrentName,
  () => {
    requestId++
    abort?.abort()
    keyword.value = ''
    id.value = ''
    type.value = 'movie'
    inputMethod.value = 'search'
    results.value = []
    searched.value = false
    loading.value = false
    error.value = ''
  },
)
if (getCurrentScope())
  onScopeDispose(() => {
    requestId++
    abort?.abort()
  })
</script>

<template>
  <section>
    <div class="tf-heading">
      <div>
        <span class="tf-eyebrow">02 / MEDIA IDENTITY</span>
        <h2>确认媒体信息</h2>
        <p>自动识别不准确或没有 MP 下载记录？搜索 TMDB 或输入明确的媒体 ID。</p>
      </div>
    </div>
    <div class="tf-card">
      <div class="tf-card-header">
        <h3>当前生效媒体</h3>
        <v-btn
          v-if="modelValue"
          variant="text"
          color="primary"
          size="small"
          :disabled="busy"
          @click="emit('update:modelValue', null)"
          >恢复自动识别</v-btn
        ><span v-else class="tf-source-badge">自动 / 下载记录</span>
      </div>
      <div class="tf-card-body">
        <div v-if="current" class="tf-media-current">
          <v-img v-if="current.poster" :src="current.poster" class="tf-poster" cover aspect-ratio=".72" />
          <div v-else class="tf-poster d-flex align-center justify-center">
            <v-icon icon="mdi-movie-open-outline" color="primary" />
          </div>
          <div class="tf-media-copy">
            <h3>
              {{ current.title }} <span class="tf-muted">{{ current.year }}</span>
            </h3>
            <div class="tf-meta-line">
              <span>{{ current.media_type === 'tv' ? '电视剧' : '电影' }}</span
              ><span>{{ current.media_source || 'themoviedb' }} / {{ current.media_id || '待识别' }}</span>
            </div>
            <div class="mt-2">
              <span class="tf-source-badge">{{
                modelValue ? manualSourceLabel : sourceLabels[current.source] || '下载记录提示'
              }}</span>
            </div>
          </div>
        </div>
        <div v-else class="tf-meta-line">
          <v-icon icon="mdi-auto-fix" size="20" color="primary" /><span
            >尚未识别。生成预览时会按下载历史 → 种子名称尝试识别。</span
          >
        </div>
        <p class="mb-0">手动身份优先，只影响这次处理。标题、年份和实际命名仍由后端从媒体源读取。</p>
      </div>
    </div>
    <div class="tf-card tf-media-picker">
      <div class="tf-card-header tf-media-method-switch">
        <h3>手动指定媒体</h3>
        <v-btn-toggle
          v-model="inputMethod"
          mandatory
          color="primary"
          variant="tonal"
          density="compact"
          divided
          aria-label="媒体指定方式"
        >
          <v-btn
            value="search"
            size="small"
            :disabled="busy"
            :aria-pressed="inputMethod === 'search'"
            prepend-icon="mdi-magnify"
            >搜索媒体</v-btn
          >
          <v-btn value="id" size="small" :disabled="busy" :aria-pressed="inputMethod === 'id'" prepend-icon="mdi-pound"
            >指定 TMDB ID</v-btn
          >
        </v-btn-toggle>
      </div>
      <p class="tf-media-method-note">
        两种方式指定同一个媒体，选一种即可。切换方式不改变上方生效媒体，选择结果或点击「应用此 ID」后才生效。
      </p>
      <template v-if="inputMethod === 'search'">
        <div class="tf-card-body">
          <div class="tf-media-search">
            <v-text-field
              v-model="keyword"
              label="电影或剧集名称"
              placeholder="例如：星际穿越 2014"
              variant="outlined"
              density="compact"
              hide-details
              clearable
              :disabled="busy"
              prepend-inner-icon="mdi-magnify"
              @keydown.enter.prevent="search"
            /><v-btn
              color="primary"
              variant="tonal"
              :loading="loading"
              :disabled="busy || !keyword?.trim()"
              @click="search"
              >搜索</v-btn
            >
          </div>
          <v-alert v-if="error" class="mt-3" variant="tonal" type="error" density="compact">{{ error }}</v-alert>
        </div>
        <div v-if="results.length" class="tf-media-results">
          <button
            v-for="item in results"
            :key="`${item.media_type}:${item.media_id}`"
            class="tf-media-result"
            :class="{
              'tf-media-result--active':
                modelValue?.media_id === item.media_id && modelValue?.media_type === item.media_type,
            }"
            type="button"
            :disabled="busy"
            @click="choose(item)"
          >
            <v-img v-if="item.poster" :src="item.poster" class="tf-poster" cover />
            <div class="tf-media-copy">
              <h3>
                {{ item.title }} <span class="tf-muted">{{ item.year }}</span>
              </h3>
              <div class="tf-meta-line">
                {{ item.media_type === 'tv' ? '电视剧' : '电影' }} · TMDB {{ item.media_id }}
              </div>
              <p v-if="item.overview">{{ item.overview }}</p>
            </div>
            <v-icon icon="mdi-plus-circle-outline" color="primary" size="20" />
          </button>
        </div>
        <div v-else-if="searched && !loading" class="tf-card-body tf-muted">
          没有找到匹配结果，可换关键词，或切换到「指定 TMDB ID」。
        </div>
      </template>
      <div v-else class="tf-card-body">
        <div class="tf-form-grid">
          <v-select
            v-model="type"
            :items="[
              { title: '电影', value: 'movie' },
              { title: '电视剧', value: 'tv' },
            ]"
            label="媒体类型"
            variant="outlined"
            density="compact"
            hide-details
            :disabled="busy"
          /><v-text-field
            v-model="id"
            label="TMDB ID"
            inputmode="numeric"
            placeholder="正整数 ID"
            maxlength="12"
            variant="outlined"
            density="compact"
            hide-details
            :disabled="busy"
            @keydown.enter.prevent="applyId"
          />
        </div>
        <div class="tf-action-row">
          <span class="tf-muted text-caption">同时指定类型与 ID，避免电影/剧集 ID 混淆。编辑输入后请点击应用。</span
          ><v-btn variant="tonal" color="primary" :disabled="!validId || busy" size="small" @click="applyId"
            >应用此 ID</v-btn
          >
        </div>
      </div>
    </div>
    <div v-if="isTv" class="tf-card">
      <div class="tf-card-body">
        <v-text-field
          :model-value="season"
          label="补充季号（可选）"
          type="number"
          min="0"
          max="99"
          hint="仅补充未识别季号的文件，0 表示特别篇；不会按文件顺序猜集数。"
          persistent-hint
          clearable
          :disabled="busy"
          variant="outlined"
          density="compact"
          @update:model-value="emit('update:season', $event === '' || $event === null ? null : Number($event))"
        />
      </div>
    </div>
  </section>
</template>
