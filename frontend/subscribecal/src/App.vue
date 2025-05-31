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
            <v-switch v-model="dashboardConfig.attrs.border" label="显示边框" color="primary" class="mb-4"></v-switch>
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
      return {
    "2025-05-28": [
        {
            "id": 4,
            "dtstart": "20250528T160000Z",
            "dtend": "20250529T160000Z",
            "summary": "[9/13]摇滚乃是淑女的爱好 (2025)",
            "description": "竟演当天，在苦涩甘纳许之后，莉莉纱一行的演奏开始了。但蒂娜从一开始就没踩准节奏，她试图调整却始终没能跟上，整个演奏过程中都十分痛苦。另一方面，莉莉纱的表现也比平时更加生硬，她越是挣扎，观众却越是失去兴趣——",
            "location": null,
            "uid": "5ed772a7-00f4-4741-9995-cdcc6663416c",
            "year": "2025",
            "type": "电视剧",
            "season": 1,
            "poster": "https://image.tmdb.org/t/p/w500/vmpnW5jEBtVvCJwBgXpOLhr9EqX.jpg",
            "backdrop": "https://image.tmdb.org/t/p/w500/i9E2lwBBJGuVEIwl1LZckC4isKz.jpg",
            "vote": 9.0,
            "state": "R"
        }
    ],
    "2025-05-31": [
        {
            "id": 6,
            "dtstart": "20250531T160000Z",
            "dtend": "20250601T160000Z",
            "summary": "[9/26]魔女与使魔 (2025)",
            "description": "",
            "location": null,
            "uid": "c7106815-b28b-46ae-8756-3247b32de595",
            "year": "2025",
            "type": "电视剧",
            "season": 1,
            "poster": "https://image.tmdb.org/t/p/w500/anSSufoNCoKb0xdqbIasC2HVEJk.jpg",
            "backdrop": "https://image.tmdb.org/t/p/w500/yTfaG7igCJa324DU0QaxCTzYBXF.jpg",
            "vote": 10.0,
            "state": "R"
        },
        {
            "id": 8,
            "dtstart": "20250531T160000Z",
            "dtend": "20250601T160000Z",
            "summary": "[9/13]拉撒路 (2025)",
            "description": "",
            "location": null,
            "uid": "62f1c449-a8a3-477d-80b3-59c55ae318b4",
            "year": "2025",
            "type": "电视剧",
            "season": 1,
            "poster": "https://image.tmdb.org/t/p/w500/3OZ8EFSSWHceyXnxMUOvBcVXfea.jpg",
            "backdrop": "https://image.tmdb.org/t/p/w500/1WxpAZfoW8B2sIfWND81GdTHm4G.jpg",
            "vote": 9.2,
            "state": "R"
        }
    ],
    "2025-06-03": [
        {
            "id": 2,
            "dtstart": "20250603T100000Z",
            "dtend": "20250604T160000Z",
            "summary": "[9/12]末日后酒店 (2025)",
            "description": "",
            "location": null,
            "uid": "efb66210-d441-496c-9051-6531f54d538f",
            "year": "2025",
            "type": "电视剧",
            "season": 1,
            "poster": "https://image.tmdb.org/t/p/w500/5iKarEfaqHdUzg2tQZ7GpBAN48Q.jpg",
            "backdrop": "https://image.tmdb.org/t/p/w500/bGIuwVsPN7T2c7ffWLUdey52EMx.jpg",
            "vote": 9.5,
            "state": "R"
        }
    ]
}
    } else if (url === 'media/category') {
      return {
        data: {
          movie: ['动作片', '科幻片', '爱情片'],
          tv: ['剧情剧', '喜剧剧', '动画剧']
        }
      }
    } else if (url === 'plugin/NotifyExt/rules') {
    return []
    } else if (url === 'plugin/NotifyExt/templates') {
    }
    return [
      {
        "id": "b211080f-a003-4ec1-92e4-619bd5921f74",
        "name": "模板1",
        "template": "{\n  'title': '',\n  'text': ''\n}"
      }
    ]
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