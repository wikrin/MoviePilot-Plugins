import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { federation } from '@module-federation/vite'

export default defineConfig({
  base: './',
  plugins: [
    vue(),
    federation({
      name: 'CureTMDbAnime',
      filename: 'assets/remoteEntry.js',
      dts: false,
      exposes: {
        './Config': './src/components/Config.vue',
      },
      shared: {
        vue: {
          requiredVersion: false,
          import: false,
          singleton: true
        },
        vuetify: {
          requiredVersion: false,
          import: false,
          singleton: true
        },
        'vuetify/styles': {
          requiredVersion: false,
          import: false,
          singleton: true
        },
      },
    }),
  ],
  build: {
    target: 'esnext',
    minify: 'esbuild',
    cssCodeSplit: true,
  },
  css: {
    preprocessorOptions: {
      scss: {
        additionalData: '/* 覆盖vuetify样式 */',
      },
    },
    postcss: {
      plugins: [
        {
          postcssPlugin: 'internal:charset-removal',
          AtRule: {
            charset: atRule => {
              if (atRule.name === 'charset') {
                atRule.remove()
              }
            },
          },
        },
        {
          postcssPlugin: 'vuetify-filter',
          Root(root) {
            // 过滤掉所有vuetify相关的CSS
            root.walkRules(rule => {
              if (rule.selector && (rule.selector.includes('.v-') || rule.selector.includes('.mdi-'))) {
                rule.remove()
              }
            })
          },
        },
      ],
    },
  },
  server: {
    port: 5001, // 使用不同于主应用的端口
    cors: true, // 启用CORS
    origin: 'http://localhost:5001',
  },
})
