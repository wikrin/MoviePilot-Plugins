<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue';

const props = defineProps({
  api: {
    type: Object,
    required: true,
  },
  initialConfig: {
    type: Object,
    default: () => ({}),
  }
});

const emit = defineEmits(['close', 'switch', 'save']);

// 插件默认配置
const defaultConfig = reactive({
  cron_enabled: false,
  downloader: [],
  exclude_dirs: "",
  exclude_tags: "",
  cron: "",
  event_enabled: false,
  rename_torrent: false,
  rename_file: false,
  format_torrent_name: "{{ title }}{% if year %} ({{ year }}){% endif %}{% if season_episode %} - {{season_episode}}{% endif %}",
  format_save_path: "{{title}}{% if year %} ({{year}}){% endif %}",
  format_movie_path: "{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}",
  format_tv_path: "Season {{season}}/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}",
});

const downloaderOptions = ref<{ title: string; value: string }[]>([])
const config = reactive({ ...defaultConfig })

const form = ref<{ validate: () => Promise<{ valid: boolean }>; resetValidation: () => void } | null>(null);
const isFormValid = ref(true);
const error = ref<string | null>(null);
const successMessage = ref<string | null>(null);
const saving = ref(false);
const _tabs = ref('basic_tab');


async function loadDownloaderSetting() {
  try {
    const downloaders = await props.api.get('download/clients')
    downloaderOptions.value = [
      { title: '默认', value: '' },
      ...downloaders.map((item: { name: any }) => ({
        title: item.name,
        value: item.name,
      })),
    ]
  } catch (error) {
    console.error('加载下载器设置失败:', error)
  }
}

// 保存配置
async function saveConfig() {
  if (!isFormValid.value) {
    error.value = '请修正表单错误'
    return
  }

  saving.value = true
  error.value = null

  try {
    // 发送保存事件
    emit('save', { ...config })
  } catch (err) {
    console.error('保存配置失败:', err)
    error.value = err.message || '保存配置失败'
  } finally {
    saving.value = false
  }
}

// 重置表单
function resetForm() {
  Object.keys(defaultConfig).forEach(key => {
    config[key] = defaultConfig[key]
  })

  if (form.value) {
    form.value.resetValidation()
  }
}

// 初始化配置
onMounted(() => {
  // 加载初始配置
  if (props.initialConfig) {
    Object.keys(props.initialConfig).forEach(key => {
      if (key in config) {
        config[key] = props.initialConfig[key]
      }
    })
  }
  // 加载下载器设置
  loadDownloaderSetting()
})

</script>

<template>
  <div class="plugin-config">
    <v-card flat class="rounded border">
      <!-- 标题区域 -->
      <v-card-title class="text-subtitle-1 d-flex align-center px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-cog" class="mr-2" color="primary" size="small" />
        <span>路径名称格式化配置</span>
      </v-card-title>
      <v-card-text class="px-3 py-2">
        <v-alert v-if="error" type="error" density="compact" class="mb-2 text-caption" variant="tonal" closable>{{ error }}</v-alert>
        <v-alert v-if="successMessage" type="success" density="compact" class="mb-2 text-caption" variant="tonal" closable>{{ successMessage }}</v-alert>
        <v-form ref="form" v-model="isFormValid" @submit.prevent="saveConfig">
          <!-- 基本设置卡片 -->
          <v-card flat class="rounded mb-3 border config-card">
            <v-card-title class="text-caption d-flex align-center px-3 py-2 bg-primary-lighten-5">
              <v-icon icon="mdi-tune" class="mr-2" color="primary" size="small" />
              <span>工作方式</span>
            </v-card-title>
            <v-card-text class="px-3 py-2">
              <v-row>
                <!-- 事件监听 -->
                <v-col cols="12" md="2">
                  <div class="setting-item d-flex align-center justify-space-between">
                    <div class="d-flex align-center">
                      <v-icon
                        icon="mdi-power"
                        size="small"
                        :color="config.event_enabled ? 'success' : 'grey'"
                        class="mr-3"
                      />
                      <span class="text-subtitle-2">事件监听</span>
                    </div>
                    <v-switch
                      v-model="config.event_enabled"
                      color="primary"
                      inset
                      :disabled="saving"
                      density="compact"
                      hide-details
                      class="small-switch"
                    />
                  </div>
                </v-col>
                <!-- 定时任务组件 -->
                <v-col cols="12" md="10">
                  <div class="related-components">
                    <v-row>
                      <!-- 定时任务 -->
                      <v-col cols="12" md="3">
                        <div class="setting-item d-flex align-center justify-space-between">
                          <div class="d-flex align-center">
                            <v-icon
                              icon="mdi-clock"
                              size="small"
                              :color="config.cron_enabled ? 'info' : 'grey'"
                              class="mr-3"
                            />
                            <span class="text-subtitle-2">定时任务</span>
                          </div>
                          <v-switch
                            v-model="config.cron_enabled"
                            color="info"
                            inset
                            :disabled="saving"
                            density="compact"
                            hide-details
                            class="small-switch"
                          />
                        </div>
                      </v-col>
                      <!-- 下载器选择 -->
                      <v-col cols="12" md="4">
                        <div class="setting-item d-flex align-center justify-space-between">
                          <div class="d-flex align-center">
                            <span class="text-subtitle-2">下载器</span>
                          </div>
                          <v-select
                            v-model="config.downloader"
                            :items="downloaderOptions"
                            :disabled="!config.cron_enabled"
                            variant="outlined"
                            multiple
                            chips
                            clearable
                            item-title="title"
                            item-value="value"
                            density="compact"
                            class="text-caption"
                            style="max-width: 140px"
                          >
                          </v-select>
                        </div>
                      </v-col>
                      <!-- 执行周期 -->
                      <v-col cols="12" md="5">
                        <div class="setting-item d-flex align-center justify-space-between">
                          <div class="d-flex align-center">
                            <span class="text-subtitle-2">执行周期</span>
                          </div>
                          <VCronField v-model="config.cron" density="compact" class="cron-field flex-grow-1 ml-2" :disabled="!config.cron_enabled" />
                        </div>
                      </v-col>
                    </v-row>
                  </div>
                </v-col>
              </v-row>
            </v-card-text>
          </v-card>

          <v-card flat class="rounded mb-3 border config-card">
            <v-card-title class="text-caption d-flex align-center px-3 py-2 bg-primary-lighten-5">
              <v-icon icon="mdi-clock-time-five" class="mr-2" color="primary" size="small" />
              <span>格式设置</span>
            </v-card-title>
            <v-card-text class="px-3 py-2">
              <!-- 统一 tabs 样式 -->
              <v-tabs v-model="_tabs" class="mb-3" density="compact" fixed-tabs>
                <v-tab value="basic_tab">基本设置</v-tab>
                <v-tab value="critical_tab">实验性功能</v-tab>
              </v-tabs>
              <v-window v-model="_tabs">
                <v-window-item value="basic_tab">
                  <v-row>
                    <v-col cols="12" md="7">
                      <div class="setting-item mb-2">
                        <v-text-field
                          v-model="config.format_save_path"
                          label="保存路径格式"
                          hint="使用Jinja2语法, 不会覆盖原保存路径, 仅追加. 留空不修改"
                          clearable
                          persistent-hint
                          density="compact"
                          variant="outlined"
                        />
                      </div>
                    </v-col>
                    <v-col cols="12" md="5">
                      <div class="setting-item mb-2">
                        <v-text-field
                          v-model="config.exclude_tags"
                          label="排除标签"
                          placeholder="空白字符会排除所有未设置标签的种子"
                          hint="多个标签用, 分割"
                          clearable
                          persistent-hint
                          density="compact"
                          variant="outlined"
                        />
                      </div>
                    </v-col>
                  </v-row>
                  <v-row>
                    <v-col cols="12">
                      <!-- 统一文本输入框样式 -->
                      <div class="setting-item mb-2">
                        <v-textarea
                          v-model="config.exclude_dirs"
                          rows="3"
                          auto-grow
                          label="排除目录"
                          hint="排除目录, 一行一个, 路径深度不能超过保存路径"
                          :placeholder="`例如:\n/mnt/download\nE:\\download`"
                          clearable
                          persistent-hint
                          density="compact"
                          variant="outlined"
                        />
                      </div>
                    </v-col>
                  </v-row>
                  <v-row>
                    <v-col cols="12" md="8">
                      <div class="setting-item d-flex align-center py-2">
                        <v-switch
                          v-model="config.rename_torrent"
                          label="种子重命名"
                          color="primary"
                          inset
                          :disabled="saving"
                          density="compact"
                          hide-details
                          class="small-switch"
                        />
                      </div>
                    </v-col>
                  </v-row>
                  <v-row>
                    <v-col cols="12">
                      <div class="setting-item mb-2">
                        <v-textarea
                          v-model="config.format_torrent_name"
                          rows="2"
                          auto-grow
                          label="种子标题重命名格式"
                          hint="使用Jinja2语法, 所用变量与主程序相同"
                          clearable
                          persistent-hint
                          density="compact"
                          variant="outlined"
                        />
                      </div>
                    </v-col>
                  </v-row>
                  <v-alert
                    type="info"
                    variant="tonal"
                    icon="mdi-information"
                  >
                    谨慎开启定时任务: 修改保存目录或种子文件都属于源目录操作, 已整理入库的文件会与整理记录失去关联!
                  </v-alert>
                  <v-alert
                    type="info"
                    variant="tonal"
                    icon="mdi-information"
                  >
                    种子重命名: 重命名种子在下载器显示的名称,qBittorrent 不会影响保存路径和种子内容布局; Transmission 不支持
                  </v-alert>
                </v-window-item>
                <v-window-item value="critical_tab">
                  <v-row>
                    <v-col cols="12" md="8">
                      <div class="setting-item d-flex align-center py-2">
                        <v-switch
                          v-model="config.rename_file"
                          label="种子文件重命名(实验功能)"
                          color="primary"
                          inset
                          :disabled="saving"
                          density="compact"
                          hide-details
                          class="small-switch"
                        />
                      </div>
                    </v-col>
                  </v-row>
                  <v-row>
                    <v-col cols="12">
                      <div class="setting-item mb-2">
                        <v-textarea
                          v-model="config.format_movie_path"
                          rows="3"
                          auto-grow
                          label="电影文件重命名格式"
                          hint="使用Jinja2语法, 所用变量与主程序相同"
                          clearable
                          persistent-hint
                          density="compact"
                          variant="outlined"
                        />
                      </div>
                    </v-col>
                  </v-row>
                  <v-row>
                    <v-col cols="12">
                      <div class="setting-item mb-2">
                        <v-textarea
                          v-model="config.format_tv_path"
                          rows="3"
                          auto-grow
                          label="电视剧文件重命名格式"
                          hint="使用Jinja2语法, 所用变量与主程序相同"
                          clearable
                          persistent-hint
                          density="compact"
                          variant="outlined"
                        />
                      </div>
                    </v-col>
                  </v-row>
                  <v-alert
                    type="info"
                    variant="tonal"
                    icon="mdi-information"
                  >
                  谨慎开启 种子文件重命名, 会导致无法辅种和其他意料之外的问题, 增加种子维护难度
                  </v-alert>
                </v-window-item>
              </v-window>
            </v-card-text>
          </v-card>
        </v-form>
      </v-card-text>

      <v-card-actions class="px-2 py-1">
        <v-btn color="info" @click="emit('switch')" prepend-icon="mdi-view-dashboard" variant="text" size="small">数据页</v-btn>
        <v-spacer></v-spacer>
        <v-btn color="secondary" variant="text" @click="resetForm" prepend-icon="mdi-restore" size="small">恢复默认</v-btn>
        <v-btn color="primary" :disabled="!isFormValid || saving" @click="saveConfig" :loading="saving" prepend-icon="mdi-content-save" variant="text" size="small">保存配置</v-btn>
        <v-btn color="grey" @click="emit('close')" prepend-icon="mdi-close" variant="text" size="small">关闭</v-btn>
      </v-card-actions>
    </v-card>
  </div>
</template>

<style scoped>
/* 插件配置容器样式 */
.plugin-config {
  max-width: 80rem;
  margin: 0 auto;
  padding: 0.5rem;
}

/* 虚线框样式，用于包裹相关组件 */
.related-components {
  border: 1px dashed rgba(var(--v-theme-primary), 0.5);
  border-radius: 8px;
  margin: 1rem 5px;
  display: flex;
  flex-wrap: wrap;
}

/* 主色浅化 5 级的背景颜色样式 */
.bg-primary-lighten-5 {
  background-color: rgba(var(--v-theme-primary), 0.07);
}

/* 通用边框样式 */
.border {
  border: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}

/* 配置卡片样式 */
.config-card {
  /* 背景渐变，包含主背景和重复渐变效果 */
  background-image: linear-gradient(to right, rgba(var(--v-theme-surface), 0.98), rgba(var(--v-theme-surface), 0.95)),
                    repeating-linear-gradient(45deg, rgba(var(--v-theme-primary), 0.03), rgba(var(--v-theme-primary), 0.03) 10px, transparent 10px, transparent 20px);
  background-attachment: fixed;
  box-shadow: 0 1px 2px rgba(var(--v-border-color), 0.05) !important;
  transition: all 0.3s ease;
}

/* 配置卡片悬停样式 */
.config-card:hover {
  box-shadow: 0 3px 6px rgba(var(--v-border-color), 0.1) !important;
}

/* 设置项样式 */
.setting-item {
  border-radius: 8px;
  transition: all 0.2s ease;
  padding: 0.5rem;
  height: 100%;
  display: flex;
  align-items: center;
}

/* 设置项悬停样式 */
.setting-item:hover {
  background-color: rgba(var(--v-theme-primary), 0.03);
}

/* 设置项内选择器样式 */
.setting-item .v-select {
  flex-shrink: 1;
  min-width: 0;
}

/* 确保所有列的高度一致 */
.v-col {
  display: flex;
  align-items: stretch;
}

/* 小开关组件样式，调整大小和位置 */
.small-switch {
  transform: scale(0.8);
  margin-right: -8px;
  flex-shrink: 0;
}

/* 副标题 2 文本样式 */
.text-subtitle-2 {
  font-size: 0.875rem !important;
  font-weight: 500;
  white-space: nowrap;
  margin-right: 0.5rem;
}

/* 深度选择器，调整选择器输入区域样式 */
:deep(.v-select .v-field__input) {
  min-height: 32px;
  padding-top: 0;
  padding-bottom: 0;
}
</style>