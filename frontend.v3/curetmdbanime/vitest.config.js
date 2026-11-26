import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['checks/*.spec.ts'],
    server: { deps: { inline: ['vuetify'] } },
  },
})
