<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { errorMessage, formatTime, resultColor, resultLabel, results, type Api, type RecordItem } from '../api'
const props = defineProps<{ api: Api; mediaKey: string; result: string; query: string }>()
const page = ref(1)
const items = ref<RecordItem[]>([])
const total = ref(0)
const error = ref('')
const loading = ref(false)
const resultCategories = results.slice(1)
const itemResults = (item: RecordItem) => resultCategories.filter(category => item.counts[category.value] > 0)
let requestId = 0
async function load() {
  const id = ++requestId
  loading.value = true
  error.value = ''
  try {
    const data = await props.api.get('history/records', {
      media_key: props.mediaKey,
      page: page.value,
      page_size: 10,
      result: props.result,
      query: props.query,
    })
    if (id === requestId) {
      items.value = data.items
      total.value = data.total
    }
  } catch (err) {
    if (id === requestId) error.value = errorMessage(err)
  } finally {
    if (id === requestId) loading.value = false
  }
}
watch(page, load)
watch(
  () => [props.mediaKey, props.result, props.query],
  () => {
    requestId++
    items.value = []
    if (page.value !== 1) page.value = 1
    else void load()
  },
  { flush: 'sync' },
)
onMounted(load)
onBeforeUnmount(() => {
  requestId++
})
</script>
<template>
  <div class="ct-history-attempts">
    <v-progress-linear v-if="loading" indeterminate color="primary" aria-label="正在读取资源明细" />
    <v-alert v-if="error" type="error" variant="tonal" density="compact"
      >{{ error }}<v-btn variant="text" size="small" :disabled="loading" @click="load">重试</v-btn></v-alert
    >
    <p v-if="!loading && !error && !items.length" class="ct-record-empty">暂无符合条件的资源明细</p>
    <article v-for="item in items" :key="item.id" class="ct-record ct-surface ct-surface--nested app-surface app-surface-static">
      <div class="ct-record-heading">
        <p class="ct-record-title">{{ item.resource_title || '无资源标题' }}</p>
        <v-chip class="ct-record-total" size="x-small" variant="tonal">累计处理 {{ item.count }} 次</v-chip>
      </div>
      <p v-if="item.resource_subtitle" class="ct-record-subtitle">
        <span class="ct-record-subtitle-label">副标题：{{ item.resource_subtitle }}</span>
      </p>
      <div class="ct-record-counts" aria-label="资源累计分类次数">
        <v-chip
          v-for="category in itemResults(item)"
          :key="category.value"
          class="ct-record-count"
          :class="{ 'ct-record-count--selected': result === category.value }"
          :data-result="category.value"
          :aria-current="result === category.value ? 'true' : undefined"
          :color="resultColor(category.value)"
          size="x-small"
          variant="tonal"
          >{{ category.title }} {{ item.counts[category.value] }} 次</v-chip
        >
        <span v-if="result" class="ct-record-selected-count">选中分类 {{ item.selected_count }} 次</span>
      </div>
      <div class="ct-record-summary">
        <v-chip :color="resultColor(item.result)" size="x-small" variant="tonal">{{ resultLabel(item.result) }}</v-chip>
        <time>{{ formatTime(item.created_at) }}</time>
        <span>{{ result ? '选中分类最新摘要' : '资源最新摘要' }}</span>
      </div>
      <p class="ct-record-range">
        <code>{{ item.original_range || '未解析' }}</code> → <code>{{ item.final_range || '未解析' }}</code>
      </p>
      <p class="ct-record-reason">{{ item.reason || '无额外原因' }}</p>
      <p class="ct-record-source">
        来源：{{ item.source || '未知' }}<span v-if="item.strategy"> · 策略：{{ item.strategy }}</span>
      </p>
    </article>
    <v-pagination
      v-if="total > 10"
      v-model="page"
      :length="Math.ceil(total / 10)"
      :total-visible="4"
      density="compact"
      aria-label="资源明细分页"
      :disabled="loading"
    />
  </div>
</template>
