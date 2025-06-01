<template>
  <v-app>
    <v-app-bar color="primary" app>
      <v-app-bar-title>MoviePilot插件组件示例</v-app-bar-title>
    </v-app-bar>

    <v-main>
      <v-container>
        <v-tabs v-model="activeTab" bg-color="primary">
          <v-tab value="config">配置页面</v-tab>
          <!-- <v-tab value="page">详情页面</v-tab> -->
          <v-tab value="dashboard">仪表板</v-tab>
        </v-tabs>

        <v-window v-model="activeTab" class="mt-4">
          <!-- <v-window-item value="page">
            <h2 class="text-h5 mb-4">Page组件</h2>
            <div class="component-preview">
              <Page @action="handleAction"/>
            </div>
          </v-window-item> -->

          <v-window-item value="config">
            <h2 class="text-h5 mb-4">Config组件</h2>
            <div class="component-preview">
              <Config :api="mockApi" :initialConfig="mockConfig" />
            </div>
          </v-window-item>

          <v-window-item value="dashboard">
            <h2 class="text-h5 mb-4">Dashboard组件</h2>
            <div class="component-preview">
              <Dashboard :api="mockApi" :config="dashboardConfig" :allow-refresh="true"/>
            </div>
          </v-window-item>
        </v-window>
      </v-container>
    </v-main>

    <v-footer app color="primary" class="text-center d-flex justify-center">
      <span class="text-white">MoviePilot 模块联邦示例 ©{{ new Date().getFullYear() }}</span>
    </v-footer>
  </v-app>
</template>

<script setup>
import { ref, reactive } from 'vue';
import Config from './components/Config.vue';
// import Page from './components/Page.vue';
import Dashboard from './components/Dashboard.vue';

const activeTab = ref('dashboard')

// 模拟 api 对象
const mockApi = {
  get: async (url) => {
    console.log('GET 请求:', url);
    // 模拟返回数据
    if (url === 'plugin/SubscribeCal/grouped_events') {
      return {}
    }
    return []
  },

  post: async (url, data) => {
    console.log('POST 请求:', url, data);
    return { success: true };
  }
}

// 模拟 initialConfig
const mockConfig = ref({
  enabled: false,
  calc_time: 0,
  onlyonce: false,
  interval_minutes: 15,
  calname: '追剧日历',
  cron: ''
});

// 仪表板配置
const dashboardConfig = reactive({
  id: 'test_plugin',
  name: '测试插件',
  attrs: {
    title: '仪表板示例',
    subtitle: '插件数据展示',
    border: true,
  },
})
</script>

<style>
#app {
  padding: 20px;
}
</style>