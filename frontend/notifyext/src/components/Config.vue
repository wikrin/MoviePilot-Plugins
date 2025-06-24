<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue';
import { v4 as uuidv4 } from 'uuid';
import RuleCard from './cards/RuleCard.vue'
import TemplateCard from './cards/TemplateCard.vue'
import type { NotificationRule, TemplateConf, NotificationConf, FrameHandlerItem } from '../types';


const props = defineProps({
  api: {
    type: Object,
    required: true,
  },
  initialConfig: {
    type: Object,
    default: () => ({})
  }
});

const emit = defineEmits(['close', 'save']);

// 插件默认配置
const defaultConfig = reactive({
  enabled: false,
  cooldown: 0,
});

const config = reactive({ ...defaultConfig });

const rules = ref<NotificationRule[]>([]);
const templates = ref<TemplateConf[]>([]);

const showDialog = ref(false)
const dialogMessage = ref('')
let resolveConfirm: ((value: boolean | PromiseLike<boolean>) => void)
const activeTab = ref('rules');
const isFormValid = ref(true);
const form = ref<{ resetValidation: () => void } | null>(null);
const error = ref<string | null>(null);
const successMessage = ref<string | null>(null);
const saving = ref(false);

// 所有消息渠道
const notifications = ref<NotificationConf[]>([])
const frameitems = ref<FrameHandlerItem[]>([])

// 调用API查询通知渠道设置
async function loadNotificationSetting() {
  try {
    const result: { [key: string]: any } = await props.api.get('system/setting/Notifications')
    notifications.value = result.data?.value ?? []
  } catch (error) {
    console.log(error)
  }
}

// 调用API获取消息通知规则
async function loadNotificationRule() {
  try {
    const result = await props.api.get('plugin/NotifyExt/rules')
    rules.value = result ?? []
  } catch (error) {
    console.log(error)
  }
}

// 调用API保存规则
async function saveRules() {
  try {
    await props.api.post('plugin/NotifyExt/rules', rules.value)
    successMessage.value = '规则保存成功'
  } catch (error) {
    console.log(error)
  }
}

// 调用API获取消息模板
async function loadNotificationTemplate() {
  try {
    const result = await props.api.get('plugin/NotifyExt/templates')
    templates.value = result ?? []
  } catch (error) {
    console.log(error)
  }
}

// 调用API保存消息模板
async function saveTemplate() {
  try {
    await props.api.post('plugin/NotifyExt/templates', templates.value)
    successMessage.value = '模板保存成功'
  } catch (error) {
    console.log(error)
  }
}

// 调用API获取已实现调用帧项
async function loadFrameItems() {
  try {
    const result = await props.api.get('plugin/NotifyExt/frameitems')
    frameitems.value = result ?? []
  } catch (error) {
    console.log(error)
  }
}

// 通知状态
const snackbar = reactive({
  show: false,
  text: '',
  color: 'success',
  timeout: 3000,
})

// 显示通知
function showNotification(text, color = 'success') {
  snackbar.text = text
  snackbar.color = color
  snackbar.show = true
}

//  显示弹窗
function showConfirm(message: string): Promise<boolean> {
  dialogMessage.value = message
  showDialog.value = true
  return new Promise((resolve) => {
    resolveConfirm = resolve
  })
}

function onConfirm() {
showDialog.value = false
resolveConfirm(true)
}

function onCancel() {
showDialog.value = false
resolveConfirm(false)
}

// 计算消息模板选项
const templateOptions = computed(() => {
  return templates.value.map(item => {
    return {
      title: item.name,
      value: item.id,
    }
  })
})


// 生成唯一ID
function generateId() {
  return uuidv4();
}

// 添加新规则
function addNewRule() {
  let name = `规则${rules.value.length + 1}`
  while (rules.value.some(item => item.name === name)) {
    name = `规则${parseInt(name.split("规则")[1]) + 1}`
  }

  rules.value.push({
    id: generateId(),
    name: name,
    enabled: false,
    switch: '',
    type: '',
    target: '',
  });
}

// 保存规则回调
function handleSaveRule(rule, index) {
  if (index === null) {
    rules.value.push(rule);
  } else {
    rules.value[index] = rule;
  }
}

// 删除规则
function deleteRule(index) {
  rules.value.splice(index, 1);
}

// 添加新模板
function addNewTemplate() {
  let name = `模板${templates.value.length + 1}`
  while (templates.value.some(item => item.name === name)) {
    name = `模板${parseInt(name.split("模板")[1]) + 1}`
  }

  templates.value.push({
    id: generateId(),
    name: name,
    template: "{\n  'title': '',\n  'text': ''\n}"
  });
}

// 保存模板回调
function handleSaveTemplate(template, index) {
  if (index === null) {
    templates.value.push(template);
  } else {
    templates.value[index] = template;
  }
}

// 删除模板
async function deleteTemplate(index) {
  const template = templates.value[index]

  // 查找并清理引用该模板的规则
  const referencedRules = rules.value.filter(rule =>
    ['organizeSuccess', 'downloadAdded', 'subscribeAdded', 'subscribeComplete']
      .some(field => rule[field] === template.id)
  )

  if (referencedRules.length > 0) {
    const confirmed = await showConfirm(
      `该模板正在以下 ${referencedRules.length} 条规则中被引用：\n${referencedRules.map(r => r.name).join(', ')}\n\n确定要删除吗？`
    )
    if (!confirmed) return
  }

  // 清空规则中的模板引用
  rules.value.forEach((rule, idx) => {
    const field = ['organizeSuccess', 'downloadAdded', 'subscribeAdded', 'subscribeComplete']
      .find(f => rule[f] === template.id)

    if (field) {
      rules.value[idx] = { ...rule, [field]: undefined }
    }
  })

  // 删除单个模板
  templates.value.splice(index, 1)
}

// 保存配置
async function saveConfig() {

  saving.value = true;
  error.value = null;

  try {
    // 保存规则和模板
    await saveRules();
    await saveTemplate();
    //  保存配置
    emit('save', { ...config });
    successMessage.value = '配置保存成功';
  } catch (err) {
    console.error('保存配置失败:', err);
    error.value = err.message || '保存配置失败';
  } finally {
    saving.value = false;
  }
}

// 重置表单
function resetForm() {
  Object.keys(defaultConfig).forEach(key => {
    config[key] = defaultConfig[key];
  });

  if (form.value) {
    form.value.resetValidation();
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
  // 加载通知渠道设置
  loadNotificationSetting()
  // 加载消息模板
  loadNotificationTemplate()
  // 加载消息通知分发规则
  loadNotificationRule()
  // 加载调用帧处理器
  loadFrameItems()
})
</script>

<template>
  <div class="plugin-config">
    <v-card flat class="rounded border">
      <!-- 标题区域 -->
      <v-card-title class="text-subtitle-1 d-flex align-center px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-cog" class="mr-2" color="primary" size="small" />
        <span>消息通知扩展配置</span>
      </v-card-title>
      <v-card-text class="px-3 py-2">
        <v-alert v-if="error" type="error" density="compact" class="mb-2 text-caption" variant="tonal" closable>{{ error }}</v-alert>
        <v-alert v-if="successMessage" type="success" density="compact" class="mb-2 text-caption" variant="tonal" closable>{{ successMessage }}</v-alert>

        <v-form ref="form" v-model="isFormValid" @submit.prevent="saveConfig">
          <!-- 新增的选项卡组件 -->
          <v-card flat class="rounded mb-3 border config-card">
            <v-card-text class="px-3 py-2">
              <v-row dense no-gutters>
                <!-- 插件开关 -->
                <v-col cols="12" md="6" class="pr-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <v-icon icon="mdi-power" size="small" :color="config.enabled ? 'info' : 'grey'" class="mr-3" />
                      <span class="text-subtitle-2">启用插件</span>
                    </div>
                    <v-switch
                      v-model="config.enabled"
                      color="info"
                      inset
                      density="compact"
                      hide-details
                      class="small-switch"
                    />
                  </div>
                </v-col>
                <!-- 重复消息控制 -->
                <v-col cols="12" md="6" class="pl-md-2 pb-2">
                  <div class="setting-item d-flex align-center justify-space-between pa-3 bg-grey-lighten-3 rounded">
                    <div class="d-flex align-center">
                      <span class="text-subtitle-2">重复消息控制(分钟)</span>
                    </div>
                    <v-text-field
                      v-model.number="config.cooldown"
                      density="compact"
                      hide-details
                      style="max-width: 150px"
                      @input="(value) => config.cooldown = value.replace(/[^0-9]/g, '')"
                    />
                  </div>
                </v-col>
              </v-row>
            </v-card-text>
          </v-card>

          <v-card flat class="rounded mb-3 border config-card">
            <v-card-text class="px-3 py-2">
              <v-tabs v-model="activeTab" class="mb-3" density="compact" fixed-tabs>
                <v-tab value="rules">消息规则</v-tab>
                <v-tab value="templates">消息模板</v-tab>
              </v-tabs>

              <v-window v-model="activeTab">
                <!-- 分发规则选项卡内容 -->
                <v-window-item value="rules">
                  <div class="rules-tab">
                    <v-row v-if="rules && rules.length > 0">
                      <v-col
                        v-for="(rule, index) in rules"
                        :key="rule.id"
                        cols="12"
                        md="6"
                        lg="4"
                      >
                        <RuleCard
                          :rule="rule"
                          :index="index"
                          :notifications="notifications"
                          :rules="rules"
                          :templates="templateOptions"
                          :frameitems="frameitems"
                          @alert="showNotification"
                          @save="handleSaveRule"
                          @delete="deleteRule"
                        />
                      </v-col>
                    </v-row>

                    <v-row v-else>
                      <v-col cols="12">
                        <v-alert type="info" variant="tonal" icon="mdi-information">
                          暂无规则，请点击下方按钮添加新规则
                        </v-alert>
                      </v-col>
                    </v-row>
                    <VCardText>
                      <VForm @submit.prevent="() => {}">
                        <VBtnGroup density="comfortable" class="d-flex flex-wrap gap-4 mt-4">
                          <VBtn color="success" variant="tonal" @click="addNewRule">
                            <VIcon icon="mdi-plus" />
                          </VBtn>
                        </VBtnGroup>
                      </VForm>
                    </VCardText>
                  </div>
                </v-window-item>

                <!-- 消息模板选项卡内容 -->
                <v-window-item value="templates">
                  <div class="templates-tab">
                    <v-row v-if="templates && templates.length > 0">
                      <v-col
                        v-for="(template, index) in templates"
                        :key="template.id"
                        cols="12"
                        md="6"
                        lg="4"
                      >
                        <TemplateCard
                          :template="template"
                          :index="index"
                          :templates="templates"
                          @alert="showNotification"
                          @save="handleSaveTemplate"
                          @delete="deleteTemplate"
                        />
                      </v-col>
                    </v-row>

                    <v-row v-else>
                      <v-col cols="12">
                        <v-alert type="info" variant="tonal" icon="mdi-information">
                          暂无消息模板，请点击下方按钮添加新模板
                        </v-alert>
                      </v-col>
                    </v-row>
                    <VCardText>
                      <VForm @submit.prevent="() => {}">
                        <VBtnGroup density="comfortable" class="d-flex flex-wrap gap-4 mt-4">
                          <VBtn color="success" variant="tonal" @click="addNewTemplate">
                            <VIcon icon="mdi-plus" />
                          </VBtn>
                        </VBtnGroup>
                      </VForm>
                    </VCardText>
                  </div>
                </v-window-item>
              </v-window>
            </v-card-text>
          </v-card>
        </v-form>
      </v-card-text>

      <!-- 确认框 -->
      <v-dialog v-model="showDialog" max-width="400">
        <v-card>
        <v-card-title class="text-h6">确认操作</v-card-title>
        <v-card-text>{{ dialogMessage }}</v-card-text>
        <v-card-actions>
            <v-spacer></v-spacer>
            <v-btn color="grey" text @click="onCancel">取消</v-btn>
            <v-btn color="primary" @click="onConfirm">确定</v-btn>
        </v-card-actions>
        </v-card>
      </v-dialog>

    <!-- 通知弹窗 -->
    <v-snackbar v-model="snackbar.show" :color="snackbar.color" :timeout="snackbar.timeout">
      {{ snackbar.text }}
      <template v-slot:actions>
        <v-btn variant="text" @click="snackbar.show = false"> 关闭 </v-btn>
      </template>
    </v-snackbar>

      <v-card-actions class="px-2 py-1">
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

/* 卡片样式 */
.rule-card, .template-card {
  transition: all 0.3s ease;
  height: 100%;
  display: flex;
  flex-direction: column;
  cursor: pointer;
}

.rule-card:hover, .template-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
}

/* JSON编辑器错误样式 */
.json-error {
  background-color: #ffebee !important;
}

/* 规则/模板列表布局 */
.templates-tab,
.rules-tab {
  min-height: 400px;
}

/* 主色浅化 5 级的背景颜色样式 */
.bg-primary-lighten-5 {
  background-color: rgba(var(--v-theme-primary), 0.07);
}

/* 通用边框样式 */
.border {
  border: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
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

/* 副标题 2 文本样式 */
.text-subtitle-2 {
  font-size: 0.875rem !important;
  font-weight: 500;
  white-space: nowrap;
  margin-right: 0.5rem;
}
</style>