<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const expanded = ref({})   // group_id -> proof
const loading = ref({})    // group_id -> bool
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await api.quarantine()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function toggle(row) {
  const id = row.id
  if (expanded.value[id]) {
    const { [id]: _, ...rest } = expanded.value
    expanded.value = rest
    return
  }
  loading.value = { ...loading.value, [id]: true }
  try {
    const proof = await api.dupGroupProof(id)
    expanded.value = { ...expanded.value, [id]: proof }
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    const { [id]: _, ...rest } = loading.value
    loading.value = rest
  }
}
</script>

<template>
  <!-- plant-ui -->

  <div class="page-card">
    <h1>隔离</h1>
    <p class="lead">重复键整组隔离区：同运单 (event_ts, location_code, event_code) 撞键的点整组隔离，组内任一点不进正式时间线。点击行展开组成员与证明。</p>
    <p v-if="err" class="muted">{{ err }}</p>
    <table class="table">
      <thead>
        <tr><th>组</th><th>运单</th><th>重复键</th><th>成员数</th><th>原因</th><th>处置</th><th>时间</th></tr>
      </thead>
      <tbody>
        <template v-for="r in rows" :key="r.id">
          <tr class="group-row" @click="toggle(r)">
            <td>{{ r.id }}</td>
            <td>{{ r.waybill_id }}</td>
            <td>{{ r.event_ts }} · {{ r.location_code }} · {{ r.event_code }}</td>
            <td>{{ r.member_count }}</td>
            <td>{{ r.reason }}</td>
            <td>{{ r.disposition }}</td>
            <td>{{ r.created_at }}</td>
          </tr>
          <tr v-if="loading[r.id]"><td colspan="7" class="muted">证明加载中…</td></tr>
          <tr v-if="expanded[r.id]">
            <td colspan="7">
              <div class="member-box">
                <p class="member-head">
                  处置结论：{{ expanded[r.id].disposition }}
                  <span v-if="expanded[r.id].official_leak?.length" class="leak">
                    正式线泄漏：{{ expanded[r.id].official_leak.join(', ') }}
                  </span>
                  <span v-else class="ok-tag">正式线无组成员 ✓</span>
                </p>
                <table class="table member-table">
                  <thead>
                    <tr><th>hop</th><th>来源</th><th>event_ts</th><th>location</th><th>event_code</th><th>seq</th><th>批次</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="m in expanded[r.id].members" :key="m.hop_id">
                      <td>{{ m.hop_id }}</td>
                      <td>{{ m.origin === 'official_pulled' ? '正式线拉出' : '本批' }}</td>
                      <td>{{ m.event_ts }}</td>
                      <td>{{ m.location_code }}</td>
                      <td>{{ m.event_code }}</td>
                      <td>{{ m.seq ?? '—' }}</td>
                      <td>{{ m.batch_id }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!rows.length"><td colspan="7" class="muted">暂无隔离组</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.group-row { cursor: pointer; }
.group-row:hover td { background: #eef5f0; }
.member-box {
  padding: 10px 12px;
  background: #fbf6ea;
  border: 1px solid var(--amber-soft);
}
.member-head { margin: 0 0 8px; font-size: 13px; }
.member-table { background: var(--panel); }
.ok-tag { color: var(--mint); margin-left: 10px; font-family: var(--mono); font-size: 12px; }
.leak { color: var(--danger); margin-left: 10px; font-family: var(--mono); font-size: 12px; }
</style>
