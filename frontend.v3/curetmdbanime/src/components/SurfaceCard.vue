<script setup lang="ts">
// 统一卡片材质；保留原生 div 与 Vuetify 两种结构，让宿主默认间距和插槽行为保持一致。
withDefaults(defineProps<{
  vuetify?: boolean
  variant?: string
  rounded?: boolean | string
  kicker?: string
  title?: string
  icon?: string
  headerClass?: string
  bodyClass?: string
}>(), { vuetify: false })

defineOptions({ inheritAttrs: false })
</script>

<template>
  <component
    :is="vuetify ? 'v-card' : 'div'"
    v-bind="$attrs"
    :variant="vuetify ? variant : undefined"
    :rounded="vuetify ? rounded : undefined"
    :class="vuetify ? 'ct-surface' : 'ct-card ct-surface app-surface app-surface-static'"
  >
    <component
      :is="vuetify ? 'v-card-title' : 'div'"
      v-if="$slots.header || title || kicker || icon"
      :class="[vuetify ? undefined : 'ct-card-header', headerClass]"
    >
      <slot name="header">
        <div>
          <span v-if="kicker" class="ct-card-kicker">{{ kicker }}</span>
          <h3 v-if="title">{{ title }}</h3>
        </div>
        <v-icon v-if="icon" :icon="icon" color="primary" />
      </slot>
    </component>
    <component :is="vuetify ? 'v-card-text' : 'div'" :class="[vuetify ? undefined : 'ct-card-body', bodyClass]">
      <slot />
    </component>
    <component :is="vuetify ? 'v-card-actions' : 'div'" v-if="$slots.actions">
      <slot name="actions" />
    </component>
  </component>
</template>
