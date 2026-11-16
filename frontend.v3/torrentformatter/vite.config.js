import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'TorrentFormatter',
      filename: 'remoteEntry.js',
      exposes: {
        './Config': './src/components/Config.vue',
        './Page': './src/components/Page.vue',
      },
      shared: {
        vue: {
          requiredVersion: false,
          generate: false,
        },
        vuetify: {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
        'vuetify/styles': {
          requiredVersion: false,
          generate: false,
          singleton: true,
        },
      },
      format: 'esm',
    }),
  ],
  build: {
    target: 'esnext', // 必须设置为esnext以支持顶层await
    minify: 'terser', // 开发阶段建议关闭混淆
    cssCodeSplit: true,
    rollupOptions: {
      output: {
        // finalizer 只保留联邦白名单，共享组件/工具的 chunk 也必须匹配。
        chunkFileNames: 'assets/__federation_[name]-[hash].js',
        assetFileNames: 'assets/__federation_[name]-[hash][extname]',
      },
    },
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
        // 只在非开发环境下启用 vuetify 样式过滤
        ...(process.env.NODE_ENV !== 'development'
          ? [
              {
                postcssPlugin: 'vuetify-filter',
                Root(root) {
                  // 只过滤依赖级样式，不能误删插件自己的 .v-* 深度定制。
                  const source = (root.source?.input?.file || '').replaceAll('\\', '/')
                  if (!source.includes('node_modules/vuetify/')) return
                  root.walkRules(rule => {
                    if (rule.selector && (rule.selector.includes('.v-') || rule.selector.includes('.mdi-'))) {
                      rule.remove()
                    }
                  })
                },
              },
            ]
          : []),
      ],
    },
  },
  server: {
    port: 5001, // 使用不同于主应用的端口
    cors: true,
  },
})
