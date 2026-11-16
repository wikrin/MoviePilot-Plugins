import { createApp } from 'vue'
import App from './App.vue'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import 'vuetify/styles'

// 此入口只负责独立开发壳；宿主加载 ./Page 与 ./Config，不加载演示 API。
if (import.meta.env.DEV) {
  const icons = document.createElement('link')
  icons.rel = 'stylesheet'
  icons.href = 'https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css'
  document.head.appendChild(icons)
}
const vuetify = createVuetify({
  components,
  directives,
  theme: {
    defaultTheme: 'light',
    themes: {
      light: { colors: { primary: '#5663D8', secondary: '#6B7188', background: '#F6F7FB', surface: '#FFFFFF' } },
      dark: { colors: { primary: '#A6B0FF', secondary: '#B0B6CC', background: '#171A24', surface: '#222633' } },
    },
  },
})
createApp(App).use(vuetify).mount('#app')
