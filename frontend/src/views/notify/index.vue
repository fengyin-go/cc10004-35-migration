<template>
  <section class="page" data-module="notify">
    <header class="page-head">
      <div>
        <h2>通知分发管理</h2>
        <p class="page-desc">维护通知分发规则的输入、执行约束与结果记录，为结果推送提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记通知规则</button>
        <button class="btn" type="button" @click="exportRows">导出通知分发清单</button>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无通知分发数据，可先登记通知规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条通知分发记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="artifact-panel">
      <header class="artifact-head">
        <h3>制品投递记录</h3>
        <span class="page-desc">跨模块投递按制品身份寻址，下载地址不包含模板名称</span>
        <span class="page-desc">共 {{ deliveryTotal }} 条</span>
      </header>
      <form class="filter-bar" @submit.prevent="deliver">
        <label class="filter-item">
          <span>制品编号</span>
          <input v-model="deliveryForm.artifact_id" placeholder="填写已确认制品的 artifact_id" />
        </label>
        <label class="filter-item">
          <span>通知规则 id</span>
          <input v-model="deliveryForm.rule_id" placeholder="例如 1" />
        </label>
        <label class="filter-item">
          <span>接收列表</span>
          <input v-model="deliveryForm.recipients" placeholder="值班组" />
        </label>
        <button class="btn primary" type="submit">投递制品</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>投递编号</th>
            <th>制品编号</th>
            <th>通道</th>
            <th>状态</th>
            <th>投递地址</th>
            <th>投递时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="delivery in deliveries" :key="delivery.delivery_id">
            <td>{{ delivery.delivery_id }}</td>
            <td>{{ delivery.artifact_id }}</td>
            <td>{{ delivery.channel }}</td>
            <td>{{ delivery.status }}</td>
            <td>
              <a class="link" :href="delivery.download_url" target="_blank" rel="noopener">
                {{ delivery.download_url }}
              </a>
            </td>
            <td>{{ delivery.delivered_at ?? '—' }}</td>
          </tr>
          <tr v-if="!deliveries.length">
            <td colspan="6" class="empty-state">暂无投递记录，可填写制品编号发起投递</td>
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

type Delivery = {
  delivery_id: number
  artifact_id: string
  channel: string
  status: string
  download_url: string
  delivered_at: string | null
}

const ENDPOINT = '/api/notify'
const columns = ["规则编号", "触发条件", "通知渠道", "接收列表", "消息模板", "合并窗口秒", "重复抑制", "优先级"]
const actions = ["提交校验", "启用通知", "停用通知"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const stats = [{"label": "通知规则总数", "value": 0}, {"label": "已启用规则", "value": 0}, {"label": "待校验规则", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const deliveries = ref<Delivery[]>([])
const deliveryTotal = ref(0)
const deliveryForm = ref({ artifact_id: '', rule_id: '', recipients: '' })
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

async function deliver() {
  errorMessage.value = ''
  const artifactId = deliveryForm.value.artifact_id.trim()
  if (!artifactId) {
    errorMessage.value = '请填写制品编号'
    return
  }
  const body: Record<string, unknown> = { artifact_id: artifactId }
  const ruleId = Number(deliveryForm.value.rule_id)
  if (deliveryForm.value.rule_id.trim() && Number.isFinite(ruleId)) {
    body.rule_id = ruleId
  }
  if (deliveryForm.value.recipients.trim()) {
    body.recipients = deliveryForm.value.recipients.trim()
  }
  try {
    const response = await request(`${ENDPOINT}/deliveries`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      throw new Error('投递未受理：制品可能未确认或文件不可用')
    }
    await reloadDeliveries()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '制品投递失败'
  }
}

async function reloadDeliveries() {
  try {
    const response = await request(`${ENDPOINT}/deliveries?size=50`)
    if (!response.ok) {
      throw new Error('投递记录读取失败')
    }
    const payload = await response.json()
    deliveries.value = payload.items ?? []
    deliveryTotal.value = payload.total ?? deliveries.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '投递记录读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '通知规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('通知分发动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '通知分发操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('通知规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await reloadDeliveries()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '通知分发列表读取失败'
  }
}

onMounted(reload)
</script>
