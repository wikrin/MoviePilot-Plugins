<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { type NotificationRule } from '../../types'

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
  categories: {
    type: Object,
    default: () => {}
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
const mediaTypeItems = [
  { title: '全部', value: '' },
  { title: '电影', value: '电影' },
  { title: '电视剧', value: '电视剧' },
]


function openDialog() {
  editingIndex.value = props.index
  showDialog.value = true
}

// 根据选中的媒体类型，获取对应的媒体类别
const getCategories = computed(() => {
  const default_value = []
  if (!props.categories || !props.categories[editingRule.value.media_type ?? '']) return default_value
  return default_value.concat(props.categories[editingRule.value.media_type ?? ''])
})

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
              <!-- 媒体类型 -->
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.media_type"
                  :items="mediaTypeItems"
                  label="媒体类型"
                  clearable
                  outlined
                />
              </v-col>

              <!-- 媒体类别 -->
              <v-col cols="12" md="6" v-if="editingRule.media_type">
                <v-combobox
                  v-model="editingRule.media_category"
                  :items="getCategories"
                  label="媒体类别"
                  multiple
                  chips
                  deletable-chips
                  clearable
                  outlined
                />
              </v-col>
            </v-row>

            <v-row>
              <!-- 入库成功 -->
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.organizeSuccess"
                  :items="templates"
                  label="入库成功模板"
                  clearable
                  outlined
                />
              </v-col>

              <!-- 下载添加 -->
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.downloadAdded"
                  :items="templates"
                  label="下载添加模板"
                  clearable
                  outlined
                />
              </v-col>
            </v-row>

            <v-row>
              <!-- 订阅添加 -->
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.subscribeAdded"
                  :items="templates"
                  label="订阅添加模板"
                  clearable
                  outlined
                />
              </v-col>

              <!-- 订阅完成 -->
              <v-col cols="12" md="6">
                <v-select
                  v-model="editingRule.subscribeComplete"
                  :items="templates"
                  label="订阅完成模板"
                  clearable
                  outlined
                />
              </v-col>
          </v-row>
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