<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('products.repairAction')"
    width="540px"
    destroy-on-close
    :close-on-press-escape="!saving"
    class="form-dialog"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form :model="form" label-position="top">
      <el-form-item :label="t('common.sn')" required>
        <el-input v-model="form.sn" :disabled="Boolean(presetSn)" :placeholder="t('products.repairSnPh')" />
      </el-form-item>

      <el-form-item :label="t('products.repairAction')" required>
        <el-radio-group v-model="form.repair_action" class="action-group">
          <el-radio-button v-for="a in REPAIR_ACTIONS" :key="a" :value="a">
            {{ t(`repair.${a}`) }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>

      <el-form-item
        v-if="needsTarget"
        :label="t('products.repairTargetStation')"
        required
      >
        <el-select v-model="form.target_station" filterable style="width:100%" :placeholder="t('configs.selectStation')">
            <el-option v-for="s in stationOptions" :key="s" :value="s" :label="s" />
          </el-select>
      </el-form-item>

      <el-form-item :label="t('products.repairReason')" required>
        <el-input v-model="form.reason" type="textarea" :rows="3" :placeholder="t('products.repairReasonPh')" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button :disabled="saving" @click="emit('update:modelValue', false)">{{ t('common.cancel') }}</el-button>
      <el-button type="primary" :loading="saving" :disabled="saving" @click="submit">{{ t('common.confirm') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { repairApi } from '../api'
import { REPAIR_ACTIONS, REPAIR_ACTIONS_WITH_TARGET } from '../utils/constants'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  presetSn: { type: String, default: '' },
  /** 可选目标工位（RETEST / ROLLBACK 使用）；传入则从在制品已盖章工位中挑选 */
  stationOptions: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue', 'saved'])

const { t } = useI18n()
const saving = ref(false)
const form = reactive({ sn: '', repair_action: 'RETEST', target_station: '', reason: '' })

const needsTarget = computed(() => REPAIR_ACTIONS_WITH_TARGET.includes(form.repair_action))

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    form.sn = props.presetSn
    form.repair_action = 'RETEST'
    form.target_station = props.stationOptions[0] || ''
    form.reason = ''
  }
)

async function submit() {
  if (saving.value) return
  if (!form.sn.trim()) return ElMessage.warning(t('repairs.snRequired'))
  if (!form.reason.trim()) return ElMessage.warning(t('repairs.reasonRequired'))
  if (needsTarget.value && !form.target_station) return ElMessage.warning(t('products.targetRequired'))

  saving.value = true
  try {
    await repairApi.create({
      sn: form.sn.trim(),
      repair_action: form.repair_action,
      target_station: needsTarget.value ? form.target_station : undefined,
      reason: form.reason.trim(),
    })
    ElMessage.success(t('products.repairSuccess'))
    emit('update:modelValue', false)
    emit('saved')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.action-group { display: flex; flex-wrap: wrap; }
</style>
