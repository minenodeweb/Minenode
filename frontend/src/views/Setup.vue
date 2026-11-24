<template>
  <div class="setup">
    <div class="box">
      <div class="brand">
        <BrandMark />
        <div>
          <div class="brand-name">Varden</div>
          <div class="small muted">{{ $t('Pierwsza konfiguracja panelu') }}</div>
        </div>
      </div>

      <ol class="steps" :aria-label="$t('Kroki konfiguracji')">
        <li v-for="(s, i) in steps" :key="s.id" :class="{ on: i === index, done: i < index }">
          <span class="num"><Check v-if="i < index" /><template v-else>{{ i + 1 }}</template></span>
          <span class="label">{{ $t(s.label) }}</span>
        </li>
      </ol>

      <!-- ---------------------------------------------------------- language -->
      <section v-if="step === 'language'" class="col" style="gap: 18px">
        <div>
          <h1>{{ $t('Wybierz język') }}</h1>
          <p class="muted lead">{{ $t('Domyślny język panelu. Każdy użytkownik może go potem zmienić dla siebie w menu.') }}</p>
        </div>
        <div class="langs" role="radiogroup">
          <button v-for="l in LANGS" :key="l.id" type="button" role="radio" class="lang" :aria-checked="language === l.id"
                  :class="{ on: language === l.id }" @click="pickLanguage(l.id)">
            <span class="code">{{ l.code }}</span>
            <span class="col" style="gap: 2px; text-align: left">
              <b>{{ l.name }}</b>
              <span class="tiny muted">{{ l.note }}</span>
            </span>
          </button>
        </div>
        <div class="callout info small"><Info /><div>{{ $t('Ten wybór ustawia też domyślne teksty widoczne na liście serwerów w grze (MOTD, gdy serwer jest wyłączony lub się uruchamia). Późniejsza zmiana języka panelu ich nie zmienia – edytujesz je w Ustawieniach.') }}</div></div>
        <div class="actions"><span class="spacer" /><button class="btn primary" @click="next">{{ $t('Dalej') }} <ArrowRight /></button></div>
      </section>

      <!-- ---------------------------------------------------------- account -->
      <form v-else-if="step === 'account'" class="col" style="gap: 14px" @submit.prevent="createAccount">
        <div>
          <h1>{{ $t('Konto administratora') }}</h1>
          <p class="muted lead">{{ $t('Ten użytkownik będzie miał pełny dostęp do panelu. Kolejne konta dodasz później w Ustawieniach.') }}</p>
        </div>
        <div class="field">
          <label>{{ $t('Nazwa użytkownika') }}</label>
          <input v-model="username" class="input" autocomplete="username" autofocus required maxlength="32" />
          <span class="hint">{{ $t('Litery, cyfry oraz _ - . (do 32 znaków).') }}</span>
        </div>
        <div class="field">
          <label>{{ $t('Hasło') }}</label>
          <input v-model="password" class="input" type="password" autocomplete="new-password" required />
          <span class="hint">{{ $t('Minimum 8 znaków.') }}</span>
        </div>
        <div class="field">
          <label>{{ $t('Powtórz hasło') }}</label>
          <input v-model="password2" class="input" type="password" autocomplete="new-password" required />
        </div>
        <div v-if="error" class="callout warn small"><AlertTriangle /><div>{{ $tm(error) }}</div></div>
        <div class="actions">
          <button type="button" class="btn ghost" @click="back"><ArrowLeft /> {{ $t('Wstecz') }}</button>
          <span class="spacer" />
          <button class="btn primary" :disabled="busy"><Loader2 v-if="busy" class="spin" /> {{ $t('Utwórz konto') }} <ArrowRight /></button>
        </div>
      </form>

      <!-- ---------------------------------------------------------- domain & ports -->
      <section v-else-if="step === 'domain'" class="col" style="gap: 16px">
        <div>
          <h1>{{ $t('Domena i porty') }}</h1>
          <p class="muted lead">{{ $t('Gracze łączą się pod adresem subdomena.twoja-domena, np. survival.mc.example.com. Panel rozpoznaje serwer po nazwie wpisanej przez gracza.') }}</p>
        </div>
        <div class="field">
          <label :class="{ bad: !cleanDomains.length }">{{ $t('Domena główna') }}<AlertCircle v-if="!cleanDomains.length" class="bi" /></label>
          <div class="col" style="gap: 6px">
            <div v-for="(d, i) in domains" :key="i" class="input-group">
              <input v-model="domains[i]" class="input mono" :class="{ bad: !cleanDomains.length }" :placeholder="i ? $t('np. mc.druga-domena.pl') : 'mc.example.com'" />
              <button v-if="i" type="button" class="btn addon-btn" :title="$t('Usuń domenę')" @click="domains.splice(i, 1)"><Trash2 /></button>
            </div>
            <button type="button" class="btn sm ghost" style="align-self: flex-start" @click="domains.push('')"><Plus /> {{ $t('Dodaj kolejną domenę') }}</button>
          </div>
          <span v-if="cleanDomains.length" class="hint">{{ $t('Dodaj w DNS rekord') }} <span v-for="(d, i) in cleanDomains" :key="d" class="mono">{{ i ? ', ' : '' }}*.{{ d }} → A → {{ $t('IP serwera') }}</span></span>
          <span v-else class="hint bad">{{ $t('Bez domeny serwery dostaną tylko przykładowe adresy. Możesz ją dodać teraz albo później w Ustawieniach.') }}</span>
        </div>
        <div class="field">
          <label>{{ $t('Publiczny adres (opcjonalnie)') }}</label>
          <input v-model="publicAddress" class="input mono" :placeholder="$t('np. 203.0.113.10')" />
          <span class="hint">{{ $t('Publiczne IP lub nazwa tego komputera – używane, gdy nie ma domeny, oraz dla graczy Bedrock.') }}</span>
        </div>

        <div class="ports">
          <div class="row" style="gap: 8px"><ShieldAlert class="bi warn" /><b class="small">{{ $t('Upewnij się, że te porty są otwarte') }}</b></div>
          <p class="tiny muted" style="margin: 2px 0 8px">{{ $t('W routerze (przekierowanie portów na ten komputer), w zaporze systemu i w docker-compose. Bez nich gracze spoza Twojej sieci nie połączą się z serwerami.') }}</p>
          <table>
            <tr v-for="p in info?.ports || []" :key="p.port + p.proto"><td class="mono">{{ p.port }}</td><td class="mono muted">{{ p.proto }}</td><td>{{ $t(p.what) }}</td></tr>
          </table>
          <label class="ack"><input v-model="portsChecked" type="checkbox" /> {{ $t('Sprawdziłem, że porty są otwarte (lub zrobię to przed zaproszeniem graczy)') }}</label>
        </div>
        <div v-if="error" class="callout warn small"><AlertTriangle /><div>{{ $tm(error) }}</div></div>
        <div class="actions">
          <button type="button" class="btn ghost" @click="back"><ArrowLeft /> {{ $t('Wstecz') }}</button>
          <span class="spacer" />
          <button class="btn primary" :disabled="!portsChecked" @click="next">{{ $t('Dalej') }} <ArrowRight /></button>
        </div>
      </section>

      <!-- ---------------------------------------------------------- resource packs -->
      <section v-else-if="step === 'packs'" class="col" style="gap: 16px">
        <div>
          <h1>{{ $t('Paczki zasobów') }}</h1>
          <p class="muted lead">{{ $t('Gracze pobierają paczki zasobów serwerów prosto z panelu, przez port Minecrafta – nie trzeba otwierać dodatkowego portu. Domyślne ustawienie pasuje prawie zawsze.') }}</p>
        </div>
        <div class="field">
          <label>{{ $t('Subdomena paczek zasobów') }}</label>
          <div class="input-group">
            <input v-model="packSub" class="input mono" placeholder="packs" />
            <span class="addon">.{{ cleanDomains[0] || publicAddress.trim() || $t('twoja-domena') }}:{{ info?.proxy_port || 25565 }}</span>
          </div>
          <span class="hint">{{ $t('Adres paczek:') }} <span class="mono">{{ packUrl }}</span></span>
        </div>
        <details class="adv" :open="!!panelUrl">
          <summary class="small text-2">{{ $t('Własny adres hostingu paczek (zaawansowane)') }}</summary>
          <div class="field" style="margin-top: 10px">
            <input v-model="panelUrl" class="input mono" placeholder="https://packs.example.com" />
            <span class="hint">{{ $t('Zostaw puste, aby używać portu Minecrafta. Wpisz adres tylko wtedy, gdy serwujesz paczki przez własny reverse proxy HTTP(S).') }}</span>
          </div>
        </details>
        <div v-if="error" class="callout warn small"><AlertTriangle /><div>{{ $tm(error) }}</div></div>
        <div class="actions">
          <button type="button" class="btn ghost" @click="back"><ArrowLeft /> {{ $t('Wstecz') }}</button>
          <span class="spacer" />
          <button class="btn primary" :disabled="busy" @click="finish"><Loader2 v-if="busy" class="spin" /><Check v-else /> {{ $t('Zakończ konfigurację') }}</button>
        </div>
      </section>
    </div>
  </div>
</template>
<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { AlertCircle, AlertTriangle, ArrowLeft, ArrowRight, Check, Info, Loader2, Plus, ShieldAlert, Trash2 } from 'lucide-vue-next'
import BrandMark from '../components/BrandMark.vue'
import { api } from '../api'
import { i18n, setLocale, t } from '../i18n'
import { auth, toast } from '../store'

const LANGS = [
  { id: 'en', code: 'EN', name: 'English', note: 'Panel and server list texts in English' },
  { id: 'pl', code: 'PL', name: 'Polski', note: 'Panel i teksty listy serwerów po polsku' },
]
const ALL_STEPS = [
  { id: 'language', label: 'Język' },
  { id: 'account', label: 'Konto' },
  { id: 'domain', label: 'Domena i porty' },
  { id: 'packs', label: 'Paczki zasobów' },
]

const router = useRouter()
// The account already exists when the wizard is resumed after a reload: its step is left out.
const steps = computed(() => ALL_STEPS.filter((s) => s.id !== 'account' || auth.setupStep === 'account' || accountJustCreated.value))
const index = ref(0)
const step = computed(() => steps.value[index.value]?.id)
const accountJustCreated = ref(false)

const language = ref(i18n.locale === 'pl' ? 'pl' : 'en')
const username = ref('')
const password = ref('')
const password2 = ref('')
const domains = ref([''])
const publicAddress = ref('')
const portsChecked = ref(false)
const packSub = ref('packs')
const panelUrl = ref('')
const info = ref(null)
const error = ref('')
const busy = ref(false)

const cleanDomains = computed(() => [...new Set(domains.value.map((d) => d.trim().toLowerCase().replace(/^\*\./, '').replace(/^\.+|\.+$/g, '')).filter(Boolean))])
const packUrl = computed(() => {
  if (panelUrl.value.trim()) return panelUrl.value.trim().replace(/\/+$/, '') + '/rp/…'
  const host = cleanDomains.value[0] ? `${packSub.value.trim() || 'packs'}.${cleanDomains.value[0]}` : (publicAddress.value.trim() || t('twoja-domena'))
  return `http://${host}:${info.value?.proxy_port || 25565}/rp/…`
})

function pickLanguage(l) {
  language.value = l
  setLocale(l)  // the wizard switches right away
}

async function loadInfo() {
  try {
    info.value = await api.get('/setup/info')
    if (info.value.domains?.length) domains.value = [...info.value.domains]
    publicAddress.value = info.value.public_address || ''
    packSub.value = info.value.pack_subdomain || 'packs'
    panelUrl.value = info.value.public_panel_url || ''
    if (info.value.language) language.value = info.value.language
  } catch { /* not logged in yet: loaded after the account step */ }
}

onMounted(() => {
  if (!auth.setupRequired && !auth.user) { router.replace({ name: 'login' }); return }
  if (auth.user) loadInfo()
})
watch(step, () => { error.value = '' })

function next() { if (index.value < steps.value.length - 1) index.value++ }
function back() { if (index.value > 0) index.value-- }

async function createAccount() {
  error.value = ''
  if (password.value !== password2.value) { error.value = t('Hasła nie są identyczne'); return }
  busy.value = true
  try {
    const r = await api.post('/auth/setup', { username: username.value, password: password.value })
    auth.user = r.user
    auth.setupRequired = false
    auth.setupStep = 'panel'
    accountJustCreated.value = true
    await loadInfo()
    next()
  } catch (e) { error.value = e.message } finally { busy.value = false }
}

async function finish() {
  error.value = ''
  busy.value = true
  try {
    const r = await api.post('/setup/panel', {
      language: language.value, base_domain: cleanDomains.value[0] || '', extra_domains: cleanDomains.value.slice(1),
      public_address: publicAddress.value.trim(), pack_subdomain: packSub.value.trim() || 'packs',
      public_panel_url: panelUrl.value.trim(),
    })
    auth.setupStep = null
    auth.language = language.value
    toast(t('Panel jest gotowy – utwórz pierwszy serwer'), 'success')
    router.replace('/')
  } catch (e) { error.value = e.message } finally { busy.value = false }
}
</script>
<style scoped>
.setup { min-height: 100vh; display: grid; place-items: center; padding: 40px 16px; background-color: var(--bg);
  background-image: linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px);
  background-size: 32px 32px; background-position: -1px -1px; }
.box { width: 100%; max-width: 620px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
  padding: 32px 36px; display: flex; flex-direction: column; gap: 24px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.25); }
.brand { display: flex; align-items: center; gap: 12px; }
.brand-name { font-weight: 650; font-size: 17px; }
h1 { font-size: 22px; margin: 0; }
.lead { margin: 6px 0 0; }
.steps { list-style: none; margin: 0; padding: 0; display: flex; gap: 6px; flex-wrap: wrap; }
.steps li { display: flex; align-items: center; gap: 7px; padding: 5px 10px 5px 5px; border-radius: 999px; border: 1px solid var(--border);
  color: var(--muted); font-size: 12.5px; font-weight: 550; }
.steps li.on { border-color: var(--accent); color: var(--text); }
.steps li.done { color: var(--text-2); }
.num { width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center; background: var(--surface-3); font-size: 11.5px; }
.steps li.on .num { background: var(--accent); color: var(--accent-ink); }
.steps li.done .num { background: var(--accent-soft); color: var(--accent); }
.num svg { width: 12px; height: 12px; }
.langs { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.lang { display: flex; align-items: center; gap: 12px; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--surface-2); color: var(--text); font: inherit; cursor: pointer; }
.lang:hover { border-color: var(--border-strong); }
.lang.on { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.code { width: 38px; height: 38px; border-radius: 9px; display: grid; place-items: center; background: var(--surface-3); font-weight: 700; font-size: 13px; }
.lang.on .code { background: var(--accent); color: var(--accent-ink); }
.actions { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
.actions .btn svg { width: 15px; height: 15px; }
.bi { width: 15px; height: 15px; }
.warn { color: var(--warn); }
label.bad, .hint.bad { color: var(--danger); }
label.bad { display: inline-flex; align-items: center; gap: 5px; }
.input.bad { border-color: var(--danger); box-shadow: 0 0 0 1px var(--danger) inset; }
.addon-btn { border-radius: 0 var(--radius-sm) var(--radius-sm) 0; margin-left: -1px; height: auto; }
.addon-btn svg { width: 15px; height: 15px; }
.ports { padding: 14px 16px; border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--border)); border-radius: var(--radius);
  background: var(--warn-soft); }
.ports table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ports td { padding: 5px 8px 5px 0; border-top: 1px solid var(--border); }
.ports td:first-child { width: 70px; font-weight: 650; }
.ports td:nth-child(2) { width: 90px; }
.ack { display: flex; align-items: flex-start; gap: 8px; margin-top: 12px; font-size: 13px; cursor: pointer; }
.ack input { margin-top: 2px; accent-color: var(--accent); }
@media (max-width: 560px) {
  .box { padding: 24px 18px; }
  .langs { grid-template-columns: 1fr; }
}
</style>
