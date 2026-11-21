<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { MOBILE_PAGE_SIZE } from '../composables/useTorrentFormatter.js'

const props = defineProps({
  total: { type: Number, default: 0 },
  page: { type: Number, default: 1 },
  disabled: Boolean,
})
const emit = defineEmits(['apply'])
const open = ref(false)
const menu = ref(null)
const list = ref(null)
const returnFocus = ref(false)
const pages = computed(() => Math.max(1, Math.ceil(props.total / MOBILE_PAGE_SIZE)))
const pageNumbers = computed(() => Array.from({ length: pages.value }, (_, index) => pages.value - index))
const rangeStart = computed(() => props.total ? (props.page - 1) * MOBILE_PAGE_SIZE + 1 : 0)
const rangeEnd = computed(() => Math.min(props.page * MOBILE_PAGE_SIZE, props.total))
watch(() => props.disabled, () => { void focusButton() })
watch(open, value => {
  returnFocus.value = !value
  if (!value) void focusButton()
})
async function focusButton() {
  if (!returnFocus.value || open.value || props.disabled) return
  await nextTick()
  if (!open.value && !props.disabled) {
    menu.value?.activatorEl?.focus()
    returnFocus.value = false
  }
}
function afterLeave() {
  returnFocus.value = true
  void focusButton()
}
function scrollToCurrent() {
  const element = list.value?.$el || list.value
  const selected = element?.querySelector('[aria-selected="true"]')
  if (!element || !selected) return
  const item = selected.getBoundingClientRect()
  const offset = element.scrollTop + item.top - element.getBoundingClientRect().top
  // 当前页靠近入口，同时尽量留出下方两行较小页码。
  element.scrollTop = Math.max(0, offset - Math.max(0, element.clientHeight - item.height * 3))
}
function choosePage(page) {
  if (props.disabled) return
  if (page !== props.page) emit('apply', { page, pageSize: MOBILE_PAGE_SIZE })
  open.value = false
}
function pageRange(page) {
  const start = props.total ? (page - 1) * MOBILE_PAGE_SIZE + 1 : 0
  const end = Math.min(page * MOBILE_PAGE_SIZE, props.total)
  return `${start}–${end}`
}
function pageLabel(page) {
  return `第 ${page} 页 · ${pageRange(page)} 个种子`
}
</script>

<template>
  <v-menu ref="menu" v-model="open" location="top start" :offset="6" :max-height="250" :width="180" :min-width="180" :max-width="180"
    content-class="tf-pagination-dropdown" transition="slide-y-reverse-transition"
    @after-enter="scrollToCurrent" @after-leave="afterLeave">
    <template #activator="{ props: activatorProps }">
      <v-btn v-bind="activatorProps" class="tf-pagination-menu-button" variant="text"
        :append-icon="open ? 'mdi-chevron-down' : 'mdi-chevron-up'" :disabled="disabled" aria-haspopup="listbox"
        :aria-label="`选择种子页码，当前 ${rangeStart}–${rangeEnd} / ${total} 个种子`"
        >{{ rangeStart }}–{{ rangeEnd }} / {{ total }} 个种子</v-btn>
    </template>
    <v-list ref="list" role="listbox" aria-label="种子页码" density="compact" color="primary">
      <v-list-item v-for="number in pageNumbers" :key="number" :value="number" :active="number === page"
        class="tf-pagination-page" role="option" :aria-selected="number === page" :disabled="disabled"
        :aria-label="pageLabel(number)" @click="choosePage(number)">
        <template #title>
          <div class="tf-pagination-page-content">
            <span class="tf-pagination-page-number">{{ String(number).padStart(2, '0') }}</span>
            <span class="tf-pagination-page-range">{{ pageRange(number) }}</span>
            <v-icon v-if="number === page" icon="mdi-check" size="16" aria-hidden="true" />
          </div>
        </template>
      </v-list-item>
    </v-list>
  </v-menu>
</template>
