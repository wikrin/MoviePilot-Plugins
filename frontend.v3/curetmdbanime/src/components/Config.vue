<script setup lang="ts">
import { computed, reactive, ref, useId, watch } from 'vue'
import { useTheme } from 'vuetify'
import { createApi, defaults, type Client, type Config } from '../api'
import CureShell from './CureShell.vue'
import RuntimePanel from './RuntimePanel.vue'
import LogPanel from './LogPanel.vue'
import CorrectionPanel from './CorrectionPanel.vue'
import HistoryPanel from './HistoryPanel.vue'
import SurfaceCard from './SurfaceCard.vue'
import SettingRow from './SettingRow.vue'

const props = defineProps<{
  initialConfig?: Partial<Config>
  api: Client
  pluginId?: string
  sourcePluginId?: string
}>()
const emit = defineEmits(['save', 'close'])
const theme = useTheme()
const themeMode = computed(() => (theme.current.value.dark ? 'dark' : 'light'))
const config = reactive<Config>({ ...defaults })
const baseline = ref('')
const baselineEnabled = ref(defaults.enabled)
const enabledHintId = `ct-enabled-hint-${useId()}`
const page = ref('service-overview')
const busy = ref(false)
const form = ref<any>(null)
const headerSaveTarget = ref<HTMLElement | null>(null)
const dockSaveTarget = ref<HTMLElement | null>(null)
const api = computed(() => createApi(props.api, props.pluginId || 'CureTMDbAnime'))
// 日志读取已保存目标；编辑草稿不能让两个服务的输出混在同一面板。
const logTarget = computed(() => {
  const saved = props.initialConfig || {}
  const mode = saved.service_mode || defaults.service_mode
  return JSON.stringify([
    props.pluginId,
    mode,
    mode === 'external' ? saved.external_url || defaults.external_url : saved.port || defaults.port,
    saved.enabled ?? defaults.enabled,
  ])
})
const groups = [
  {
    id: 'service',
    title: '服务管理',
    icon: 'mdi-server-network',
    overviewId: 'service-overview',
    children: [
      { id: 'service-config', title: '部署与运行配置', icon: 'mdi-cog-outline' },
      { id: 'service-logs', title: '运行日志', icon: 'mdi-text-box-outline' },
    ],
  },
  {
    id: 'correction',
    title: '元数据修正',
    icon: 'mdi-animation-play-outline',
    overviewId: 'correction-rules',
    children: [
      { id: 'correction-rules', title: '修正规则', icon: 'mdi-tune-variant' },
      { id: 'correction-history', title: '修正历史', icon: 'mdi-history' },
    ],
  },
]
// 标题与说明集中维护，切换页面时同步更新页头和导航的无障碍名称。
const pages: Record<string, { title: string; description: string }> = {
  'service-overview': {
    title: '服务概览',
    description: '查看服务运行状态、管理托管进程，并检测服务与上游连通性。',
  },
  'service-config': {
    title: '部署与运行配置',
    description: '配置插件托管参数，或连接外部服务。',
  },
  'service-logs': {
    title: '运行日志',
    description: '查看当前服务的运行输出。插件自身日志请在 MoviePilot 插件日志中查看。',
  },
  'correction-rules': {
    title: '修正规则',
    description: '配置动画识别与整理流程使用的季集修正规则。',
  },
  'correction-history': {
    title: '修正历史',
    description: '按媒体查看资源聚合与分类最新摘要，并管理资源保留和分类清理。',
  },
}
const pageTitle = computed(() => pages[page.value]?.title || '服务概览')
const sectionDescription = computed(() => pages[page.value]?.description || '管理插件设置。')
const groupTitle = computed(() => (page.value.startsWith('correction') ? '元数据修正' : '服务管理'))
// 整体草稿决定保存按钮状态；只有启用值变化才显示电源按钮上的「待保存」。
const dirty = computed(() => JSON.stringify(config) !== baseline.value)
const enabledDirty = computed(() => config.enabled !== baselineEnabled.value)
const integer = (min: number, max: number) => (v: unknown) =>
  (v !== '' && v !== null && Number.isInteger(Number(v)) && Number(v) >= min && Number(v) <= max) ||
  `请输入 ${min}–${max} 的整数`
watch(
  () => props.initialConfig,
  value => {
    Object.assign(config, defaults, value || {})
    baseline.value = JSON.stringify(config)
    baselineEnabled.value = config.enabled
  },
  { immediate: true, deep: true },
)
async function save() {
  if (busy.value) return
  if ((await form.value?.validate())?.valid === false) return
  emit('save', { ...config })
}
</script>

<template>
  <CureShell
    :page="page"
    :groups="groups"
    :group-title="groupTitle"
    :page-title="pageTitle"
    :dirty="dirty"
    :busy="busy"
    :theme-mode="themeMode"
    @update:page="page = $event"
    @close="emit('close')"
  >
    <template #header-actions="{ mobile }">
      <div class="ct-enable-control">
        <button
          type="button"
          class="ct-config-action ct-plugin-enable"
          :class="{ 'ct-plugin-enable--enabled': config.enabled }"
          :disabled="busy"
          aria-label="启用插件"
          :aria-pressed="config.enabled"
          :aria-describedby="enabledDirty ? `${enabledHintId} ${enabledHintId}-pending` : enabledHintId"
          @click="config.enabled = !config.enabled"
        >
          <v-icon icon="mdi-power" size="16" aria-hidden="true" />
          <span class="ct-plugin-enable-label">插件：{{ config.enabled ? '已启用' : '未启用' }}</span>
          <span v-if="enabledDirty" :id="`${enabledHintId}-pending`" class="ct-enable-pending">待保存</span>
        </button>
        <span
          :id="enabledHintId"
          class="ct-enable-hint"
          title="启用后连接服务并应用 TMDB 代理及修正补丁；停用后恢复原始行为。"
        ></span>
      </div>
      <!-- 复用同一个保存按钮，窄屏移到底栏，避免切换布局时丢失状态或出现双按钮。 -->
      <div ref="headerSaveTarget" />
      <Teleport
        :to="mobile && page !== 'correction-history' && dockSaveTarget ? dockSaveTarget : headerSaveTarget || 'body'"
        :disabled="!headerSaveTarget"
      >
        <v-btn
          class="ct-config-action ct-save-config"
          color="primary"
          variant="flat"
          size="small"
          :disabled="!dirty || busy"
          @click="save"
          >保存配置</v-btn
        >
      </Teleport>
    </template>

    <template #default="{ mobile, scrollElement, paginationTarget }">
      <div v-if="page !== 'correction-history' && page !== 'service-logs'" class="ct-heading">
        <div>
          <span class="ct-eyebrow">{{ groupTitle }}</span>
          <h2>{{ pageTitle }}</h2>
          <p>{{ sectionDescription }}</p>
        </div>
      </div>

      <v-form v-if="page !== 'service-logs' && page !== 'service-overview'" ref="form" @submit.prevent="save">
        <fieldset :disabled="busy" class="ct-fieldset">
          <section v-if="page === 'service-config'" aria-label="部署与运行配置">
            <SurfaceCard kicker="DEPLOYMENT" title="部署与服务连接" icon="mdi-connection" body-class="ct-settings-list">
              <!-- SettingRow 保留原有包装层与类名，只收敛重复的标题、说明和控件布局。 -->
              <SettingRow title="部署方式" description="选择插件托管本地服务，或连接独立部署的服务。">
                <v-select
                  v-model="config.service_mode"
                  :items="[
                    { title: '插件托管', value: 'local' },
                    { title: '外部连接', value: 'external' },
                  ]"
                  hide-details
                  density="compact"
                  variant="outlined"
                  aria-label="部署方式"
                />
              </SettingRow>
              <template v-if="config.service_mode === 'external'">
                <SettingRow
                  title="服务基础地址"
                  description="填写 MoviePilot 后端可访问的 HTTP(S) 地址，可包含反向代理路径前缀。"
                >
                  <v-text-field
                    v-model="config.external_url"
                    placeholder="http://curetmdbanime:8632"
                    hide-details="auto"
                    density="compact"
                    variant="outlined"
                    aria-label="服务基础地址"
                    :rules="[(v: string) => /^https?:\/\//.test(v) || '请输入 HTTP(S) 地址']"
                  />
                </SettingRow>
                <SettingRow
                  title="外部服务管理令牌"
                  description="若服务设置了 MANAGEMENT_TOKEN，请填写相同令牌以读取日志和检测上游；服务未设置时可留空。"
                  last
                >
                  <v-text-field
                    v-model="config.management_token"
                    type="password"
                    autocomplete="new-password"
                    hide-details
                    density="compact"
                    variant="outlined"
                    aria-label="外部服务管理令牌"
                  />
                </SettingRow>
                <v-alert type="info" variant="tonal" density="compact" class="mt-3">
                  外部服务的启动、停止、重启、下载和更新由容器部署工具管理。
                </v-alert>
              </template>
              <template v-else>
                <SettingRow title="监听端口" description="插件托管服务使用的监听端口。">
                  <v-text-field
                    v-model.number="config.port"
                    type="number"
                    hide-details="auto"
                    density="compact"
                    variant="outlined"
                    aria-label="监听端口"
                    :rules="[integer(1024, 65535)]"
                  />
                </SettingRow>
                <SettingRow title="TVDB API 地址" description="TVDB API 服务入口地址。">
                  <v-text-field
                    v-model="config.tvdb_api_url"
                    hide-details
                    density="compact"
                    variant="outlined"
                    aria-label="TVDB API 地址"
                  />
                </SettingRow>
                <SettingRow
                  title="Bangumi 使用代理"
                  description="启用后通过 MoviePilot 配置的代理访问 Bangumi。"
                  control="switch"
                >
                  <v-switch
                    v-model="config.bangumi_use_proxy"
                    color="primary"
                    hide-details
                    inset
                    aria-label="Bangumi 使用代理"
                  />
                </SettingRow>
                <SettingRow title="自定义分季 JSON 数据源" description="Bangumi 和 TMDB 上游地址沿用系统设置。" last>
                  <v-text-field
                    v-model="config.source"
                    clearable
                    hide-details
                    density="compact"
                    variant="outlined"
                    aria-label="自定义分季 JSON 数据源"
                  />
                </SettingRow>
              </template>
            </SurfaceCard>
          </section>

          <section v-else-if="page === 'correction-rules'" aria-label="修正规则">
            <CorrectionPanel
              :model-value="config"
              @update:model-value="Object.assign(config, $event)"
              :integer="integer"
            />
          </section>
          <HistoryPanel
            v-else-if="page === 'correction-history'"
            :api="api"
            :busy="busy"
            :max-records="config.history_max_records"
            :integer="integer"
            :mobile="mobile"
            :scroll-element="scrollElement"
            :pagination-target="paginationTarget"
            @update:max-records="config.history_max_records = $event"
          >
            <template #intro>
              <div class="ct-heading">
                <div>
                  <span class="ct-eyebrow">{{ groupTitle }}</span>
                  <h2>{{ pageTitle }}</h2>
                  <p>{{ sectionDescription }}</p>
                </div>
              </div>
            </template>
          </HistoryPanel>
        </fieldset>
      </v-form>

      <RuntimePanel
        v-if="page === 'service-overview'"
        :api="api"
        :dirty="dirty"
        :mobile="mobile"
        @busy="busy = $event"
      />
      <LogPanel v-else-if="page === 'service-logs'" :key="logTarget" :api="api" :mobile="mobile" />
    </template>

    <template #actions>
      <span>{{ dirty ? '配置尚未保存' : '' }}</span>
      <div ref="dockSaveTarget" />
    </template>
  </CureShell>
</template>
