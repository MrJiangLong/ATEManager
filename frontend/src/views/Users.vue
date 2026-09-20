<template>
  <div class="page">
    <div class="page-head">
      <h2 class="page-title">{{ t('menu.users') }}</h2>
      <el-button type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" size="small" border stripe>
      <el-table-column prop="username" :label="t('users.username')" min-width="140" />
      <el-table-column :label="t('users.fullName')" min-width="140">
        <template #default="{ row }">{{ row.full_name || '—' }}</template>
      </el-table-column>
      <el-table-column :label="t('users.role')" width="120" align="center">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="roleTagType(row.role)">{{ t(`users.roles.${row.role}`) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('users.status')" width="100" align="center">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="row.is_active ? 'success' : 'danger'">
            {{ row.is_active ? t('users.enabled') : t('users.disabled') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('users.createdAt')" width="170">
        <template #default="{ row }">{{ fmtDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="140" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
          <el-button link type="danger" size="small" :disabled="row.username === state.user?.username" @click="onDelete(row)">
            {{ t('common.delete') }}
          </el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <el-dialog v-model="dialogVisible" :title="isEdit ? t('users.editTitle') : t('users.createTitle')" width="440px" destroy-on-close class="form-dialog">
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-form-item :label="t('users.role')" required>
          <div class="role-cards">
            <div
              v-for="r in ROLES"
              :key="r"
              class="role-card"
              :class="{ active: form.role === r }"
              @click="form.role = r"
            >
              <div class="role-name">{{ t(`users.roles.${r}`) }}</div>
              <div class="role-desc">{{ t(`users.roleTag.${r}`) }}</div>
            </div>
          </div>
        </el-form-item>
        <el-form-item :label="t('users.username')" required>
          <el-input v-model="form.username" :disabled="isEdit" :placeholder="t('users.usernamePh')" />
        </el-form-item>
        <el-form-item :label="t('users.fullName')">
          <el-input v-model="form.full_name" :placeholder="t('users.fullNamePh')" />
        </el-form-item>
        <el-form-item v-if="!isEdit" :label="t('users.password')" required>
          <el-input v-model="form.password" type="password" show-password :placeholder="t('users.passwordPh')" />
        </el-form-item>
        <el-form-item v-if="isEdit" :label="t('users.status')">
          <el-switch v-model="form.is_active" :active-text="t('users.enabled')" :inactive-text="t('users.disabled')" />
        </el-form-item>
        <el-form-item v-if="isEdit" :label="t('users.resetPwd')">
          <el-input v-model="form.password" type="password" show-password :placeholder="t('users.resetPwdPh')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { usersApi } from '../api'
import EmptyState from '../components/EmptyState.vue'
import { useAuth } from '../stores/auth'
import { fmtDateTime } from '../utils/format'

const { t } = useI18n()
const { state } = useAuth()

const ROLES = ['viewer', 'operator', 'admin']
const rows = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const editingId = ref(null)
const form = reactive({ username: '', full_name: '', role: '', password: '', is_active: true })

function roleTagType(role) {
  return role === 'admin' ? 'danger' : role === 'operator' ? 'warning' : 'info'
}

async function load() {
  loading.value = true
  try {
    rows.value = (await usersApi.list()).data
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, { username: '', full_name: '', role: '', password: '', is_active: true })
  dialogVisible.value = true
}

function openEdit(row) {
  isEdit.value = true
  editingId.value = row.id
  Object.assign(form, { username: row.username, full_name: row.full_name || '', role: row.role, password: '', is_active: row.is_active })
  dialogVisible.value = true
}

async function submit() {
  if (!isEdit.value && (!form.username.trim() || form.password.length < 6)) {
    ElMessage.warning(t('users.invalidInput'))
    return
  }
  if (!form.role) {
    ElMessage.warning(t('users.roleRequired'))
    return
  }
  saving.value = true
  try {
    if (isEdit.value) {
      const payload = { full_name: form.full_name, role: form.role, is_active: form.is_active }
      if (form.password) payload.password = form.password
      await usersApi.update(editingId.value, payload)
    } else {
      await usersApi.create({ username: form.username.trim(), password: form.password, full_name: form.full_name, role: form.role })
    }
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(t('users.deleteConfirm', { name: row.username }), t('common.delete'), { type: 'warning' })
  try {
    await usersApi.remove(row.id)
    ElMessage.success(t('common.deleted'))
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(load)
</script>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.page-title { font-size: 16px; font-weight: 600; margin: 0; }
/* 角色卡片选择器：替代单选按钮 + 说明行 */
.role-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; width: 100%; }
.role-card {
  border: 1px solid var(--app-border, #eef1f7);
  border-radius: 10px;
  padding: 10px 8px;
  cursor: pointer;
  text-align: center;
  background: var(--app-card-bg, #fff);
  transition: border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;
}
.role-card:hover { border-color: var(--app-primary, #2f6bff); }
.role-card.active {
  border-color: var(--app-primary, #2f6bff);
  background: var(--app-primary-soft, rgba(47, 107, 255, 0.06));
  box-shadow: inset 0 0 0 1px var(--app-primary, #2f6bff);
}
.role-name { font-size: 13px; font-weight: 600; color: var(--app-text, #303133); }
.role-card.active .role-name { color: var(--app-primary, #2f6bff); }
.role-desc { font-size: 11.5px; color: var(--app-text-muted, #8a94a6); margin-top: 3px; }
</style>
