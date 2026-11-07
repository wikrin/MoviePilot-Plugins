<template>
  <div class="app-container">
    <v-app>
      <v-main>
        <config-component/>
        <page-component/>
      </v-main>
    </v-app>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" :timeout="snackbar.timeout" location="top end">
      {{ snackbar.text }}
      <template v-slot:actions>
        <v-btn variant="text" @click="snackbar.show = false"> 关闭 </v-btn>
      </template>
    </v-snackbar>
  </div>
</template>

<script setup>
import { reactive } from 'vue';
import ConfigComponent from './components/Config.vue'
import PageComponent from './components/Page.vue'

const snackbar = reactive({
  show: false,
  text: '',
  color: 'success',
  timeout: 3000,
});

</script>

<style scoped>
.component-preview {
  overflow: hidden;
  border: 1px solid #e0e0e0;
  border-radius: 8px;
  padding: 16px;
  background-color: #f9f9f9;
}
.v-tab {
  text-transform: none !important;
}
</style>
