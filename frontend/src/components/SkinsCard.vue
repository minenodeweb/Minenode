<template>
  <section class="card">
    <div class="card-head">
      <Shirt class="hi" /><h3>{{ $t('Skiny graczy (SkinsRestorer)') }}</h3>
      <span v-if="d?.enabled" class="badge green">{{ $t('włączony') }}</span>
    </div>
    <div class="card-body col" style="gap: 14px">
      <div v-if="!d" class="row small muted"><Loader2 class="spin" style="width: 15px" /> {{ $t('Sprawdzanie…') }}</div>
      <template v-else>
        <div class="row" style="align-items: flex-start; gap: 12px">
          <div class="grow col" style="gap: 3px; min-width: 0">
            <div class="row wrap" style="gap: 8px">
              <b class="small">{{ $t('Skiny kont Minecraft Java') }}</b>
              <span class="badge">{{ $t(d.kind === 'plugin' ? 'plugin' : 'mod') }} · SkinsRestorer</span>
              <span v-if="d.version" class="badge mono">{{ d.version }}</span>
              <span v-if="d.update_available" class="badge amber">{{ $t('dostępna {v}', { v: d.latest_version }) }}</span>
            </div>
            <div class="tiny muted">{{ $t('Na serwerze bez weryfikacji kont (offline-mode) wszyscy wyglądają jak Steve lub Alex. SkinsRestorer daje każdemu graczowi skina konta Minecraft Java o tej samej nazwie. Gracz bez takiego konta wybierze skina sam: /skin set <nazwa gracza> albo /skin url <link do skina>.') }}</div>
            <div v-if="!d.supported" class="tiny warn-text">{{ $t(d.reason) }}</div>
            <div v-else-if="d.available === false && !d.enabled" class="tiny warn-text">{{ d.error || $t('Brak wersji dla tego silnika i wersji Minecrafta.') }}</div>
          </div>
          <Toggle :model-value="d.enabled" :disabled="busy || !d.supported || (!d.enabled && d.available === false)" @update:model-value="save" />
        </div>
        <div v-if="d.online_mode" class="callout info small"><Info /><div>{{ $t('Ten serwer działa w trybie online-mode – gracze i tak mają swoje skiny. SkinsRestorer przyda się tu tylko do zmiany skina komendą /skin.') }}</div></div>
        <div v-if="d.enabled" class="callout small bedrock"><Smartphone /><div>{{ $t('Gracze Bedrock (Geyser) dostają domyślnego skina albo skina konta Java o tej samej nazwie – nie swojego skina z Bedrocka. Mogą ustawić skina komendą /skin.') }}</div></div>
        <div v-if="restart" class="callout warn small">
          <RotateCcw />
          <div class="grow">{{ $t('Zmiana zadziała po restarcie serwera.') }}</div>
          <button class="btn sm" @click="doRestart">{{ $t('Restartuj teraz') }}</button>
        </div>
      </template>
    </div>
  </section>
</template>
<script setup>
import { onMounted, ref } from 'vue'
import { Info, Loader2, RotateCcw, Shirt, Smartphone } from 'lucide-vue-next'
import Toggle from './Toggle.vue'
import { api } from '../api'
import { t } from '../i18n'
import { toast, toastError } from '../store'

const props = defineProps({ serverId: String })
const d = ref(null)
const busy = ref(false)
const restart = ref(false)

async function load() {
  try { d.value = await api.get(`/servers/${props.serverId}/skins`) } catch (e) { toastError(e) }
}
onMounted(load)

async function save(on) {
  busy.value = true
  try {
    const r = await api.post(`/servers/${props.serverId}/skins`, { enabled: on })
    d.value = r
    if (r.restart_required) restart.value = true
    toast(t(on ? 'SkinsRestorer włączony' : 'SkinsRestorer wyłączony'), 'success')
  } catch (e) { toastError(e) } finally { busy.value = false }
}

async function doRestart() {
  try {
    await api.post(`/servers/${props.serverId}/power`, { action: 'restart' })
    restart.value = false
  } catch (e) { toastError(e) }
}
</script>
<style scoped>
.hi { width: 16px; height: 16px; color: var(--muted); flex: none; }
.warn-text { color: var(--warn); }
.callout.bedrock { background: var(--surface-2); }
</style>
