<template>
  <section class="page" data-module="reportgen">
    <header class="page-head">
      <div>
        <h2>报告生成管理</h2>
        <p class="page-desc">维护分析报告模板的输入、执行约束与结果记录，为报告生成提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记报告模板</button>
        <button class="btn" type="button" @click="exportRows">导出报告生成清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="generate(row)">生成制品</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无报告生成数据，可先登记报告模板</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条报告生成记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="artifact-panel">
      <header class="artifact-head">
        <h3>已生成制品</h3>
        <span class="page-desc">只有已确认且校验通过的制品才会出现下载与领取入口</span>
        <span class="page-desc">共 {{ artifactTotal }} 条</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>制品编号</th>
            <th>模板编号</th>
            <th>格式</th>
            <th>状态</th>
            <th>大小</th>
            <th>生成时间</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="artifact in artifacts" :key="artifact.artifact_id">
            <td>{{ artifact.artifact_id }}</td>
            <td>{{ artifact.template_code }}</td>
            <td>{{ artifact.format }}</td>
            <td>{{ artifact.state }}{{ artifact.available ? '' : '（不可投递）' }}</td>
            <td>{{ artifact.size_bytes ?? '—' }}</td>
            <td>{{ artifact.created_at ?? '—' }}</td>
            <td class="row-actions">
              <a
                v-if="artifact.available"
                class="link"
                :href="artifact.download_url ?? '#'"
                target="_blank"
                rel="noopener"
              >下载</a>
              <button
                v-if="artifact.available"
                class="link"
                type="button"
                @click="claim(artifact)"
              >领取</button>
              <span v-if="!artifact.available" class="page-desc">{{ artifact.error || '文件未就绪' }}</span>
            </td>
          </tr>
          <tr v-if="!artifacts.length">
            <td colspan="7" class="empty-state">暂无已生成制品，可在模板行点击“生成制品”</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type Artifact = {
  artifact_id: string
  template_id: number
  template_code: string
  state: string
  available: boolean
  format: string
  size_bytes: number | null
  created_at: string | null
  error: string | null
  download_url: string | null
  claim_url: string | null
}

const ENDPOINT = '/api/reportgen'
const columns = ["模板编号", "模板名称", "包含图表", "报告周期", "导出格式", "推送渠道", "水印开关", "附件压缩"]
const actions = ["提交审核", "生效发布", "停用模板"]
const statuses = ["草稿", "待审核", "已生效", "已停用"]
const stats = [{"label": "报告模板总数", "value": 0}, {"label": "已生效模板", "value": 0}, {"label": "待审核模板", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const artifacts = ref<Artifact[]>([])
const artifactTotal = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '报告模板登记入口尚未接入审批流'
}

function generate(row: Row) {
  // 确认请求号由前端生成，重复点击同一请求只受理一次；失败时用新请求号重试。
  const key = `ui-${row.id}-${Date.now()}-${Math.floor(Math.random() * 1000)}`
  void submitGenerate(row, key)
}

async function submitGenerate(row: Row, key: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/artifacts`, {
      method: 'POST',
      body: JSON.stringify({ idempotency_key: key, format: row['导出格式'] }),
    })
    if (!response.ok) {
      throw new Error('制品生成未受理，请稍后重试')
    }
    await reloadArtifacts()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '制品生成失败'
  }
}

async function claim(artifact: Artifact) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/artifacts/${artifact.artifact_id}/claim`, {
      method: 'POST',
      body: JSON.stringify({ claimer: '值班管理员', idempotency_key: `ui-claim-${Date.now()}` }),
    })
    if (!response.ok) {
      throw new Error('制品领取失败，可能文件已经不可用')
    }
    await reloadArtifacts()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '制品领取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('报告生成动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报告生成操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('报告模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await reloadArtifacts()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报告生成列表读取失败'
  }
}

async function reloadArtifacts() {
  try {
    const response = await request(`${ENDPOINT}/artifacts?size=50`)
    if (!response.ok) {
      throw new Error('制品列表读取失败')
    }
    const payload = await response.json()
    artifacts.value = payload.items ?? []
    artifactTotal.value = payload.total ?? artifacts.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '制品列表读取失败'
  }
}

onMounted(reload)
</script>
