<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import FormatterShell from './FormatterShell.vue'
import RuleEditor from './RuleEditor.vue'
import { createTorrentFormatterApi, DEFAULT_RULES, errorMessage } from '../api/torrentformatter.js'

const props = defineProps({
  api: { type: Object, required: true },
  initialConfig: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['close', 'switch', 'save', 'layout'])
const defaults = {
  ...DEFAULT_RULES,
  cron_enabled: false,
  downloader: [],
  exclude_dirs: '',
  exclude_tags: '',
  cron: '',
  event_enabled: false,
}
const clone = value => JSON.parse(JSON.stringify(value))
const config = reactive(clone(defaults))
const baseline = ref(clone(defaults))
const section = ref('automation')
const clients = ref([])
const loading = ref(false)
const error = ref('')
const submitted = ref(false)
const form = ref(null)
const changed = computed(() =>
  Object.keys(defaults).filter(key => JSON.stringify(config[key]) !== JSON.stringify(baseline.value[key])),
)
const naming = computed({
  get: () => Object.fromEntries(Object.keys(DEFAULT_RULES).map(key => [key, config[key]])),
  set: value => Object.assign(config, value),
})
const sections = [
  { id: 'automation', title: '工作方式', subtitle: '事件与定时扫描' },
  { id: 'exclusions', title: '排除与保护', subtitle: '目录、标签及风险' },
  { id: 'naming', title: '命名规则', subtitle: '全局模板与开关' },
]
const cronRules = [
  value =>
    !config.cron_enabled ||
    !value ||
    /^[\d\w*,/-]+(?:\s+[\d\w*,/-]+){4}$/.test(value.trim()) ||
    '请输入 5 段 Cron 表达式，如 0 8 * * *',
]

watch(
  () => props.initialConfig,
  value => {
    Object.assign(config, clone({ ...defaults, ...value, downloader: [...(value.downloader || [])] }))
    baseline.value = clone(config)
    submitted.value = false
  },
  { deep: true, immediate: true },
)

async function loadClients() {
  loading.value = true
  error.value = ''
  try {
    clients.value = await createTorrentFormatterApi(props.api).downloaders()
  } catch (err) {
    error.value = errorMessage(err, '下载器列表加载失败')
  } finally {
    loading.value = false
  }
}
async function save() {
  const validation = await form.value?.validate()
  if (validation?.valid === false) return
  // emit 只提交请求，不能宣称已持久化；实际保存结果由宿主通知。
  emit('save', clone(config))
  submitted.value = true
}
function reset() {
  Object.assign(config, clone({ ...config, ...defaults }))
  submitted.value = false
  form.value?.resetValidation()
}
onMounted(() => {
  emit('layout', { maxWidth: '96rem' })
  void loadClients()
})
</script>

<template>
  <FormatterShell
    title="种子格式化"
    subtitle="全局配置"
    badge="AUTO RULES"
    :section="section"
    :sections="sections"
    @update:section="section = $event"
    @switch="emit('switch')"
    @close="emit('close')"
    @refresh="loadClients"
  >
    <template #header-actions
      ><v-btn
        class="tf-header-save"
        color="primary"
        variant="flat"
        size="small"
        :disabled="!changed.length"
        @click="save"
        >保存配置</v-btn
      ></template
    >
    <v-alert v-if="error" variant="tonal" type="error" density="compact" class="tf-inline-alert"
      >{{ error }}<v-btn size="small" variant="text" @click="loadClients">重试</v-btn></v-alert
    >
    <v-alert v-if="submitted" variant="tonal" type="info" density="compact" class="tf-inline-alert"
      >已请求宿主保存配置，实际保存结果以宿主通知为准。</v-alert
    >
    <v-form ref="form" @submit.prevent="save">
      <template v-if="section === 'automation'">
        <div class="tf-heading">
          <div>
            <span class="tf-eyebrow">01 / AUTOMATION</span>
            <h2>选择插件的工作方式</h2>
            <p>手动工作台始终可用，以下开关只控制自动处理。</p>
          </div>
        </div>
        <div class="tf-card">
          <div class="tf-card-header">
            <h3>事件监听</h3>
          </div>
          <div class="tf-card-body">
            <div class="tf-rule-row">
              <div class="tf-rule-copy">
                <strong>添加下载时自动处理</strong>
                <p>使用下载事件已有的媒体信息，仍遵守排除规则并保留原始备份。</p>
              </div>
              <v-switch
                v-model="config.event_enabled"
                inset
                color="primary"
                density="compact"
                hide-details
                aria-label="启用下载事件自动处理"
              />
            </div>
          </div>
        </div>
        <div class="tf-card">
          <div class="tf-card-header">
            <h3>定时扫描</h3>
            <v-switch
              v-model="config.cron_enabled"
              inset
              color="primary"
              density="compact"
              hide-details
              aria-label="启用定时扫描"
            />
          </div>
          <div class="tf-card-body">
            <v-select
              v-model="config.downloader"
              :items="clients.map(item => ({ title: item.name, value: item.name }))"
              :disabled="!config.cron_enabled || loading"
              :loading="loading"
              label="扫描下载器"
              hint="这里只限制定时扫描；手动工作台可选择所有已启用的支持下载器。"
              persistent-hint
              multiple
              chips
              clearable
              variant="outlined"
              density="compact"
            />
            <v-cron-field
              v-model="config.cron"
              :disabled="!config.cron_enabled"
              label="执行周期（Cron）"
              placeholder="0 8 * * *"
              hint="Cron 表达式，留空默认每天 08:00。"
              persistent-hint
              clearable
              variant="outlined"
              density="compact"
              class="mt-3"
            />
          </div>
        </div>
        <div class="tf-card">
          <div class="tf-card-body">
            <h3>想只处理一个种子？</h3>
            <p>不用启用自动任务，打开工作台即可选择、指定媒体、临时覆盖模板并预览差异。</p>
            <v-btn color="primary" variant="tonal" size="small" @click="emit('switch')">打开单种子工作台</v-btn>
          </div>
        </div>
      </template>
      <template v-else-if="section === 'exclusions'">
        <div class="tf-heading">
          <div>
            <span class="tf-eyebrow">02 / SAFETY</span>
            <h2>排除与保护规则</h2>
            <p>手动与自动流程共用这些保护，不会在单次处理时隐式绕过。</p>
          </div>
        </div>
        <div class="tf-card">
          <div class="tf-card-header">
            <h3>排除范围</h3>
          </div>
          <div class="tf-card-body">
            <v-textarea
              v-model="config.exclude_dirs"
              label="排除保存目录"
              rows="4"
              auto-grow
              clearable
              variant="outlined"
              density="compact"
              hint="每行一个，按目录边界匹配该目录及子目录，不按名称子串误匹配。"
              persistent-hint
              :placeholder="'/mnt/download/protected\nD:/downloads/protected'"
            />
            <v-text-field
              v-model="config.exclude_tags"
              label="排除标签"
              variant="outlined"
              density="compact"
              clearable
              class="mt-4"
              hint="多个标签用逗号分隔；空白标签条目匹配没有标签的种子。"
              persistent-hint
            />
          </div>
        </div>
        <div class="tf-card">
          <div class="tf-card-body">
            <h3>系统屏蔽与原始目录风险</h3>
            <p>
              文件改名遵守 MoviePilot
              的整理屏蔽词，命中文件会保留原路径并在预览中注明。非法目标、重名、越界路径不会执行。
            </p>
            <v-alert variant="tonal" type="warning" density="compact"
              >修改保存目录或文件布局属于源目录操作，已整理文件可能与整理记录失去关联；实验性文件改名可能影响辅种。操作前请核对预览。</v-alert
            >
          </div>
        </div>
      </template>
      <template v-else>
        <div class="tf-heading">
          <div>
            <span class="tf-eyebrow">03 / NAMING RULES</span>
            <h2>全局命名规则</h2>
            <p>这些规则用于自动任务，也是单次处理的默认起点。变量与 MoviePilot 整理命名保持一致。</p>
          </div>
        </div>
        <RuleEditor v-model="naming" :temporary="false" />
      </template>
    </v-form>
    <template #summary>
      <div class="tf-summary">
        <div class="tf-summary-heading">
          <h2>配置影响摘要</h2>
          <span class="tf-source-badge">全局</span>
        </div>
        <div class="tf-stat-row">
          <span>事件自动处理</span><strong>{{ config.event_enabled ? '启用' : '关闭' }}</strong>
        </div>
        <div class="tf-stat-row">
          <span>定时扫描</span><strong>{{ config.cron_enabled ? '启用' : '关闭' }}</strong>
        </div>
        <div v-if="config.cron_enabled" class="tf-summary-block">
          <span class="tf-summary-label">下载器 / 周期</span
          ><span class="tf-summary-name">{{ config.downloader.join('、') || '尚未选择' }}</span
          ><code class="tf-code-path mt-2">{{ config.cron || '0 8 * * *' }}</code>
        </div>
        <div class="tf-summary-block">
          <div class="tf-stat-row">
            <span>显示名重命名</span><strong>{{ config.rename_torrent ? '启用' : '关闭' }}</strong>
          </div>
          <div class="tf-stat-row">
            <span>实验性文件改名</span><strong>{{ config.rename_file ? '启用' : '关闭' }}</strong>
          </div>
          <p class="tf-summary-note">
            只对本插件的源下载目录进行处理。Transmission 不支持独立显示名及任意跨目录文件布局。
          </p>
        </div>
        <div class="tf-summary-block">
          <span class="tf-summary-label">未保存的改动</span
          ><strong class="tf-summary-name">{{ changed.length }} 项</strong>
          <p class="tf-summary-note">单种子临时规则不会改变这里的配置；已处理种子必须先恢复后再应用新规则。</p>
        </div>
        <div class="tf-summary-action">
          <v-btn color="primary" variant="flat" block :disabled="!changed.length" @click="save">保存全局配置</v-btn
          ><v-btn variant="text" block class="mt-2" @click="reset">恢复默认值</v-btn>
        </div>
        <p class="tf-summary-note">配置提交由宿主负责保存。插件重置会清除处理备份。</p>
      </div>
    </template>
    <template #actions
      ><span>{{ changed.length ? `${changed.length} 项尚未保存` : '没有未保存改动' }}</span
      ><v-btn color="primary" variant="flat" :disabled="!changed.length" @click="save">保存配置</v-btn></template
    >
  </FormatterShell>
</template>
