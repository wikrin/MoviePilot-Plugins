<script setup>
import Page from './Page.vue'

defineOptions({ inheritAttrs: false })
</script>

<template>
  <Page v-bind="$attrs" start-section="automation" />
</template>
