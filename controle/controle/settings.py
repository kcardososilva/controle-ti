"""
Django settings for controle project.

Credenciais sensíveis devem ser definidas via variáveis de ambiente.
Crie um arquivo .env na raiz do projeto com as chaves abaixo
(veja .env.example para o modelo).
"""

from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Carrega variáveis do arquivo .env (se existir)
load_dotenv(BASE_DIR / ".env")

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Pasta protegida onde o TI copia o instalador (.apk) do app Quiosque. NÃO fica em
# MEDIA_ROOT — /media/ é servido sem autenticação (django.views.static.serve) e
# qualquer pessoa com a URL acessaria o arquivo. O download do .apk só é liberado
# pela view kiosk_instalador_download, com link de token e validade curta (ver
# services/quiosque_service.py). Defina KIOSK_APK_DIR no .env para usar outro
# caminho (ex.: fora do OneDrive, como o DJANGO_DB_PATH).
KIOSK_APK_DIR = os.environ.get('KIOSK_APK_DIR') or os.path.join(BASE_DIR, 'kiosk_apk')

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError(
        "DJANGO_SECRET_KEY não definida. "
        "Adicione-a ao arquivo .env e reinicie o servidor."
    )

DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

ALLOWED_HOSTS = os.environ.get(
    'DJANGO_ALLOWED_HOSTS',
    '127.0.0.1,localhost'
).split(',')

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'ProjetoEstoque',
    'about',
    'users',
    'widget_tweaks',
    'django.contrib.humanize',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serve os arquivos estaticos (CSS/JS/imagens) de forma
    # comprimida e com cache no navegador, sem passar pela stack do Django.
    # Deve vir logo apos o SecurityMiddleware.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'ProjetoEstoque.middleware.LoginThrottleMiddleware',
    'ProjetoEstoque.middleware.TVAccessMiddleware',
    'ProjetoEstoque.middleware.FornecedorAccessMiddleware',
    'ProjetoEstoque.middleware.LicencaOfficeAccessMiddleware',
]

ROOT_URLCONF = 'controle.urls'

# Loaders de template:
#  • Produção (DEBUG=False) → cached.Loader: cada template é compilado UMA vez e
#    reusado da memória. Como as páginas têm muito CSS inline, isso elimina o
#    re-parse a cada requisição (ganho grande de render).
#  • Dev (DEBUG=True) → loaders simples, para o auto-reload de template continuar.
# APP_DIRS e 'loaders' são mutuamente exclusivos: por isso APP_DIRS sai e a lista
# de loaders assume o mesmo papel — filesystem (DIRS) + app_directories
# (templates dos apps, incluindo o admin).
_TEMPLATE_LOADERS = [
    'django.template.loaders.filesystem.Loader',
    'django.template.loaders.app_directories.Loader',
]
if not DEBUG:
    _TEMPLATE_LOADERS = [('django.template.loaders.cached.Loader', _TEMPLATE_LOADERS)]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'ProjetoEstoque.context_processors.notificacoes',
                'ProjetoEstoque.context_processors.assets',
                'ProjetoEstoque.context_processors.perfil_acesso',
            ],
            'loaders': _TEMPLATE_LOADERS,
        },
    },
]

WSGI_APPLICATION = 'controle.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        # Caminho do banco. Por padrão fica em BASE_DIR/db.sqlite3, mas quando o
        # projeto está numa pasta sincronizada (OneDrive/Dropbox) o SQLite pode
        # falhar com "attempt to write a readonly database" enquanto o serviço de
        # sincronização trava o arquivo. Defina DJANGO_DB_PATH com um caminho
        # LOCAL (fora do OneDrive) para evitar isso — ex.: C:\ProjetoEstoque\db.sqlite3.
        'NAME': os.environ.get('DJANGO_DB_PATH') or (BASE_DIR / 'db.sqlite3'),
        'OPTIONS': {
            'timeout': 20,
        },
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'pt-br'

TIME_ZONE = 'America/Sao_Paulo'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Armazenamento de arquivos: estaticos servidos via WhiteNoise (comprimidos +
# cache). Sem manifesto, para nao quebrar referencias estaticas existentes.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.outlook.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
ALERTA_EMAIL = os.environ.get('ALERTA_EMAIL', '')
ALERTA_EMAILS = [
    e.strip()
    for e in os.environ.get('ALERTA_EMAILS', '').split(',')
    if e.strip()
]

# Destinatário(s) das notificações de movimentação de manutenção (fornecedor ↔ TI).
# Em TESTES aponta para kayque.silva@santacolomba.com.br; em produção defina
# TI_EMAILS no .env (ex.: TI_EMAILS=ti@santacolomba.com.br).
TI_EMAILS = [
    e.strip()
    for e in os.environ.get('TI_EMAILS', 'kayque.silva@santacolomba.com.br').split(',')
    if e.strip()
]


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'

# Cache em memória — mais rápido que FileBasedCache para servidor único local
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "TIMEOUT": 300,
    }
}

# PRTG Network Monitor — credenciais via .env
PRTG_URL      = os.environ.get("PRTG_URL", "")
PRTG_USER     = os.environ.get("PRTG_USER", "")
PRTG_PASSHASH = os.environ.get("PRTG_PASSHASH", "")

# NinjaOne RMM — credenciais via .env (OAuth2 Client Credentials)
# Gerar em: Administração → Apps → API → Adicionar aplicativo OAuth
NINJA_BASE_URL      = os.environ.get("NINJA_BASE_URL", "")
NINJA_CLIENT_ID     = os.environ.get("NINJA_CLIENT_ID", "")
NINJA_CLIENT_SECRET = os.environ.get("NINJA_CLIENT_SECRET", "")
# URI exata cadastrada no app OAuth do NinjaOne — deve corresponder exatamente
NINJA_REDIRECT_URI  = os.environ.get(
    "NINJA_REDIRECT_URI",
    "http://santa-colomba-karitel-qqprmnjdwc.dynamic-m.com:65300/ninja/oauth/callback/",
)

# =============================================================================
# TRANSPORTE E COOKIES
# =============================================================================
#
# ATENÇÃO AO LIGAR O HTTPS: `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` fazem
# o navegador só devolver o cookie em conexão TLS. Ligados enquanto o acesso
# externo ainda é http:// (ver NINJA_REDIRECT_URI no settings.py), NINGUÉM
# entra — o login cai em laço de redirecionamento porque a sessão nunca volta
# ao servidor. Por isso ficam atrás de UMA chave, desligada por padrão: quando
# o domínio externo tiver certificado, defina DJANGO_HTTPS=True no .env.
HTTPS_ATIVO = os.environ.get('DJANGO_HTTPS', 'False') == 'True'

SESSION_COOKIE_SECURE = HTTPS_ATIVO
CSRF_COOKIE_SECURE = HTTPS_ATIVO
SECURE_SSL_REDIRECT = HTTPS_ATIVO

# HSTS só faz sentido com HTTPS, e começa curto de propósito: o cabeçalho é
# IRREVERSÍVEL pelo prazo anunciado. Se o certificado falhar com um ano
# declarado, os navegadores recusam o acesso por http durante um ano e não há
# como contornar do lado do servidor. Suba para 31536000 depois de alguns dias
# de HTTPS estável.
SECURE_HSTS_SECONDS = (
    int(os.environ.get('DJANGO_HSTS_SECONDS', '3600')) if HTTPS_ATIVO else 0
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# Atrás do túnel (dynamic-m.com), o Django só sabe que a requisição original
# era HTTPS por este cabeçalho — sem ele, SECURE_SSL_REDIRECT entra em laço.
if HTTPS_ATIVO:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Django 4+ exige a origem declarada para aceitar POST com Host externo.
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')
    if o.strip()
]
if not CSRF_TRUSTED_ORIGINS:
    _esquema = 'https' if HTTPS_ATIVO else 'http'
    CSRF_TRUSTED_ORIGINS = [
        f'{_esquema}://{h.strip()}'
        for h in ALLOWED_HOSTS
        if h.strip() and h.strip() != '*'
    ]

# ── Vale desde já, não depende de HTTPS ──────────────────────────────────────
SECURE_CONTENT_TYPE_NOSNIFF = True       # navegador não "adivinha" o tipo
SECURE_REFERRER_POLICY = 'same-origin'   # não vaza URL interna ao sair do site
X_FRAME_OPTIONS = 'DENY'                 # nenhuma tela do sistema usa <iframe>
SESSION_COOKIE_HTTPONLY = True

# SameSite=Lax: o cookie não acompanha requisição vinda de outro site, mas
# acompanha o clique num link — é disso que o retorno do OAuth do NinjaOne
# depende. 'Strict' quebraria esse callback.
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

# CSRF_COOKIE_HTTPONLY fica FALSO de propósito. Sete templates leem o token do
# cookie em JS (`document.cookie.match(/csrftoken=([^;]+)/)`) para os POST via
# fetch — sino de notificações (base.html), ficha do equipamento, licenças
# Office, portal do fornecedor, portal de licenças e duas telas de preventiva.
# Ligar isso quebraria os sete SEM erro visível: o fetch sai sem token e leva
# 403 silencioso. O token no cookie não é segredo — a proteção do CSRF vem da
# política de mesma origem, não de esconder o valor.
CSRF_COOKIE_HTTPONLY = False

# =============================================================================
# SESSÃO
# =============================================================================
#
# 12h cobre um turno inteiro. Com SESSION_SAVE_EVERY_REQUEST a janela é
# deslizante: quem está trabalhando não é derrubado no meio do serviço; quem
# largou a estação logada perde a sessão.
#
# O painel de TV é a exceção — fica numa tela de parede, logado por dias. O
# prazo longo dele é dado no login (services/sessao_service.py), não aqui:
# esticar o padrão de todo mundo por causa da TV seria o caminho errado.
SESSION_COOKIE_AGE = int(os.environ.get('DJANGO_SESSION_HORAS', '12')) * 3600
SESSION_SAVE_EVERY_REQUEST = True

# Validade do link de "esqueci minha senha". O padrão do Django é 3 dias —
# longo demais para um e-mail que troca senha sem pedir a antiga.
PASSWORD_RESET_TIMEOUT = (
    int(os.environ.get('DJANGO_RESET_TIMEOUT_HORAS', '2')) * 3600
)

# Remetente das mensagens automáticas (recuperação de senha, alertas).
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'nao-responda@santacolomba.com.br'
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# =============================================================================
# LOGGING
# =============================================================================
#
# Sem LOGGING configurado o Django escreve no console do Waitress: a janela
# fecha e o rastro do erro vai com ela. Aqui erro e evento de segurança vão
# para arquivo rotativo.
#
# O diretório sai de dentro do OneDrive quando DJANGO_LOG_DIR é definido — a
# pasta do projeto é sincronizada, e um arquivo escrito a cada requisição faz o
# OneDrive subir o arquivo sem parar (mesmo motivo do DJANGO_DB_PATH).
LOG_DIR = Path(os.environ.get('DJANGO_LOG_DIR') or (BASE_DIR / 'logs'))
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'detalhado': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
        'simples': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simples',
            'level': 'INFO',
        },
        'arquivo_erros': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOG_DIR / 'erros.log'),
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'encoding': 'utf-8',
            'formatter': 'detalhado',
            'level': 'WARNING',
        },
        'arquivo_seguranca': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOG_DIR / 'seguranca.log'),
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 10,
            'encoding': 'utf-8',
            'formatter': 'detalhado',
            'level': 'INFO',
        },
    },
    'root': {
        'handlers': ['console', 'arquivo_erros'],
        'level': 'INFO',
    },
    'loggers': {
        # Erro 500 com stack trace completo em arquivo.
        'django.request': {
            'handlers': ['console', 'arquivo_erros'],
            'level': 'WARNING',
            'propagate': False,
        },
        # Host não permitido, CSRF rejeitado, tentativa de acesso indevido.
        'django.security': {
            'handlers': ['console', 'arquivo_seguranca'],
            'level': 'INFO',
            'propagate': False,
        },
        # Trilha própria: login, logout, falha e recuperação de senha
        # (services/seguranca_service.py, services/sessao_service.py).
        'seguranca': {
            'handlers': ['console', 'arquivo_seguranca'],
            'level': 'INFO',
            'propagate': False,
        },
        # Silencia o ruído de SQL mesmo com DEBUG=True.
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
