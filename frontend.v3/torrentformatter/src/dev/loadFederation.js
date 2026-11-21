// 只用于独立开发壳：用宿主 Vue 单例加载正式 dist，验证联邦 ABI 与 CSS 资源。
import * as Vue from 'vue'

const remotes = new Map()
export async function loadFederatedView(entry, name) {
  if (!remotes.has(entry)) {
    remotes.set(
      entry,
      import(/* @vite-ignore */ entry).then(async remote => {
        await remote.init({
          vue: {
            [Vue.version]: {
              get: () => Promise.resolve(() => Vue),
              loaded: 1,
              from: 'torrentformatter-test-host',
              scope: 'default',
            },
          },
        })
        return remote
      }),
    )
  }
  const remote = await remotes.get(entry)
  const factory = await remote.get(`./${name}`)
  const module = await factory()
  return module.default || module
}
