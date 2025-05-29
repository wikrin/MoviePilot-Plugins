<template>
  <div id="app">
    <Config :api="mockApi" :initial-config="mockConfig" />
  </div>
</template>

<script setup>
import { ref } from 'vue';
import Config from './components/Config.vue';

// 模拟 api 对象
const mockApi = {
  get: async (url) => {
    console.log('GET 请求:', url);
    // 模拟返回数据
    if (url === 'system/setting/Notifications') {
      return {
        data: {
          value: [
            { name: 'Telegram', type: 'telegram' },
            { name: 'Email', type: 'email' }
          ]
        }
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
  enabled: true,
  cooldown: 300
});
</script>

<style>
#app {
  padding: 20px;
}
</style>