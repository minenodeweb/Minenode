<template>
  <div class="page" style="max-width: 1080px">
    <div class="page-head">
      <div class="grow">
        <h1>{{ $t('Aktualizacje') }}</h1>
        <div class="sub">{{ $t('Składniki panelu oraz pluginy, mody, datapacki i paczki zasobów wszystkich serwerów w jednym miejscu.') }}</div>
      </div>
      <button class="btn" :disabled="checking" @click="refreshAll"><RotateCcw :class="{ spin: checking }" /> {{ $t('Sprawdź ponownie') }}</button>
    </div>

    <div class="col" style="gap: 16px">
      <!-- ---------------------------------------------------------- panel components -->
      <section class="card">
        <div class="card-head"><Boxes class="hi" /><h3>{{ $t('Składniki panelu') }}</h3></div>
        <div v-if="!components" class="empty"><Loader2 class="spin" /></div>
        <div v-else class="rows">
          <div v-for="c in components" :key="c.id" class="comp">
            <div class="grow col" style="gap: 3px; min-width: 0">
              <div class="row wrap" style="gap: 8px">
                <b>{{ c.title }}</b>
                <span class="mono small">{{ c.version || '—' }}</span>
                <span v-if="c.update_available" class="badge amber"><ArrowUpCircle class="bi" /> {{ $t('dostępna {v}', { v: c.latest }) }}</span>
                <span v-else-if="c.installed && c.latest" class="badge green">{{ $t('aktualny') }}</span>
                <span v-if="c.testing" class="badge"><FlaskConical class="bi" /> {{ $t('testowa (PR)') }}</span>
              </div>
              <div class="tiny muted">{{ $t(c.about) }}</div>
              <div v-if="c.note" class="tiny muted"><Info class="bi" /> {{ $t(c.note) }}</div>
              <div v-if="c.id === 'geyser'" class="row wrap" style="gap: 8px; margin-top: 6px">
                <div class="seg">
                  <button type="button" :class="{ active: geyser.channel === 'stable' }" :disabled="busy.geyser" @click="setChannel('stable')">{{ $t('Stabilna') }}</button>
                  <button type="button" :class="{ active: geyser.channel === 'preview' }" :disabled="busy.geyser" @click="geyser.channel = 'preview'">{{ $t('Testowa (PR)') }}</button>
                </div>
                <template v-if="geyser.channel === 'preview'">
                  <span class="small text-2">PR #</span>
                  <input v-model="geyser.pr" class="input mono pr" inputmode="numeric" maxlength="6" @keydown.enter.prevent="setChannel('preview')" />
                  <button type="button" class="btn sm" :disabled="busy.geyser || !geyser.pr" @click="setChannel('preview')">{{ $t('Zastosuj') }}</button>
                </template>
              </div>
            </div>
            <a v-if="c.url" :href="c.url" target="_blank" rel="noopener" class="btn sm ghost icon" :title="$t('Strona projektu')"><ExternalLink /></a>
            <button v-if="c.action === 'update'" class="btn sm" :class="{ primary: c.update_available }" :disabled="busy[c.id]" @click="updateComponent(c)">
              <Loader2 v-if="busy[c.id]" class="spin" /><Download v-else /> {{ $t(c.update_available ? 'Aktualizuj' : 'Sprawdź i pobierz') }}</button>
            <span v-else class="badge" :title="$t(c.note || '')">{{ $t(c.action === 'local' ? 'lokalny plik' : 'w obrazie MineNode') }}</span>
          </div>
        </div>
      </section>

      <!-- ---------------------------------------------------------- servers -->
      <section class="card">
        <div class="card-head"><Server class="hi" /><h3>{{ $t('Serwery') }}</h3>
          <span v-if="totalAvailable" class="badge amber">{{ $t('{n} do aktualizacji', { n: totalAvailable }) }}</span></div>
        <div class="card-body col" style="gap: 6px">
          <div class="small muted">{{ $t('Nowe wersje pochodzą zawsze z Modrinth i muszą być oznaczone dla wersji Minecrafta serwera. Pliki wgrane ręcznie oraz pliki z modpacków są pokazane, ale panel nigdy ich nie zmienia.') }}</div>
          <div v-if="!servers" class="empty"><Loader2 class="spin" /></div>
          <div v-else-if="!servers.length" class="empty small muted">{{ $t('Brak serwerów.') }}</div>
          <div v-for="s in servers" :key="s.id" class="srv" :class="{ open: open[s.id] }">
            <div class="srv-head" role="button" tabindex="0" @click="toggle(s.id)" @keydown.enter="toggle(s.id)">
              <ChevronRight class="chev" />
              <div class="grow col" style="gap: 2px; min-width: 0">
                <b class="ellipsis">{{ s.name }}</b>
                <span class="tiny muted">{{ engineName(s.type) }} <span class="mono">{{ s.mc_version }}</span></span>
              </div>
              <span v-if="state[s.id]?.loading" class="tiny muted row" style="gap: 6px"><Loader2 class="spin bi" /> {{ $t('sprawdzanie…') }}</span>
              <span v-else-if="state[s.id]?.error" class="badge red" :title="state[s.id].error">{{ $t('błąd sprawdzania') }}</span>
              <span v-else-if="state[s.id]?.data?.available" class="badge amber">{{ $t('aktualizacje: {n}', { n: state[s.id].data.available }) }}</span>
              <span v-else-if="state[s.id]?.data" class="badge green"><Check class="bi" /> {{ $t('aktualne') }}</span>
              <button class="btn sm" :class="{ primary: state[s.id]?.data?.available }" :disabled="!state[s.id]?.data?.available || state[s.id]?.applying"
                      @click.stop="apply(s, null)">
                <Loader2 v-if="state[s.id]?.applying === 'all'" class="spin" /><Download v-else /> {{ $t('Aktualizuj wszystkie') }}</button>
            </div>

            <div v-if="open[s.id]" class="srv-body">
              <div v-if="!state[s.id]?.data" class="empty"><Loader2 class="spin" /></div>
              <template v-else>
                <div v-if="!state[s.id].data.items.length" class="small muted">{{ $t('Ten serwer nie ma pluginów, modów, datapacków ani paczek zasobów.') }}</div>
                <div v-for="g in groups(state[s.id].data.items)" :key="g.kind" class="group">
                  <div class="group-head">
                    <span class="small"><b>{{ $t(KIND[g.kind]) }}</b> <span class="muted">· {{ g.items.length }}</span></span>
                  </div>
                  <div v-for="it in g.items" :key="it.id" class="item" :class="{ skip: it.skip, off: !it.enabled }">
                    <img v-if="it.icon" :src="it.icon" class="pic" alt="" referrerpolicy="no-referrer" loading="lazy" @error="(e) => (e.target.style.visibility = 'hidden')" />
                    <div v-else class="pic ph"><Package /></div>
                    <div class="grow col" style="gap: 2px; min-width: 0">
                      <div class="row wrap" style="gap: 6px">
                        <span class="ellipsis title">{{ it.title }}</span>
                        <span v-if="!it.enabled && it.kind !== 'resourcepack'" class="badge">{{ $t('wyłączony') }}</span>
                        <span v-if="it.kind === 'resourcepack' && it.enabled" class="badge green">{{ $t('aktywna') }}</span>
                      </div>
                      <div class="tiny muted ellipsis">
                        <span class="mono">{{ it.version || '?' }}</span>
                        <template v-if="it.update_available"> <ArrowRight class="bi" /> <span class="mono new">{{ it.latest }}</span></template>
                        <template v-else-if="it.skip"> · {{ $t(it.skip) }}</template>
                        <template v-else-if="it.error"> · {{ $t(it.error) }}</template>
                        <template v-else-if="it.note"> · {{ $t(it.note) }}</template>
                        <template v-else> · {{ $t('aktualny') }}</template>
                      </div>
                    </div>
                    <button v-if="it.update_available" class="btn sm" :disabled="!!state[s.id].applying" @click="apply(s, [it.id], it)">
                      <Loader2 v-if="state[s.id].applying === it.id" class="spin" /><Download v-else /> {{ $t('Aktualizuj') }}</button>
                  </div>
                </div>
                <div v-if="state[s.id].data.errors.length" class="tiny muted">{{ state[s.id].data.errors.join(' · ') }}</div>
              </template>
            </div>

            <div v-if="state[s.id]?.restart" class="callout warn small restart">
              <RotateCcw /><div class="grow">{{ $t('Zaktualizowane pliki zadziałają po restarcie serwera.') }}</div>
              <button class="btn sm" @click="restart(s)">{{ $t('Restartuj teraz') }}</button>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ArrowRight, ArrowUpCircle, Boxes, Check, ChevronRight, Download, ExternalLink, FlaskConical, Info, Loader2, Package, RotateCcw, Server } from 'lucide-vue-next'
import { api } from '../api'
import { t } from '../i18n'
import { confirmDialog, toast, toastError } from '../store'
import { ENGINE_META } from '../utils'

const KIND = { plugin: 'Pluginy', mod: 'Mody', datapack: 'Datapacki', resourcepack: 'Paczki zasobów' }
const ORDER = ['plugin', 'mod', 'datapack', 'resourcepack']

const components = ref(null)
const servers = ref(null)
const state = reactive({})   // server id -> { loading, data, error, applying, restart }
const open = reactive({})
const busy = reactive({})
const geyser = reactive({ channel: 'stable', pr: '' })
const checking = ref(false)

const engineName = (type) => ENGINE_META[type]?.name || type
const totalAvailable = computed(() => Object.values(state).reduce((n, s) => n + (s.data?.available || 0), 0))

function groups(items) {
  return ORDER.map((kind) => {
    const list = items.filter((i) => i.kind === kind)
    // updates first, then the rest alphabetically
    list.sort((a, b) => (b.update_available - a.update_available) || a.title.localeCompare(b.title))
    return { kind, items: list, updatable: list.filter((i) => i.update_available) }
  }).filter((g) => g.items.length)
}

async function loadComponents(refresh = false) {
  try {
    components.value = await api.get(`/updates/components${refresh ? '?refresh=true' : ''}`)
    const g = components.value.find((c) => c.id === 'geyser')
    if (g) { geyser.channel = g.channel; geyser.pr = (g.preview || '').replace(/^pr\./, '') }
  } catch (e) { toastError(e) }
}

async function checkServer(id, refresh = false) {
  const st = (state[id] ||= {})
  st.loading = true
  st.error = null
  try {
    st.data = await api.get(`/updates/servers/${id}${refresh ? '?refresh=true' : ''}`)
  } catch (e) { st.error = e.message } finally { st.loading = false }
}

async function checkServers(refresh = false) {
  const queue = [...(servers.value || [])]
  // two servers at a time: every check asks Modrinth about all of a server's files
  const worker = async () => { while (queue.length) await checkServer(queue.shift().id, refresh) }
  await Promise.all([worker(), worker()])
}

async function load(refresh = false) {
  checking.value = true
  try {
    const [, list] = await Promise.all([loadComponents(refresh), api.get('/updates/servers')])
    servers.value = list
    await checkServers(refresh)
  } catch (e) { toastError(e) } finally { checking.value = false }
}
onMounted(() => load())
const refreshAll = () => load(true)

function toggle(id) { open[id] = !open[id] }
// ids = null: every available update of the server ("Aktualizuj wszystkie", confirmed first);
// one id: the single file whose "Aktualizuj" button was pressed.
async function apply(s, ids, item = null) {
  const st = state[s.id]
  const n = ids ? ids.length : st?.data?.available
  if (!n || st.applying) return
  if (!item && !(await confirmDialog({
    title: t('Zaktualizować pliki ({n}) na serwerze {s}?', { n, s: s.name }),
    message: t('Panel pobierze nowe wersje z Modrinth i podmieni stare pliki. Wyłączone pliki pozostaną wyłączone. Warto mieć świeżą kopię zapasową.'),
    confirmText: t('Aktualizuj wszystkie'),
  }))) return
  st.applying = item ? item.id : 'all'
  try {
    const r = await api.post(`/updates/servers/${s.id}`, { items: ids })
    st.data = r.server
    if (r.restart_required) st.restart = true
    if (r.failed.length) toast(t('Zaktualizowano {ok}, nieudane: {bad} – szczegóły w konsoli serwera', { ok: r.updated.length, bad: r.failed.length }), 'error', 6000)
    else toast(t('Zaktualizowano pliki: {n}', { n: r.updated.length }), 'success')
  } catch (e) { toastError(e) } finally { st.applying = null }
}

async function restart(s) {
  try {
    await api.post(`/servers/${s.id}/power`, { action: 'restart' })
    state[s.id].restart = false
    toast(t('Restartowanie serwera {s}', { s: s.name }), 'success')
  } catch (e) { toastError(e) }
}

async function updateComponent(c) {
  busy[c.id] = true
  try {
    const r = await api.post(`/updates/components/${c.id}`)
    components.value = r.components
    toast(t('{c}: pobieranie najnowszej wersji rozpoczęte', { c: c.title }), 'success')
    setTimeout(() => loadComponents(true), 8000)
  } catch (e) { toastError(e) } finally { busy[c.id] = false }
}

async function setChannel(channel) {
  if (channel === 'preview' && !/^[0-9]{1,6}$/.test(String(geyser.pr).trim())) { toast(t('Podaj numer PR Geysera, np. 6873'), 'error'); return }
  if (channel === 'preview' && !(await confirmDialog({
    title: t('Przełączyć na testową wersję Geysera?'),
    message: t('Panel pobierze kompilację testową z PR i zrestartuje bramkę Bedrock – gracze Bedrock zostaną na chwilę rozłączeni. Serwery Java działają dalej. Do wersji stabilnej wrócisz jednym kliknięciem.'),
    confirmText: t('Przełącz'),
  }))) return
  busy.geyser = true
  try {
    await api.put('/bedrock/geyser', channel === 'preview' ? { channel, preview: geyser.pr } : { channel })
    toast(channel === 'preview' ? t('Geyser: wersja testowa PR #{n}', { n: geyser.pr }) : t('Geyser: wersja stabilna'), 'success')
    await loadComponents(true)
  } catch (e) { toastError(e) } finally { busy.geyser = false }
}
</script>
<style scoped>
.hi { width: 16px; height: 16px; color: var(--muted); }
.bi { width: 13px; height: 13px; vertical-align: -2px; }
.rows { display: flex; flex-direction: column; }
.comp { display: flex; align-items: center; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--border); }
.comp:last-child { border-bottom: 0; }
.seg { display: inline-flex; padding: 3px; border-radius: 9px; background: var(--surface-2); border: 1px solid var(--border); }
.seg button { height: 26px; padding: 0 12px; border: 0; border-radius: 7px; background: none; color: var(--text-2); font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; }
.seg button.active { background: var(--surface-3); color: var(--text); }
.pr { width: 90px; height: 30px; }

.srv { border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.srv + .srv { margin-top: 2px; }
.srv-head { display: flex; align-items: center; gap: 12px; padding: 12px 14px; cursor: pointer; border-radius: var(--radius); }
.srv-head:hover { background: var(--surface-2); }
.srv-head:focus-visible { outline: none; box-shadow: var(--ring); }
.chev { width: 16px; height: 16px; color: var(--muted); transition: transform 0.15s; flex: none; }
.srv.open .chev { transform: rotate(90deg); }
.srv-body { padding: 4px 14px 14px 42px; display: flex; flex-direction: column; gap: 14px; }
.group { display: flex; flex-direction: column; gap: 2px; }
.group-head { display: flex; align-items: center; gap: 10px; padding: 4px 0 6px; border-bottom: 1px solid var(--border); margin-bottom: 4px; }
.item { display: flex; align-items: center; gap: 10px; padding: 6px 0; min-width: 0; }
.item.skip .title, .item.skip .pic { opacity: 0.6; }
.title { font-size: 13.5px; font-weight: 550; }
.new { color: var(--warn); }
.pic { width: 30px; height: 30px; border-radius: 7px; object-fit: cover; background: var(--surface-3); flex: none; image-rendering: pixelated; }
.pic.ph { display: grid; place-items: center; color: var(--muted); }
.pic.ph svg { width: 15px; height: 15px; }
.restart { margin: 0 14px 14px; }
@media (max-width: 640px) {
  .comp { flex-wrap: wrap; padding: 14px 16px; }
  .srv-body { padding-left: 14px; }
}
</style>
