<script setup>
import { computed } from 'vue'
import RuleEditor from './RuleEditor.vue'
import { DEFAULT_RULES } from '../api/torrentformatter.js'

const props = defineProps({
  section: { type: String, required: true },
  config: { type: Object, required: true },
  downloaders: { type: Array, required: true },
  loadingDownloaders: Boolean,
  disabled: Boolean,
})
const emit = defineEmits(['save'])
const naming = computed({
  get: () => Object.fromEntries(Object.keys(DEFAULT_RULES).map(key => [key, props.config[key]])),
  set: value => Object.assign(props.config, value),
})
</script>

<template>
  <v-form :disabled="disabled" @submit.prevent="emit('save')">
    <template v-if="section === 'automation'">
      <div class="tf-heading">
        <div>
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
            :items="downloaders.map(item => ({ title: item.name, value: item.name }))"
            :disabled="!config.cron_enabled || loadingDownloaders"
            :loading="loadingDownloaders"
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
    </template>
    <template v-else-if="section === 'exclusions'">
      <div class="tf-heading">
        <div>
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
          <h2>全局命名规则</h2>
          <p>这些规则用于自动任务，也是单次处理的默认起点。变量与 MoviePilot 整理命名保持一致。</p>
        </div>
      </div>
      <RuleEditor v-model="naming" :temporary="false" :disabled="disabled" />
    </template>
  </v-form>
</template>
