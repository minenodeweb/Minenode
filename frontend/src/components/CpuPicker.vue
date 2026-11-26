<template>
  <div class="cpu">
    <div class="row" style="align-items: baseline; gap: 12px">
      <div class="big num" :class="{ warn: isRisky }">{{ isLimited ? `${modelValue}%` : $t('Bez limitu') }}</div>
      <div class="muted small">
        {{ isLimited
          ? $t('≈ {c} rdzenia (100% = 1 rdzeń)', { c: coresLabel })
          : (cpuCount ? $t('host ma {n} rdzeni ({max}%)', { n: cpuCount, max: cpuCount * 100 }) : $t('serwer może użyć wszystkich rdzeni')) }}
      </div>
      <div class="spacer" />
      <div class="input-group pct-input">
        <input
          type="number"
          class="input num"
          :value="modelValue ?? ''"
          min="10"
          max="6400"
          step="5"
          placeholder="∞"
          :aria-label="$t('Limit CPU (%)')"
          @input="onInput($event.target.value)"
          @change="onCommit($event)"
        />
        <span class="addon">%</span>
      </div>
    </div>

    <input
      type="range"
      :min="25"
      :max="maxPct"
      step="25"
      :value="isLimited ? Math.min(maxPct, Math.max(25, modelValue)) : maxPct"
      :class="{ unlimited: !isLimited, risky: isRisky }"
      @input="set(+$event.target.value)"
    />

    <div class="row wrap" style="gap: 6px">
      <button type="button" class="chip" :class="{ active: !isLimited }" @click="set(null)">{{ $t('Bez limitu') }}</button>
      <button
        v-for="p in presets"
        :key="p"
        type="button"
        class="chip"
        :class="{ active: modelValue === p, risky: p < 100 }"
        @click="set(p)"
      >
        {{ p }}%
      </button>
    </div>

    <div v-if="isRisky" class="callout warn small">
      <AlertTriangle />
      <div>{{ $t('Limit poniżej 100% (mniej niż 1 pełny rdzeń) jest ryzykowny – główny wątek gry może dławić TPS, powodować cofki graczy lub zawiesić serwer (Watchdog).') }}</div>
    </div>

    <div class="hint tiny muted">
      {{ $t('100% = 1 pełny rdzeń CPU (np. 150% = 1,5 rdzenia). Ustawienie poniżej 100% jest ryzykowne dla głównego wątku serwera.') }}<template v-if="backendNote"> {{ $t(backendNote) }}</template>
    </div>
  </div>
</template>
<script setup>
import { computed } from 'vue'
import { AlertTriangle } from 'lucide-vue-next'

const props = defineProps({
  modelValue: { type: Number, default: null },
  cpuCount: { type: Number, default: 0 },
  backend: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const isLimited = computed(() => props.modelValue != null && props.modelValue > 0)
const isRisky = computed(() => isLimited.value && props.modelValue < 100)
const cores = computed(() => Math.max(1, props.cpuCount || 4))
const maxPct = computed(() => Math.max(200, Math.min(6400, cores.value * 100)))
const presets = computed(() => [50, 100, 150, 200, 300, 400, 600, 800].filter((p) => p <= maxPct.value))
const coresLabel = computed(() => (isLimited.value ? Number((props.modelValue / 100).toFixed(2)) : ''))

const BACKEND_NOTES = {
  cgroup: 'Wewnątrz kontenera limit wymusza jądro Linux (cgroup v2 CPUQuota) oraz flaga JVM.',
  systemd: 'Na hoście limit wymusza systemd (CPUQuota) oraz flaga JVM.',
  cpulimit: 'W kontenerze limit wymusza cpulimit (pauzuje proces powyżej progu) oraz flaga JVM.',
  jvm: 'System nie udostępnia twardego limitu CPU – stosowana jest jedynie flaga JVM (ActiveProcessorCount).',
}
const backendNote = computed(() => BACKEND_NOTES[props.backend] || '')

function set(v) {
  emit('update:modelValue', v == null || v <= 0 ? null : Math.min(6400, Math.max(10, Math.round(v))))
}

function onInput(raw) {
  if (raw === '' || raw == null) {
    set(null)
    return
  }
  const n = Math.round(Number(raw))
  if (Number.isFinite(n) && n >= 10 && n <= 6400) {
    set(n)
  }
}

function onCommit(event) {
  const raw = event.target.value
  if (raw === '' || raw == null) {
    set(null)
    return
  }
  const n = Math.round(Number(raw))
  if (!Number.isFinite(n) || n <= 0) {
    set(null)
    event.target.value = ''
    return
  }
  const clamped = Math.min(6400, Math.max(10, n))
  set(clamped)
  event.target.value = String(clamped)
}
</script>
<style scoped>
.cpu { display: flex; flex-direction: column; gap: 12px; }
.big { font-size: 26px; font-weight: 650; letter-spacing: -0.02em; }
.big.warn { color: var(--warn); }
.pct-input { width: 112px; flex: none; }
.pct-input .input { height: 32px; text-align: right; }
input[type='range'].unlimited { opacity: 0.45; }
input[type='range'].risky { accent-color: var(--warn); }
.chip.risky:not(.active) { border-color: color-mix(in srgb, var(--warn) 40%, var(--border-strong)); color: var(--warn); }
.chip.risky.active { background: var(--warn-soft); border-color: color-mix(in srgb, var(--warn) 55%, transparent); color: var(--warn); }
</style>
