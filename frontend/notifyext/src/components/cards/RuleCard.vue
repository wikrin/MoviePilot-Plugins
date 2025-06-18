<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { NotificationRule, templateConf } from '../../types'

const props = defineProps({
  rule: {
    type: Object,
    required: true
  },
  index: {
    type: Number,
    required: true
  },
  sourceOptions: {
    type: Array,
    default: () => []
  },
  rules: {
    type: Array<NotificationRule>,
    required: true
  },
  templates: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['save', 'delete', 'alert'])

const showDialog = ref(false)
const editingRule = ref({ ...props.rule })
const editingIndex = ref<number | null>(null)

// 媒体类型字典
const ruleTypeItems = [
  { title: '资源入库', value: 'organizeSuccess' },
  { title: '资源下载', value: 'downloadAdded' },
  { title: '添加订阅', value: 'subscribeAdded' },
  { title: '订阅完成', value: 'subscribeComplete' },
  { title: '正则匹配', value: 'regex' },
]
// 【站点 观众 消息】
// 时间：11时17分前
// 标题：种子被删除
// 内容：
// 你下载的种子'Soul Land S02E76 2023 2160p WEB-DL H265 DDP2.0-ADWeb'被管理员删除。原因：已打包合集，清理单集。
// 查看详情
// 默认 YAML 内容
const defaultYamlContent = `
# extractors 中 除field外, 其余所有字段都将作为消息模板中的可用参数
# MetaBase 如果需要获取媒体信息必须绑定title, 否则消息模板中的可用参数仅有extractors中匹配的字段
# 详见: https://github.com/wikrin/MoviePilot-Plugins/blob/main/frontend/notifyext/README.md
extractors:
  - field: 'title'
    org_msg_title: '.*'

MetaBase:
  - title: ''
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

  if (!editingRule.value.target) {
    emit('alert', '未设置消息渠道', 'error')
    return
  }

emit('save', editingRule.value, editingIndex.value)
  showDialog.value = false
}

function cancel() {
  showDialog.value = false
}

// 当媒体类型发生变化时，清空媒体类别
watch(() => editingRule.value.media_type, () => {
  editingRule.value.media_category = undefined
})

watch(() => props.rule, (newVal) => {
  editingRule.value = { ...newVal }
})

// 监听 type 变化
watch(() => editingRule.value.type, (newType, oldType) => {
  if (!oldType) return
  if (newType === 'regex' && oldType !== 'regex') {
    editingRule.value.yaml_content = defaultYamlContent
  } else {
      editingRule.value.yaml_content = ''
    }
  },
  { immediate: true }
)
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
            <v-row>
              <!-- 配置开关 -->
              <v-col cols="12" md="6">
                <v-switch
                  v-model="editingRule.enabled"
                  label="启用规则"
                  inset
                />
              </v-col>
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.type"
                  :items="ruleTypeItems"
                  label="规则类型"
                  outlined
                />
              </v-col>
            </v-row>
            <v-row>
              <!-- 配置名 -->
              <v-col cols="12" md="6">
                <v-text-field
                  v-model="editingRule.name"
                  label="配置名"
                  @input="validateRuleName(editingRule)"
                  required
                  outlined
                />
              </v-col>

              <!-- 目标渠道 -->
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.target"
                  :items="sourceOptions"
                  label="目标渠道"
                  clearable
                  outlined
                />
              </v-col>
            </v-row>

            <v-row>
              <!-- 模板ID -->
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.template_id"
                  :items="templates"
                  label="选择模板"
                  clearable
                  outlined
                />
              </v-col>
            </v-row>

            <v-card v-show="editingRule.type === 'regex'">
              <v-card-title>模板内容</v-card-title>
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