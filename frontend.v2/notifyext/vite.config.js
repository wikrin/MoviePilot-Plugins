import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import federation from '@originjs/vite-plugin-federation'

export default defineConfig({
  plugins: [
    vue(),
    federation({
      name: 'NotifyExt',
      filename: 'remoteEntry.js',
      exposes: {
        './Config': './src/components/Config.vue',
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
      format: 'esm'
    }),
  ],
  build: {
    target: 'esnext',   // 必须设置为esnext以支持顶层await
    minify: 'terser',      // 开发阶段建议关闭混淆
    cssCodeSplit: true, // 改为true以便能分离样式文件
  },
  css: {
    preprocessorOptions: {
      scss: {
        additionalData: '/* 覆盖vuetify样式 */',
      }
    },
    postcss: {
      plugins: [
        {
          postcssPlugin: 'internal:charset-removal',
          AtRule: {
            charset: (atRule) => {
              if (atRule.name === 'charset') {
                atRule.remove();
              }
            }
          }
        },
        // 只在非开发环境下启用 vuetify 样式过滤
        ...(process.env.NODE_ENV !== 'development' ? [{
          postcssPlugin: 'vuetify-filter',
          Root(root) {
            // 过滤掉所有vuetify相关的CSS
            root.walkRules(rule => {
              if (rule.selector && (
                rule.selector.includes('.v-') ||
                rule.selector.includes('.mdi-'))) {
                rule.remove();
              }
            });
          }
        }] : [])
      ]
    }
  },
  server: {
    port: 5001,   // 使用不同于主应用的端口
    cors: true,   // 启用CORS
    origin: 'http://localhost:5001'
  },
})