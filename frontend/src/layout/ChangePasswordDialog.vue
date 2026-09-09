<template>
  <el-dialog
    :model-value="modelValue"
    :title="$t('auth.changePassword')"
    width="420px"
    destroy-on-close
    :close-on-press-escape="!saving"
    class="pwd-dialog"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <el-form ref="formRef" :model="form" :rules="rules" :label-width="isEn ? '120px' : '90px'" label-position="left">
      <el-form-item :label="$t('auth.currentPassword')" prop="current_password">
        <el-input v-model="form.current_password" type="password" show-password :placeholder="$t('auth.currentPasswordPlaceholder')" autocomplete="current-password" />
      </el-form-item>
      <el-form-item :label="$t('auth.newPassword')" prop="new_password">
        <el-input v-model="form.new_password" type="password" show-password :placeholder="$t('auth.newPasswordPlaceholder')" autocomplete="new-password" />
      </el-form-item>
      <el-form-item :label="$t('auth.confirmPassword')" prop="confirm_password">
        <el-input v-model="form.confirm_password" type="password" show-password :placeholder="$t('auth.confirmPasswordPlaceholder')" autocomplete="new-password" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :disabled="saving" @click="$emit('update:modelValue', false)">{{ $t('common.cancel') }}</el-button>
      <el-button type="primary" class="app-btn-primary" :loading="saving" :disabled="saving" @click="submit">{{ $t('common.save') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { authApi } from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const { t, locale } = useI18n()
const isEn = computed(() => locale.value === 'en')

const formRef = ref()
const saving = ref(false)
const form = reactive({ current_password: '', new_password: '', confirm_password: '' })

// Reset form when the dialog closes. destroy-on-close only unmounts the DOM;
// the reactive `form` lives in setup scope and would otherwise leak previous
// values into the next open.
function resetForm() {
  form.current_password = ''
  form.new_password = ''
  form.confirm_password = ''
  formRef.value?.clearValidate()
}
watch(
  () => props.modelValue,
  (open) => {
    if (!open) resetForm()
  },
)

const rules = {
  current_password: [
    { required: true, message: () => t('auth.currentPasswordRequired'), trigger: 'blur' },
  ],
  new_password: [
    { required: true, message: () => t('auth.newPasswordRequired'), trigger: 'blur' },
    { min: 6, message: () => t('auth.newPasswordMin'), trigger: 'blur' },
  ],
  confirm_password: [
    {
      validator: (rule, value, callback) => {
        if (!value) callback(new Error(t('auth.confirmPasswordRequired')))
        else if (value !== form.new_password) callback(new Error(t('auth.passwordMismatch')))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function submit() {
  if (saving.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    await authApi.changePassword({
      current_password: form.current_password,
      new_password: form.new_password,
    })
    ElMessage.success(t('auth.changePasswordSuccess'))
    emit('update:modelValue', false)
  } catch (e) {
    ElMessage.error(e.message || t('auth.changePasswordFail'))
  } finally {
    saving.value = false
  }
}
</script>
