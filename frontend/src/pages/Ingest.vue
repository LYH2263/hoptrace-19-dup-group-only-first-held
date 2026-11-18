<script setup>
import { computed, ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const busy = ref(false)
const err = ref('')

async function run() {
  busy.value = true
  err.value = ''
  try {
    result.value = await api.ingest()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

const report = computed(() => result.value?.report || null)
</script>

<template>
  <div class="page-card">
    <h1>装载</h1>
    <p class="lead">从本地夹具跑一批轨迹点装载。重复键 (event_ts, location_code, event_code) 撞键的点整组隔离，不进正式时间线；报告与隔离列表、正式时间线、证明接口同源。</p>
    <button class="btn" :disabled="busy" @click="run">{{ busy ? '装载中…' : '跑装载批次' }}</button>
    <p v-if="err" class="muted" style="margin-top:12px">{{ err }}</p>
    <template v-if="report">
      <div class="stat-row" style="margin-top:16px">
        <div class="stat"><b>{{ report.input }}</b><span>入批点数</span></div>
        <div class="stat"><b>{{ report.written_official }}</b><span>正式写入</span></div>
        <div class="stat"><b>{{ report.dup_group_count }}</b><span>重复组</span></div>
        <div class="stat"><b>{{ report.quarantined_points }}</b><span>隔离点</span></div>
        <div class="stat"><b>{{ report.replayed }}</b><span>重放跳过</span></div>
      </div>
      <table class="table" v-if="report.groups?.length" style="margin-top:12px">
        <thead>
          <tr><th>组</th><th>运单</th><th>重复键</th><th>成员</th><th>处置</th></tr>
        </thead>
        <tbody>
          <tr v-for="g in report.groups" :key="g.group_id">
            <td>{{ g.group_id }}</td>
            <td>{{ g.waybill_id }}</td>
            <td>{{ g.dup_key.event_ts }} · {{ g.dup_key.location_code }} · {{ g.dup_key.event_code }}</td>
            <td>{{ g.member_hop_ids.join(', ') }}</td>
            <td>{{ g.disposition }}</td>
          </tr>
        </tbody>
      </table>
    </template>
    <pre v-if="result" style="margin-top:16px;font-family:var(--mono);font-size:12px;white-space:pre-wrap">{{ result }}</pre>
  </div>
</template>
