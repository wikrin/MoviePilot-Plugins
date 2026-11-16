<script setup>
import { DEFAULT_RULES } from '../api/torrentformatter.js'

const props = defineProps({
  modelValue: { type: Object, required: true },
  disabled: Boolean,
  capabilities: { type: Object, default: null },
  temporary: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue'])
function update(key, value) {
  emit('update:modelValue', {
    ...DEFAULT_RULES,
    ...props.modelValue,
    [key]: key.startsWith('format_') ? value || '' : Boolean(value),
  })
}
</script>

<template>
  <div class="tf-card">
    <div class="tf-card-header">
      <h3>命名与目录规则</h3>
      <span class="tf-source-badge">{{ temporary ? '仅本次生效' : '全局自动处理规则' }}</span>
    </div>
    <div class="tf-card-body">
      <div class="tf-rule-section">
        <v-textarea
          class="tf-rule-field"
          :model-value="modelValue.format_save_path"
          label="保存路径追加模板"
          :disabled="disabled"
          :maxlength="8192"
          rows="2"
          auto-grow
          clearable
          persistent-hint
          hint="追加到原保存路径并去除重复目录；留空不移动。模板必须是相对路径。"
          variant="outlined"
          density="compact"
          @update:model-value="update('format_save_path', $event)"
        />
      </div>
      <div class="tf-rule-section">
        <div class="tf-rule-row">
          <div class="tf-rule-copy">
            <strong>种子显示名</strong>
            <p>修改下载器界面中的名称，不是文件夹名称。</p>
          </div>
          <v-switch
            :model-value="modelValue.rename_torrent"
            :disabled="disabled"
            inset
            color="primary"
            hide-details
            density="compact"
            aria-label="修改种子显示名"
            @update:model-value="update('rename_torrent', $event)"
          />
        </div>
        <v-textarea
          class="tf-rule-field"
          :model-value="modelValue.format_torrent_name"
          label="种子显示名模板"
          :disabled="disabled || !modelValue.rename_torrent"
          :maxlength="8192"
          rows="2"
          auto-grow
          clearable
          hint="使用宿主的 Jinja2 变量，不允许目录分隔符。"
          persistent-hint
          variant="outlined"
          density="compact"
          @update:model-value="update('format_torrent_name', $event)"
        />
        <p v-if="capabilities && !capabilities.rename_torrent" class="mb-0">
          Transmission 不支持独立显示名。本项会在预览中标为不支持，不会冒充修改成功。
        </p>
      </div>
      <div class="tf-rule-row">
        <div class="tf-rule-copy">
          <strong>文件路径重命名</strong>
          <p>实验功能，会改变种子内容布局，可能影响辅种与整理记录关联。</p>
        </div>
        <v-switch
          :model-value="modelValue.rename_file"
          :disabled="disabled"
          inset
          color="warning"
          hide-details
          density="compact"
          aria-label="重命名种子文件路径"
          @update:model-value="update('rename_file', $event)"
        />
      </div>
      <v-textarea
        class="tf-rule-field"
        :model-value="modelValue.format_movie_path"
        label="电影相对文件路径模板"
        :disabled="disabled || !modelValue.rename_file"
        :maxlength="8192"
        rows="3"
        auto-grow
        clearable
        variant="outlined"
        density="compact"
        @update:model-value="update('format_movie_path', $event)"
      />
      <v-textarea
        class="tf-rule-field"
        :model-value="modelValue.format_tv_path"
        label="剧集相对文件路径模板"
        :disabled="disabled || !modelValue.rename_file"
        :maxlength="8192"
        rows="3"
        auto-grow
        clearable
        persistent-hint
        hint="逐文件解析季集；缺少集数的文件会跳过，重名/越界目标会阻止执行。"
        variant="outlined"
        density="compact"
        @update:model-value="update('format_tv_path', $event)"
      />
      <v-alert v-if="modelValue.rename_file" variant="tonal" type="warning" density="compact" class="mt-3">{{
        capabilities && !capabilities.file_move
          ? '此下载器仅支持同目录文件改名，跨目录布局会保留原状。'
          : '请检查每个文件的预计新路径。不会覆盖现有文件，也不会猜测集数。'
      }}</v-alert>
    </div>
  </div>
</template>
