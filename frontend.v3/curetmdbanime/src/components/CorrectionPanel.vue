<script setup lang="ts">
import { computed } from 'vue'
import type { Config } from '../api'
import SettingRow from './SettingRow.vue'
import SurfaceCard from './SurfaceCard.vue'
const props = defineProps<{
  modelValue: Config
  integer: (min: number, max: number) => (v: unknown) => string | boolean
}>()
const emit = defineEmits(['update:modelValue'])
const config = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
</script>

<template>
  <SurfaceCard kicker="ANIME CORRECTION" title="修正策略" icon="mdi-tune-variant" body-class="ct-settings-list">
    <SettingRow
      title="启用元数据修正"
      description="在动画识别、搜索、订阅及整理环节按逻辑季集映射调整季集。"
      control="switch"
    >
      <v-switch v-model="config.enable_correction" color="primary" hide-details inset aria-label="启用元数据修正" />
    </SettingRow>
    <SettingRow
      title="按播出窗口推断季号"
      description="标题默认落入 S01 且有资源发布时间时，尝试匹配目标季的播出窗口。"
      control="switch"
    >
      <v-switch
        v-model="config.assume_season_by_window"
        color="primary"
        hide-details
        inset
        aria-label="按播出窗口推断季号"
      />
    </SettingRow>
    <SettingRow title="最新季越界宽限（集）" description="允许最新季映射结果在边界外额外延伸的集数。">
      <v-text-field
        v-model.number="config.grace_episodes"
        type="number"
        hide-details="auto"
        density="compact"
        variant="outlined"
        aria-label="最新季越界宽限（集）"
        :rules="[integer(0, 5)]"
      />
    </SettingRow>
    <SettingRow
      title="制作周期切分间隔（天）"
      description="逐集播出间隔超过此值时划分为新制作周期，默认 70 天。"
      last
    >
      <v-text-field
        v-model.number="config.production_cycle_gap_days"
        type="number"
        hide-details="auto"
        density="compact"
        variant="outlined"
        aria-label="制作周期切分间隔（天）"
        :rules="[integer(20, 365)]"
      />
    </SettingRow>
  </SurfaceCard>
</template>
