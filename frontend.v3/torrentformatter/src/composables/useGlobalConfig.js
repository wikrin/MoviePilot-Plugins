import { computed, onScopeDispose, ref, shallowRef } from 'vue'
import { createTorrentFormatterApi, DEFAULT_RULES, errorMessage } from '../api/torrentformatter.js'

const defaults = {
  ...DEFAULT_RULES,
  cron_enabled: false,
  downloader: [],
  exclude_dirs: '',
  exclude_tags: '',
  cron: '',
  event_enabled: false,
}
const clone = value => JSON.parse(JSON.stringify(value))
const configKeys = Object.keys(defaults)

function normalizeConfig(value) {
  const config = { ...value }
  for (const key of configKeys) {
    if (config[key] === null && typeof defaults[key] === 'string') config[key] = ''
    if (typeof defaults[key] === 'boolean' && typeof config[key] !== 'boolean') {
      // Match the backend's accepted legacy boolean values, including string "false".
      config[key] = /^(1|true|t|on|yes|y)$/i.test(String(config[key]))
    }
    config[key] ??= defaults[key]
  }
  return clone(config)
}

export function useGlobalConfig(getClient, getPluginId) {
  const config = ref(clone(defaults))
  const baseline = shallowRef(clone(defaults))
  const loaded = ref(false)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')
  let generation = 0
  let controller

  const changed = computed(() =>
    configKeys.filter(key => JSON.stringify(config.value[key]) !== JSON.stringify(baseline.value[key])),
  )

  async function load() {
    if (loading.value || saving.value) return
    const request = ++generation
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const value = await createTorrentFormatterApi(getClient()).loadConfig(getPluginId(), controller.signal)
      if (generation !== request) return
      const edits = Object.fromEntries(changed.value.map(key => [key, config.value[key]]))
      baseline.value = normalizeConfig(value)
      config.value = { ...clone(baseline.value), ...edits }
      loaded.value = true
    } catch (err) {
      if (generation === request) error.value = errorMessage(err, '全局配置加载失败')
    } finally {
      if (generation === request) loading.value = false
    }
  }

  async function save() {
    if (!loaded.value || loading.value || saving.value || !changed.value.length) return
    const request = ++generation
    const value = normalizeConfig(config.value)
    saving.value = true
    error.value = ''
    try {
      await createTorrentFormatterApi(getClient()).saveConfig(getPluginId(), value)
      if (generation !== request) return
      baseline.value = value
      config.value = clone(value)
      return value
    } catch (err) {
      if (generation === request) error.value = errorMessage(err, '全局配置保存失败')
    } finally {
      if (generation === request) saving.value = false
    }
  }

  function restoreDefaults() {
    config.value = { ...config.value, ...clone(defaults) }
    error.value = ''
  }

  function reset() {
    generation++
    controller?.abort()
    loaded.value = loading.value = saving.value = false
    error.value = ''
    baseline.value = clone(defaults)
    config.value = clone(defaults)
  }

  onScopeDispose(reset)

  return { config, changed, loaded, loading, saving, error, load, save, restoreDefaults, reset }
}
