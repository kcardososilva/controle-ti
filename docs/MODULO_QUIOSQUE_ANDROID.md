# Módulo Quiosque — App Android + Integração com o Sistema de Controle de TI

> **Para que serve este documento**
> Especificação completa para construir, em **outra sessão do Claude Code**, um aplicativo Android em **modo quiosque** que coleta dados do aparelho e os envia, via integração HTTPS, para o **Sistema de Controle de TI (Django)** da Santa Colomba Agropecuária.
> Ele é autocontido: traz a arquitetura, a stack, o contrato da API (request/response), o passo a passo do app e o que precisa existir no lado do servidor.

> ✅ **Backend já implementado.** O lado servidor (Seções 4 e 9) **já existe e está testado** no sistema Django: models `KioskDevice`/`KioskCheckin`/`KioskMatricula`/`KioskComando`, endpoints `/api/quiosque/enroll|checkin|config|comando/<id>/ack`, autenticação por token e dashboard interno `/quiosque/`. O app Android só precisa **consumir** o contrato da Seção 4.
> Observação de formato: as respostas da API vêm com um envelope — `enroll` retorna `{ "ok": true, "device_uuid", "token", "config" }`; `config` retorna `{ "ok": true, "config": {...} }`. Em erro: `{ "ok": false, "erro": "..." }` com HTTP 4xx/5xx.

---

## 0. Como usar este documento em uma nova sessão do Claude Code

1. Crie uma pasta nova **separada** do sistema Django, por exemplo:
   `C:\Users\<voce>\Área de Trabalho\Quiosque-Android\`
2. Copie este arquivo (`MODULO_QUIOSQUE_ANDROID.md`) para dentro dela.
3. Abra o terminal nessa pasta e rode `claude`.
4. Primeiro prompt sugerido:
   > "Leia o arquivo MODULO_QUIOSQUE_ANDROID.md por completo. Vamos construir o app Android de quiosque descrito nele, começando pela Fase 1 (MVP de telemetria). Use Kotlin nativo, mantendo o consumo de RAM baixo conforme as restrições. Crie a estrutura do projeto Gradle."
5. O lado do servidor (endpoints `/api/quiosque/...`) é construído **no projeto Django existente**, em outra sessão. Este documento define o **contrato** que os dois lados devem respeitar (Seção 4).

> ⚠️ **Mantenha os dois projetos separados.** O app Android **não** compartilha código nem banco com o Django. A única ligação é a API HTTPS descrita na Seção 4.

---

## 1. Objetivo e requisitos

Aplicativo Android que roda em **modo quiosque** (tela travada em um único app/launcher controlado), instalado em celulares corporativos, com as seguintes regras:

| # | Requisito | Como é atendido |
|---|---|---|
| R1 | Rodar em aparelhos com **2–4 GB de RAM** sem pesar | Kotlin nativo, libs mínimas, R8/shrink, 1 processo, serviço leve (Seção 3) |
| R2 | **Coletar informações do aparelho** | serial, modelo, fabricante, versão Android, bateria, rede, localização (Seção 5) |
| R3 | **Senha para controle do TI** | PIN do TI valida saída do quiosque/configurações (Seção 6) |
| R4 | **Permitir apenas conexões via Wi-Fi** | Política de Device Owner + checagem de transporte na camada de rede (Seção 7) |
| R5 | **Permitir apenas apps liberados pelo TI** | Lock Task allowlist + launcher do quiosque (lista vinda do servidor) (Seção 8) |
| R6 | **Permitir câmera e galeria** | Pacotes de câmera/galeria incluídos na allowlist (Seção 8) |
| R7 | **Integrar com o sistema Django** | API HTTPS com token por dispositivo (Seção 4) |
| R8 | **Não comprometer o sistema atual** | Tudo é aditivo no Django: endpoints e models novos isolados (Seção 9) |

---

## 2. Arquitetura geral

```
┌─────────────────────────────┐         HTTPS (TLS)          ┌──────────────────────────────┐
│   APK Android (Quiosque)    │   Authorization: Bearer …    │   Sistema Django (existente)  │
│   - Sem banco de dados      │ ───────────────────────────► │   /api/quiosque/enroll        │
│   - Launcher travado        │                              │   /api/quiosque/checkin       │
│   - Coleta telemetria       │ ◄─────────────────────────── │   /api/quiosque/config        │
│   - Aplica políticas do TI  │     config + comandos        │   Dashboard /quiosque/ (web)  │
└─────────────────────────────┘                              └──────────────────────────────┘
```

- O app guarda **apenas** o token e a config em cache local seguro (sem SQLite, sem banco).
- O servidor é a **fonte da verdade**: lista de apps liberados, política de Wi-Fi, PIN do TI, intervalo de check-in, comandos remotos.
- Comunicação **sempre por HTTPS** (ver pré-requisito de infra na Seção 10).

---

## 3. Stack e orçamento de memória (chave para 2–4 GB de RAM)

**Linguagem:** **Kotlin** (nativo Android). Evite **Flutter/React Native** — adicionam runtime e consumo de RAM desnecessários para um quiosque.

**Configuração do projeto:**
- `minSdkVersion = 24` (Android 7.0 — cobre aparelhos antigos), `targetSdkVersion = 34+`.
- `compileSdk` mais recente disponível.
- Build com **R8 + shrinkResources** (`minifyEnabled true`, `shrinkResources true`) → APK pequeno e menos classes em memória.
- `android:largeHeap="false"` no manifest (não peça heap grande).
- **1 processo, 1 Activity** (single-activity). Sem WebView pesada.

**Bibliotecas (mínimas — preferir o que já vem no Android):**

| Necessidade | Escolha enxuta | Por quê |
|---|---|---|
| HTTP | `HttpURLConnection` (zero dep) **ou** OkHttp | HttpURLConnection não adiciona nada; OkHttp é pequeno e mais robusto. Escolha 1. |
| JSON | `org.json` (built-in) | Evita Gson/Moshi/reflection — menos RAM. |
| Agendamento periódico | `WorkManager` (Jetpack) | Eficiente; respeita Doze. Use intervalo ≥ 15 min para periódico, ou Foreground Service para tempo real. |
| Localização | `FusedLocationProviderClient` (play-services-location) | Padrão e econômico. Se não houver Google Play, usar `LocationManager`. |
| Token/segredos | `EncryptedSharedPreferences` (security-crypto) | Guarda token cifrado, sem banco. |
| Kiosk/políticas | `DevicePolicyManager` (SDK, sem dep) | Lock Task e restrições. |

**Boas práticas de memória (obrigatórias):**
- Sempre usar `applicationContext` em serviços/singletons; **nunca** segurar referência de Activity.
- Liberar callbacks de localização em `onStop`/quando o serviço para (`removeLocationUpdates`).
- Não acumular histórico em memória — envia e descarta.
- Imagens: o app **não** carrega galeria/câmera internamente; ele só **abre** os apps nativos (sem libs de imagem).
- Coroutines com `Dispatchers.IO` para rede; sem threads soltas.
- Logs apenas em debug (`BuildConfig.DEBUG`).

**Meta de footprint:** APK < 8 MB, RAM em repouso do serviço < ~40 MB.

---

## 4. Contrato de integração com o sistema (API) — **fonte da verdade**

Base URL (produção, **HTTPS**): `https://<dominio-do-sistema>/api/quiosque/`
Formato: JSON (UTF-8). Idioma das mensagens: pt-BR.

### 4.1 Autenticação
- **Enrollment** (1ª vez): protegido por um **código de matrícula** de uso único, gerado pelo TI no dashboard.
- **Demais chamadas**: header `Authorization: Bearer <token>` + `X-Device-UUID: <uuid>`.
- O servidor guarda o **hash** do token (nunca o token puro). Token é revogável pelo dashboard.

### 4.2 `POST /enroll/` — matrícula do dispositivo
**Request:**
```json
{
  "codigo_matricula": "ABC123",
  "serial": "R58N12ABCDE",
  "android_id": "9774d56d682e549c",
  "fabricante": "Samsung",
  "modelo": "SM-A155M",
  "android_versao": "14",
  "app_versao": "1.0.0",
  "ram_mb": 4096
}
```
**Response 200:**
```json
{
  "device_uuid": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "token": "tok_live_9b1c...",
  "config": {
    "intervalo_checkin_seg": 300,
    "wifi_only": true,
    "apps_permitidos": ["com.android.camera2", "com.google.android.apps.photos"],
    "admin_pin_hash": "pbkdf2_sha256$...",
    "config_versao": 7,
    "mensagem_quiosque": "Equipamento Santa Colomba — uso corporativo"
  }
}
```
**Vínculo código ↔ aparelho — chave = `android_id`** (estável por aparelho):
1. Código **livre** → vincula ao `android_id`; reaproveita/cria o registro do aparelho.
2. Código já usado pelo **mesmo `android_id`** → reuso; devolve **o mesmo `device_uuid`** (preserva histórico).
3. Código usado por **`android_id` diferente** → **HTTP 409**.

**Erros:** `400` (código inválido/expirado/já utilizado), `409` (código já vinculado a outro aparelho).

### 4.3 `POST /checkin/` — telemetria periódica (heartbeat)
Headers: `Authorization`, `X-Device-UUID`.
**Request:**
```json
{
  "coletado_em": "2026-06-24T14:01:00-03:00",
  "serial": "R58N12ABCDE",
  "latitude": -16.6786,
  "longitude": -49.2539,
  "precisao_m": 12.4,
  "bateria": 83,
  "carregando": false,
  "rede": "wifi",
  "online": true,
  "config_versao": 7
}
```
**Response 200:**
```json
{
  "ok": true,
  "config_versao": 7,
  "config": null,
  "comandos": []
}
```
- **`coletado_em`** (ISO 8601 com fuso): instante REAL da coleta. Usado como a hora do dado; `registrado_em` (carimbado pelo servidor) = chegada.
- **Frequência (`intervalo_checkin_seg`):** faixa aceita **[5, 300]s** (o app faz clamp no dispositivo). `5s` = tempo real (recomenda-se manter a conexão HTTP viva / keep-alive); `300s` = economia de bateria/dados. O servidor é a fonte da verdade: se mandar um valor maior, ele prevalece.
- **Fila offline:** o app pode enviar leituras antigas em rajada (cada uma com seu `coletado_em` no passado). Cada leitura vira uma linha de histórico; o "estado atual" do aparelho **não regride** com leituras antigas.
- **Retenção:** o servidor mantém uma **janela móvel de 5 dias** de telemetria por aparelho (dados além disso são sobrepostos). A poda roda **de forma amostrada** nos check-ins (não a cada heartbeat, para manter a resposta leve em alta frequência); um aparelho que **parou de enviar conserva todo o seu histórico**.
- Se `config_versao` do servidor for maior que a enviada, ele devolve o objeto `config` atualizado (o app aplica e persiste).
- `comandos` (Fase 2): lista de ações pendentes (ver 4.5).

### 4.4 `GET /config/` — puxar configuração atual
Headers: `Authorization`, `X-Device-UUID`.
**Response 200:** mesmo objeto `config` do enroll. Usado no boot e quando o app detecta `config_versao` nova.

### 4.5 `POST /comando/<id>/ack/` — confirmação de comando (Fase 2)
Para controle remoto (bloquear, exibir mensagem, atualizar lista de apps).
**Request:** `{ "status": "executado", "detalhe": "" }` → **Response:** `{ "ok": true }`.

### 4.6 Regras de robustez do app
- **Offline-first:** se sem rede, o app continua funcionando com a última `config` em cache (inclusive o `admin_pin_hash` para validar o PIN offline).
- **Retry com backoff** em falha de rede; nunca travar a UI.
- **Idempotência:** check-ins são eventos; reenvio em falha é aceitável.
- **Relógio:** enviar timestamps em ISO-8601 quando aplicável; servidor carimba a chegada.

---

## 5. Coleta de informações do aparelho (R2)

| Dado | Fonte Android | Observação |
|---|---|---|
| Número de série | `Build.getSerial()` | **Só funciona como Device Owner** ou app de sistema (Android 10+). Ver caveat abaixo. |
| ID estável | `Settings.Secure.ANDROID_ID` | Sempre disponível; use como identidade principal. |
| Fabricante/Modelo | `Build.MANUFACTURER`, `Build.MODEL` | — |
| Versão Android | `Build.VERSION.RELEASE` / `SDK_INT` | — |
| Versão do app | `BuildConfig.VERSION_NAME` | — |
| RAM total | `ActivityManager.MemoryInfo.totalMem` | Para inventário. |
| Bateria | `BatteryManager` / `Intent.ACTION_BATTERY_CHANGED` | nível + carregando. |
| Rede | `ConnectivityManager` + `NetworkCapabilities` | `wifi` / `cellular` / `none`. |
| Localização | `FusedLocationProviderClient` | Só quando online; respeitar permissão. |

> ⚠️ **Caveat do número de série (Android 10+ / API 29):** apps comuns **não leem** `Build.getSerial()` (lança `SecurityException`). Só é possível sendo **Device Owner** (recomendado para frota) ou app de sistema. **Estratégia:** se Device Owner → ler serial real; senão → usar `ANDROID_ID` + o `device_uuid` do enroll como identidade, e enviar `serial = null`. O vínculo com o equipamento no sistema (model `Item`) pode ser por serial quando houver, ou manual no dashboard.

---

## 6. Senha de controle do TI (R3)

Objetivo: impedir que o usuário comum saia do quiosque ou acesse configurações; apenas o TI, com PIN, pode.

- **Gesto oculto** para abrir a tela de PIN (ex.: tocar 7× no logo, ou pressionar 2 cantos por 3s).
- **Validação do PIN:** o servidor envia `admin_pin_hash` (PBKDF2-SHA256 com salt) na `config`. O app valida o PIN digitado **offline** comparando o hash → funciona sem internet.
- **Rotação:** o TI troca o PIN no dashboard; o app recebe o novo hash na próxima `config`.
- **Ações liberadas pelo PIN:** sair do Lock Task (`stopLockTask()`), abrir configurações do app, forçar re-sync, ver diagnóstico.
- **Nunca** embutir o PIN em texto no APK. Apenas o hash trafega/fica em cache cifrado.

---

## 7. Política de rede — flag `wifi_only` (R4)

O comportamento é controlado **por dispositivo** pelo flag `wifi_only` enviado no `config` (default `true`):

- **`wifi_only = true`** — rede móvel bloqueada; o app só transmite no Wi-Fi e o atalho "Dados móveis" fica oculto. Duas camadas (defesa em profundidade):
  1. **Política (Device Owner):** desabilitar dados móveis/celular. Conforme a versão:
     - `DevicePolicyManager.addUserRestriction(admin, UserManager.DISALLOW_CONFIG_MOBILE_NETWORKS)`;
     - quando suportado, `setGlobalSetting(admin, Settings.Global.MOBILE_DATA, "0")`;
     - opção operacional mais simples: **provisionar aparelhos sem SIM / com dados desativados**.
  2. **Camada de rede:** antes de transmitir, checar `NetworkCapabilities.hasTransport(TRANSPORT_WIFI)`. Se não for Wi-Fi, **não envia** (enfileira para quando voltar ao Wi-Fi). Isso garante R4 mesmo onde a política do SO não cobrir 100%.
- **`wifi_only = false`** — para aparelhos **com chip**: permite transmissão por dados móveis e exibe o atalho "Dados móveis", que abre a tela de rede/SIM do sistema (gerenciar chip, dados, roaming) liberando as Configurações temporariamente sob o Lock Task.

A telemetria de check-in reporta o transporte em uso no campo `rede` (`"wifi"` / `"cellular"` / `"none"`) independentemente do flag.

```kotlin
fun isWifi(ctx: Context): Boolean {
    val cm = ctx.getSystemService(ConnectivityManager::class.java)
    val nc = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
    return nc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
}
```

---

## 8. Apps permitidos + câmera e galeria (R5, R6)

- Sendo **Device Owner**, definir a allowlist do Lock Task:
  ```kotlin
  dpm.setLockTaskPackages(admin, arrayOf(
      ownPackage,                       // o próprio quiosque
      "com.android.camera2",            // câmera (ajustar ao pacote do fabricante)
      "com.google.android.apps.photos", // galeria/fotos
      // + pacotes liberados pelo TI vindos da config
  ))
  ```
- A **lista de apps permitidos vem do servidor** (`config.apps_permitidos`) — o TI controla remotamente, sem reinstalar o APK.
- O **launcher do quiosque** (tela inicial do app) mostra **somente** os ícones dos apps liberados + botões de **Câmera** e **Galeria**.
  - Abrir câmera: `Intent(MediaStore.ACTION_IMAGE_CAPTURE)` ou launch do pacote da câmera.
  - Abrir galeria: `Intent(Intent.ACTION_VIEW).setType("image/*")` ou launch do app de fotos.
- Apps fora da allowlist **não abrem** no modo Lock Task (o SO bloqueia).
- Definir o app como **HOME/launcher** (intent-filter `CATEGORY_HOME`) e, como Device Owner, `addPersistentPreferredActivity` para que ele seja a tela inicial fixa.

---

## 9. O que precisa existir no sistema Django (lado servidor)

> Construído **no projeto Django existente**, de forma **aditiva** (não altera nada do que já funciona). Pode ser feito em outra sessão (peça: "crie o lado servidor do módulo quiosque conforme a Seção 4 do documento").

**Models novos** (em `models.py`, sem estender `AuditModel`, pois os dados vêm do device):
- `KioskDevice`: `device_uuid`, `token_hash`, `serial`, `android_id`, `fabricante`, `modelo`, `android_versao`, `app_versao`, `ram_mb`, `item` (FK opcional → `Item`), `ativo`, `ultimo_checkin`, `config_versao`, `criado_em`.
- `KioskCheckin`: FK `device`, `latitude`, `longitude`, `precisao_m`, `bateria`, `carregando`, `rede`, `online`, `registrado_em`.
- `KioskMatricula`: `codigo`, `usado`, `expira_em`, `criado_por` (gera o código de enroll).
- `KioskComando` *(Fase 2)*: FK `device`, `tipo`, `payload` (JSON), `status`, datas.

**Auth de dispositivo (isolada do login do site):**
- Decorator `@kiosk_token_required` que lê `Authorization: Bearer` + `X-Device-UUID`, resolve o `KioskDevice` por hash do token e injeta `request.kiosk_device`. **Nunca** usar `@login_required` nesses endpoints.
- Endpoints de API são `@csrf_exempt` (JSON + token; não afeta o CSRF do restante do site).

**Views/serviço/rotas:**
- `views/quiosque.py` (enroll, checkin, config, comando_ack) + dashboard interno (`@login_required`).
- `services/quiosque_service.py` (gerar/validar token e código, registrar check-in, montar config).
- Registrar em `views/__init__.py` e `urls.py`. Rotas de API sob `/api/quiosque/`; telas internas sob `/quiosque/`.
- **Migration aditiva** (não dropa nada → backwards-safe). SQLite em dev.

**Dashboard interno `/quiosque/`** (pt-BR, prefixo CSS `.kq-*`): lista de celulares (online/offline, último check-in, bateria, localização no mapa), detalhe com histórico de check-ins, geração de código de matrícula, definição de apps permitidos/PIN/política Wi-Fi por dispositivo ou global, e revogação de token.

---

## 10. Pré-requisito de infraestrutura (HTTPS)

Hoje a produção roda `python manage.py runserver` em **HTTP** (porta 65300). Para o celular enviar serial/localização com segurança, é **obrigatório HTTPS/TLS**:
- Reverse proxy (**nginx** ou **IIS**) na frente do Django com certificado, **ou**
- Um túnel (ex.: **Cloudflare Tunnel**) expondo o serviço com TLS.
- O domínio externo já está em `ALLOWED_HOSTS`; faltaria o certificado. Endpoints do device são `csrf_exempt`, então não dependem de `CSRF_TRUSTED_ORIGINS`.
- Recomendado: **certificate pinning** no app (fixar o certificado do servidor) para evitar interceptação.

---

## 11. Provisionamento como Device Owner (modo quiosque real)

O modo quiosque “de verdade” (Lock Task forte, ler serial, forçar Wi-Fi, allowlist) exige o app como **Device Owner**. Caminhos:

- **QR Code de provisionamento** no setup de fábrica do aparelho (recomendado para frota):
  - Aparelho **resetado** → na tela inicial, tocar 6× → leitor de QR → QR aponta para o APK (URL) + checksum + componente admin.
  - Define o app como Device Owner automaticamente.
- **ADB (para testes/poucos aparelhos):**
  ```
  adb shell dpm set-device-owner com.santacolomba.quiosque/.admin.KioskDeviceAdminReceiver
  ```
  (Aparelho **sem contas** cadastradas; ideal logo após reset.)
- Alternativa sem Device Owner: **screen pinning** (`startLockTask` com app comum) — mais simples, porém **menos seguro** (usuário pode sair) e **não** lê serial nem força Wi-Fi via política. Use só se não puder provisionar como Device Owner.

---

## 12. Estrutura de projeto sugerida (Android)

```
quiosque-android/
├── app/
│   ├── src/main/
│   │   ├── AndroidManifest.xml
│   │   ├── java/com/santacolomba/quiosque/
│   │   │   ├── App.kt                      # Application (init leve)
│   │   │   ├── ui/
│   │   │   │   ├── KioskLauncherActivity.kt # tela inicial travada (apps liberados)
│   │   │   │   └── AdminPinActivity.kt       # PIN do TI (gesto oculto)
│   │   │   ├── admin/
│   │   │   │   └── KioskDeviceAdminReceiver.kt # DeviceAdminReceiver / políticas
│   │   │   ├── kiosk/
│   │   │   │   ├── KioskPolicyManager.kt     # Lock Task, allowlist, Wi-Fi, HOME
│   │   │   │   └── PinValidator.kt           # PBKDF2 offline
│   │   │   ├── data/
│   │   │   │   ├── DeviceInfo.kt             # coleta de telemetria
│   │   │   │   ├── SecureStore.kt            # EncryptedSharedPreferences (token/config)
│   │   │   │   └── Api.kt                     # HttpURLConnection/OkHttp + org.json
│   │   │   ├── location/LocationProvider.kt
│   │   │   └── work/CheckinWorker.kt         # WorkManager periódico
│   │   └── res/...
│   └── build.gradle (R8, shrink, minSdk 24)
└── build.gradle
```

---

## 13. Trechos-chave (referência de implementação)

**AndroidManifest — permissões e HOME launcher:**
```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE"/>
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION"/>
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION"/>
<uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>

<application android:largeHeap="false" android:name=".App">
  <activity android:name=".ui.KioskLauncherActivity" android:launchMode="singleInstance">
    <intent-filter>
      <action android:name="android.intent.action.MAIN"/>
      <category android:name="android.intent.category.HOME"/>
      <category android:name="android.intent.category.DEFAULT"/>
      <category android:name="android.intent.category.LAUNCHER"/>
    </intent-filter>
  </activity>

  <receiver android:name=".admin.KioskDeviceAdminReceiver"
            android:permission="android.permission.BIND_DEVICE_ADMIN" android:exported="true">
    <meta-data android:name="android.app.device_admin" android:resource="@xml/device_admin"/>
    <intent-filter><action android:name="android.app.action.DEVICE_ADMIN_ENABLED"/></intent-filter>
  </receiver>
</application>
```

**Iniciar o modo quiosque (Lock Task) como Device Owner:**
```kotlin
fun startKiosk(activity: Activity, allowed: Array<String>) {
    val dpm = activity.getSystemService(DevicePolicyManager::class.java)
    val admin = ComponentName(activity, KioskDeviceAdminReceiver::class.java)
    if (dpm.isDeviceOwnerApp(activity.packageName)) {
        dpm.setLockTaskPackages(admin, allowed)
        dpm.addUserRestriction(admin, UserManager.DISALLOW_SAFE_BOOT)
        dpm.addUserRestriction(admin, UserManager.DISALLOW_FACTORY_RESET)
        dpm.addUserRestriction(admin, UserManager.DISALLOW_CONFIG_MOBILE_NETWORKS) // Wi-Fi only
        dpm.setStatusBarDisabled(admin, true)
    }
    activity.startLockTask()
}
```

**Check-in (org.json + HttpURLConnection, sem libs pesadas):**
```kotlin
suspend fun checkin(base: String, token: String, uuid: String, body: JSONObject): JSONObject =
  withContext(Dispatchers.IO) {
    val conn = (URL("$base/checkin/").openConnection() as HttpsURLConnection).apply {
        requestMethod = "POST"
        setRequestProperty("Authorization", "Bearer $token")
        setRequestProperty("X-Device-UUID", uuid)
        setRequestProperty("Content-Type", "application/json")
        doOutput = true; connectTimeout = 8000; readTimeout = 8000
    }
    conn.outputStream.use { it.write(body.toString().toByteArray()) }
    JSONObject(conn.inputStream.bufferedReader().readText())
  }
```

---

## 14. Plano em fases

- **Fase 1 — MVP (telemetria + quiosque básico):** enroll, check-in periódico, launcher travado com apps liberados + câmera/galeria, PIN do TI offline, Wi-Fi only, coleta de dados. Dashboard de monitoramento no Django.
- **Fase 2 — Controle remoto:** `KioskComando` + `/config/` push (bloquear aparelho, mensagem na tela, atualizar lista de apps/PIN sem reinstalar).
- **Fase 3 — Refino:** vínculo automático device↔`Item` por serial, alertas de offline/bateria baixa (reusando o padrão de e-mail do sistema), relatórios e localização histórica no mapa.

---

## 15. Testes em aparelhos de baixa RAM

- Testar em device físico de **2 GB** (ou emulador com 2048 MB).
- Medir RAM com `adb shell dumpsys meminfo com.santacolomba.quiosque` (PSS do serviço em repouso).
- Verificar: app sobrevive ao **Doze**, reinicia no **boot** (`BootReceiver`), não vaza memória após dias (check-ins não acumulam), e a UI não trava sem rede (modo offline).
- Validar: usuário **não** consegue sair sem PIN; apps fora da allowlist **não** abrem; dados móveis **não** transmitem.

---

## 16. Checklist de segurança

- [ ] HTTPS obrigatório (TLS) + (recomendado) certificate pinning.
- [ ] Token por dispositivo, **hash** no servidor, revogável.
- [ ] Código de matrícula de uso único + expiração.
- [ ] PIN do TI nunca em texto no APK; só hash (PBKDF2) em cache cifrado.
- [ ] `EncryptedSharedPreferences` para token/config.
- [ ] Rate limiting e validação de payload nos endpoints.
- [ ] Logs sensíveis apenas em debug.

---

### Resumo do que construir
**No APK (Kotlin nativo, enxuto):** launcher de quiosque (Device Owner + Lock Task), coleta de telemetria, envio por HTTPS com token, PIN do TI offline, Wi-Fi only, allowlist de apps + câmera/galeria, WorkManager para check-in, armazenamento cifrado, boot receiver.
**No Django (aditivo, isolado):** models `KioskDevice`/`KioskCheckin`/`KioskMatricula`(+`KioskComando`), endpoints `/api/quiosque/` com token próprio, `services/quiosque_service.py`, dashboard `/quiosque/`, migration, e HTTPS na frente do servidor.
**Contrato entre os dois:** Seção 4 deste documento.
