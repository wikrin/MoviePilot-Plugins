<script setup lang="ts">
// 统一设置行结构：桌面说明/控件分栏，窄屏控件换行；switch 保持右侧紧凑布局。
withDefaults(defineProps<{
  title: string
  description?: string
  control?: 'default' | 'switch'
  last?: boolean
}>(), { control: 'default', last: false })
</script>

<template>
  <div class="ct-setting-row" :class="{ 'ct-setting-row--last': last }">
    <div class="ct-setting-copy">
      <strong>{{ title }}</strong>
      <p v-if="description">{{ description }}</p>
    </div>
    <div class="ct-setting-control" :class="{ 'ct-setting-control--switch': control === 'switch' }">
      <slot />
    </div>
  </div>
</template>
