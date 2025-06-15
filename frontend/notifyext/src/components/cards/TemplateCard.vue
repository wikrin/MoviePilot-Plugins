<script setup lang="ts">
import { ref, watch } from 'vue';

const props = defineProps({
  template: {
    type: Object,
    required: true
  },
  index: {
    type: Number,
    required: true
  },
  templates: {
    type: Array,
    required: true
  }
});

const emit = defineEmits(['save', 'delete', 'alert']);

const showDialog = ref(false);
const editingTemplate = ref();
const editingIndex = ref<number | null>(null);

function openDialog() {
  editingTemplate.value = { ...props.template };
  editingIndex.value = props.index;
  showDialog.value = true;
}

function save() {
  if (!editingTemplate.value.name.trim()) {
    emit('alert', '模板名称不能为空', 'error')
    return
  }

  emit('save', editingTemplate.value, editingIndex.value);
  showDialog.value = false
}

function cancel() {
  showDialog.value = false
}

watch(() => props.template, (newVal) => {
  editingTemplate.value = { ...newVal }
})
</script>

<template>
  <v-card class="template-card rounded-lg" elevation="2" @click="openDialog">
    <v-card-title class="d-flex justify-space-between align-center py-2 px-3 bg-grey-lighten-3">
      <span>{{ template.name }}</span>
      <v-btn icon="mdi-delete" size="x-small" color="grey" @click.stop="$emit('delete', index)" />
    </v-card-title>

    <!-- 编辑弹窗 -->
    <v-dialog v-model="showDialog" max-width="50rem" scrollable>
      <v-card v-if="editingTemplate">
        <v-card-title class="bg-primary-lighten-5">编辑模板 {{ editingTemplate.name }}</v-card-title>
        <v-divider></v-divider>
        <v-card-text class="py-4">
          <v-form @submit.prevent="save">
            <v-row dense>
              <!-- 模板名称 -->
              <v-col cols="12">
                <v-text-field
                  v-model="editingTemplate.name"
                  label="模板名称"
                  required
                  dense
                  outlined
                />
              </v-col>
            </v-row>
              <!-- JSON编辑器 -->
            <v-card>
              <v-card-title>模板内容</v-card-title>
              <v-card-text class="py-0">
                <V-ace-editor
                  v-model:value="editingTemplate.template"
                  lang="json"
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
.template-card {
  cursor: pointer;
  transition: all 0.3s ease;
}
.template-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
}
.json-error {
  background-color: #ffebee !important;
}
</style>