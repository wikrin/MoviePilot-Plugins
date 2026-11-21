import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { federation } from '@module-federation/vite'

export default defineConfig({
  base: './',
  plugins: [
    vue(),
    federation({
      name: 'TorrentFormatter',
      filename: 'assets/remoteEntry.js',
      dts: false,
      exposes: {
        './Config': './src/components/Config.vue',
        './Page': './src/components/Page.vue',
      },
      shared: {
        vue: {
          requiredVersion: false,
          import: false,
          singleton: true,
        },
        vuetify: {
          requiredVersion: false,
          import: false,
          singleton: true,
        },
        'vuetify/styles': {
          requiredVersion: false,
          import: false,
          singleton: true,
        },
      },
    }),
  ],
  build: {
    target: 'esnext', // 必须设置为esnext以支持顶层await
    minify: 'terser', // 开发阶段建议关闭混淆
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
    origin: 'http://localhost:5001',
  },
})
