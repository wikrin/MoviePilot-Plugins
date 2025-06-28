<script setup lang="ts">
import { computed, PropType, ref, watch } from 'vue'
import type { FrameHandlerItem, NotificationConf, NotificationRule } from '../../types'

const props = defineProps({
  rule: {
    type: Object as PropType<NotificationRule>,
    required: true
  },
  index: {
    type: Number,
    required: true
  },
  notifications: {
    type: Object as PropType<NotificationConf[]>,
    default: () => []
  },
  rules: {
    type: Array<NotificationRule>,
    required: true
  },
  templates: {
    type: Array,
    default: () => []
  },
  frameitems: {
    type: Array as PropType<FrameHandlerItem[]>,
    default: () => []
  }
})

const emit = defineEmits(['save', 'delete', 'alert'])

const showDialog = ref(false)
const editingRule = ref({ ...props.rule })
const editingIndex = ref<number | null>(null)

const previousRegexYamlContent = ref('')
const previousFrameYamlContent = ref('')


// 计算消息渠道选项
const sourceOptions = computed(() => {
  return [
    { title: '跟随系统', value: '', subtitle: '使用系统消息渠道配置' },
    ...props.notifications.map(item => ({
      title: item.name,
      value: item.name,
      subtitle: item.type,
    })),
  ]
})


// 消息通知类型
const notificationSwitchs = [
  { title: '跟随系统', value: '', disabled: false },
  { value: '资源下载', title: '资源下载', disabled: false },
  { value: '整理入库', title: '整理入库', disabled: false },
  { value: '订阅', title: '订阅', disabled: false },
  { value: '站点', title: '站点', disabled: false },
  { value: '媒体服务器', title: '媒体服务器', disabled: false },
  { value: '手动处理', title: '手动处理', disabled: false },
  { value: '插件', title: '插件', disabled: false },
  { value: '其它', title: '其它', disabled: false },
]

// 根据 editingRule.target 动态过滤并标记不可用的通知类型
const filteredNotificationSwitchs = computed(() => {
  if (!editingRule.value.target) {
    return notificationSwitchs
  }

  const selectedNotification = props.notifications.find(n => n.name === editingRule.value.target)

  let availableTypes: string[] = []
  if (selectedNotification && selectedNotification.switchs) {
    availableTypes = selectedNotification.switchs
  }

  return [
  ...notificationSwitchs.map(item => ({
    ...item,
    disabled: item.value !== '' && item.value !== undefined && !availableTypes.includes(item.value as string),
  }))
]
})

// 根据 notificationSwitchs 的选择动态调整 ruleTypeItems
const RuleTypeItems = computed(() => {
  // 从 frameItems 中筛选出匹配当前 switch 的项
  const matchedFrameItems = props.frameitems.filter(item => item.switch === editingRule.value.switch)

  const baseItems = [
    { title: '正则匹配', value: 'regex', subtitle: '使用正则表达式提取信息' },
    { title: '上下文模式', value: 'frame', subtitle: '使用调用帧(Frame)获取局部变量' },
    ...matchedFrameItems,
  ]

  const switchMap = {
  '资源下载': [{ title: '资源下载', value: 'downloadAdded' }],
  '整理入库': [{ title: '资源入库', value: 'organizeSuccess' }],
  '订阅': [
    { title: '订阅添加', value: 'subscribeAdded' },
    { title: '订阅完成', value: 'subscribeComplete' }
  ]
}

const baseSlice = baseItems.slice(0, 1)
const matchedItem = switchMap[editingRule.value.switch]

if (matchedItem) {
  return [...matchedItem, ...baseSlice]
}

return baseItems
})

// 默认 YAML 内容
const defaultRegexYamlContent = `
# extractors 中 除field外, 其余所有字段都将作为消息模板中的可用参数
# MetaBase 如果需要获取媒体信息必须绑定title, 否则消息模板中的可用参数仅有extractors中匹配的字段
# 详见: https://github.com/wikrin/MoviePilot-Plugins/blob/main/frontend/notifyext/README.md
extractors:
  - field: 'title'
    org_msg_title: '.*'

MetaBase:
  - title: ''
`

// 默认 YAML 内容
const defaultFrameYamlContent = `
# 详见: https://github.com/wikrin/MoviePilot-Plugins/blob/main/frontend/notifyext/README.md
Frame:
  cls_name: ''      # 目标帧类名
  method_name: ''   # 帧方法名
  depth: 15         # 最大遍历深度
  skip: 2           # 跳过层数
  required: []      # 目标局部变量中要存在的变量名
`

function openDialog() {
  editingIndex.value = props.index
  showDialog.value = true
}

function validateRuleName(rule) {
  const name = rule.name?.trim()
  if (!name) return

  const count = props.rules.filter(r => r.name === name).length
  if (count > 1) {
    rule.duplicateName = true
  } else {
    delete rule.duplicateName
  }
}

function save() {
  if (!editingRule.value.name.trim()) {
    emit('alert', '规则名称不能为空', 'error')
    return
  }

  if (!editingRule.value.type?.trim()) {
    emit('alert', '规则类型不能为空', 'error')
    return
  }

  if (!editingRule.value.template_id?.trim()) {
    emit('alert', '未选择模板, 将使用系统默认', 'warning')
  }

emit('save', editingRule.value, editingIndex.value)
  showDialog.value = false
}

function cancel() {
  // 还原已编辑的内容并关闭对话框
  editingRule.value = { ...props.rule }
  showDialog.value = false
}

watch(() => props.rule, (newVal) => {
  editingRule.value = { ...newVal }
})

// 监听场景开关变化
watch(() => editingRule.value.switch, (newSwitch, oldSwitch) => {
  if (oldSwitch !== undefined && newSwitch !== oldSwitch) {
    // 获取当前 RuleTypeItems 中的所有可用 value
    const availableTypes = RuleTypeItems.value.map(item => item.value)

    // 如果当前 type 不在可用列表中，则清空
    if (!availableTypes.includes(editingRule.value.type)) {
      editingRule.value.type = undefined
    }
  }
})

// 监听 type 变化
watch(() => editingRule.value.type, (newType, oldType) => {
  // 类型映射表：根据 type 设置对应的 switch 中文名称
  const switchMap = {
    downloadAdded: '资源下载',
    organizeSuccess: '整理入库',
    subscribeAdded: '订阅',
    subscribeComplete: '订阅'
  }

  // 更新通知类型（switch）
  const switchValue = switchMap[newType as keyof typeof switchMap]
  if (switchValue) {
    editingRule.value.switch = switchValue
    editingRule.value.yaml_content = ''
  }

  // 存储旧的 YAML 内容到临时变量（用于切换回该类型时恢复）
  if (oldType === 'regex' && editingRule.value.yaml_content) {
    previousRegexYamlContent.value = editingRule.value.yaml_content
  } else if (oldType === 'frame' && editingRule.value.yaml_content) {
    previousFrameYamlContent.value = editingRule.value.yaml_content
  }

  // 根据新类型加载默认或之前的内容
  if (newType === 'regex' && previousRegexYamlContent.value) {
    editingRule.value.yaml_content = previousRegexYamlContent.value
  } else if (newType === 'frame' && previousFrameYamlContent.value) {
    editingRule.value.yaml_content = previousFrameYamlContent.value
  } else {
    editingRule.value.yaml_content = {
      regex: defaultRegexYamlContent,
      frame: defaultFrameYamlContent
    }[newType as 'regex' | 'frame'] || ''
  }})

// 当 target 变化时，自动更新 switchs（如果当前选择的类型不在新的渠道支持中，则重置）
watch(() => editingRule.value.target, (newTarget) => {
  const selectedNotification = props.notifications.find(n => n.name === newTarget)
  let availableTypes: string[] = []
  if (selectedNotification && selectedNotification.switchs) {
    availableTypes = selectedNotification.switchs
  }

  // 如果当前 switch 不在可用列表中，则重置为系统选项（空字符串）
  if (editingRule.value.switch && !availableTypes.includes(editingRule.value.switch)) {
    editingRule.value.switch = ''
  }
})
</script>

<template>
  <v-card class="rule-card rounded-lg" elevation="2" @click="openDialog">
    <v-card-title class="d-flex justify-space-between align-center py-2 px-3 bg-grey-lighten-3">
      <span>{{ rule.name }}</span>
      <v-btn icon="mdi-delete" size="x-small" color="grey" @click.stop="$emit('delete', index)" />
    </v-card-title>

    <!-- 编辑弹窗 -->
    <v-dialog v-model="showDialog" max-width="50rem" scrollable>
      <v-card v-if="editingRule">
        <v-card-title class="bg-primary-lighten-5">编辑规则 {{editingRule.name}}</v-card-title>
        <v-divider></v-divider>
        <v-card-text class="py-4">
          <v-form @submit.prevent="save">
            <v-container fluid>
              <h3 class="text-h6 mb-3">基础配置</h3>
              <v-row>
                <!-- 配置开关 -->
                <v-col cols="12" md="4">
                  <v-switch
                    v-model="editingRule.enabled"
                    label="启用规则"
                    inset
                  />
                </v-col>
                <!-- 配置名 -->
                <v-col cols="12" md="4">
                  <v-text-field
                    v-model="editingRule.name"
                    label="配置名"
                    @input="validateRuleName(editingRule)"
                    required
                    outlined
                  />
                </v-col>
                <!-- 目标渠道 -->
                <v-col cols="12" md="4">
                  <v-select
                    v-model="editingRule.target"
                    :items="sourceOptions"
                    :item-props="item => ({
                      subtitle: item.subtitle,
                    })"
                    label="目标渠道"
                    clearable
                    outlined
                  />
                </v-col>
              </v-row>
              <v-row>
                <v-col cols="12" md="4">
                  <v-select
                    v-model="editingRule.switch"
                    :items="filteredNotificationSwitchs"
                    item-title="title"
                    item-value="value"
                    :item-props="item => ({
                      disabled: item.disabled,
                      subtitle: item.subtitle,
                    })"
                    label="通知类型"
                    outlined
                  />
                </v-col>
                <v-col cols="12" md="4">
                  <v-select
                    v-model="editingRule.type"
                    :items="RuleTypeItems"
                    :item-props="item => ({
                      subtitle: item.subtitle,
                    })"
                    label="规则类型"
                    outlined
                  />
                </v-col>
                <!-- 模板ID -->
                <v-col cols="12" md="4">
                  <v-select
                    v-model="editingRule.template_id"
                    :items="templates"
                    label="选择模板"
                    clearable
                    outlined
                  />
                </v-col>
              </v-row>
              <v-divider></v-divider>
              <h3 class="text-h6 py-6">消息聚合配置</h3>
              <v-row>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="editingRule.aggregate.exclude"
                    label="排除(关键字、正则表达式)"
                    required
                    outlined
                  />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="editingRule.aggregate.include"
                    label="包含(关键字、正则表达式)"
                    required
                    outlined
                  />
                </v-col>
                <v-col cols="12" md="3">
                  <v-text-field
                    v-model="editingRule.aggregate.wait_time"
                    label="等待时间(分钟)"
                    type="number"
                    oninput="this.value = this.value.replace(/[^0-9]/g, '')"
                    required
                    outlined
                  />
                </v-col>
                <v-col cols="12" md="9">
                  <v-alert type="info">
                    等待时间内未收到符合条件的新消息时,将发送已聚合消息。
                </v-alert>
                </v-col>
              </v-row>
            </v-container>

            <v-card v-show="editingRule.type === 'regex' || editingRule.type === 'frame'">
              <v-card-title>YAML 编辑</v-card-title>
              <v-card-text class="py-0">
                <V-ace-editor
                  v-model:value="editingRule.yaml_content"
                  lang="yaml"
                  class="w-full h-full min-h-[30rem] rounded"
                />
              </v-card-text>
            </v-card>
          </v-form>
        </v-card-text>
        <v-divider></v-divider>
        <v-card-actions>
          <v-spacer></v-spacer>
          <v-btn color="grey" @click="cancel">取消</v-btn>
          <v-btn color="primary" @click="save">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-card>
</template>


<style scoped>
.rule-card {
  cursor: pointer;
  transition: all 0.3s ease
}
.rule-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1)
}
</style>